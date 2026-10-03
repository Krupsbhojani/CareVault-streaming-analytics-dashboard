"""Three-layer outlier detection for CGM + synthetic vitals streams.

Layer 1  — rules       (fast per-row; clamp-aware; physiological limits; stuck)
Layer 2  — rolling MAD (per vp per metric; window=60; |z|>3.5; warm-up 20;
                        flagged values excluded from window)
Layer 3  — cross-signal (reclassify a CGM anomaly as sensor_error vs clinical
                         using the heart-rate context)

A single reading can produce multiple alerts. Detector column distinguishes:
  'rules'         | 'mad' | 'cross_signal'
Alert types (per live.alerts CHECK constraint):
  'hypo' | 'hyper' | 'sensor_dropout' | 'rapid_change' | 'custom'

The processor never reads live.vitals_readings.injected_type (its role does
not have SELECT on that column). This detector is called from worker.py; it
sees only the physiological columns, sampling_sec and source_trace_ref.
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

# -----------------------------------------------------------------------------
# constants
# -----------------------------------------------------------------------------

# Physiological plausibility ranges. Values outside -> sensor_error (custom).
# CGM ceiling = 476: max observed organic reading across both cohorts in
# raw.readings_raw is 475.2 mg/dL (a single Shanghai row); AZT1D clamps at 400.
# 476 is a floor guarantee that no legitimate reading fires the rule, and it
# still catches the top portion of injected spike range [400, 600]. Values in
# [400, 476] are caught by MAD instead.
RANGE = {
    "cgm_mgdl":         (20.0, 476.0),
    "heart_rate_bpm":   (30.0, 220.0),
    "systolic_bp_mmhg": (60.0, 250.0),
    "diastolic_bp_mmhg": (30.0, 150.0),
    "spo2_pct":         (70.0, 100.0),
}

# Clinical thresholds on CGM.
CGM_HYPO_MGDL      = 70.0     # below -> hypo alert
CGM_SEVERE_HYPO    = 54.0     # below -> critical
CGM_HYPER_MGDL     = 250.0    # above -> hyper alert
CGM_SEVERE_HYPER   = 300.0    # above -> critical

# CGM values that are known sensor clamps (censoring, not sensor error).
# Excluded from MAD window; DO trigger clinical hypo alert (the underlying
# glucose is still below the clamp — the sensor just can't report lower).
CLAMP_VALUES = frozenset({40.0, 400.0, 39.6})

# MAD detector.
MAD_WINDOW           = 60
MAD_WARMUP           = 20
MAD_Z_THRESHOLD      = 3.5
MAD_MIN_MAD_EPSILON  = 1e-9   # divide-by-zero guard

# Stuck-value rule. Threshold set above the measured organic max run in AZT1D
# (13 consecutive identical CGM readings, p99=3). Injecting stuck runs of
# STUCK_RUN_LENGTH=18 gives a comfortable margin over the organic ceiling.
STUCK_CONSECUTIVE    = 15

# Rate-of-change spike rule.
# Threshold above measured organic p9999 = 14.21 mg/dL/min in raw.readings_raw
# (AZT1D, 5-min-cadence pairs excluding clamp boundaries). Shanghai max = 5.64.
# Injected spikes to [400, 600] from ~120 baseline give ~56-96 mg/dL/min — 4-6x
# over threshold.
ROC_SPIKE_MGDL_PER_MIN = 15.0
# Return-to-baseline confirmation: the next reading must return within
# ROC_RETURN_FRACTION * (jump size) of the pre-jump value. 0.30 means "returned
# at least 70% of the way back". Postprandial climbs continue rising, real
# hypo/hyper trends keep drifting — only a single-point spike snaps back.
ROC_RETURN_FRACTION = 0.30
# Max acceptable gap between the three readings involved in the rule check.
# Above this, treat as "different observation window" and don't compare.
ROC_MAX_GAP_MIN = 20.0

# Cross-signal HR bands (heart_rate_bpm).
HR_ELEVATED_BPM      = 100.0   # co-elevated with CGM hypo suggests real hypo
HR_NORMAL_LO         = 55.0
HR_NORMAL_HI         = 95.0


# -----------------------------------------------------------------------------
# data types
# -----------------------------------------------------------------------------

@dataclass
class Reading:
    """The processor's view of a row from live.vitals_readings.
    Notably absent: injected_type."""
    virtual_patient_id: str
    device_id: str | None
    event_time: datetime
    ingested_at: datetime
    cgm_mgdl: float | None
    heart_rate_bpm: int | None
    systolic_bp_mmhg: int | None
    diastolic_bp_mmhg: int | None
    spo2_pct: float | None
    sampling_sec: int
    source_trace_ref: str | None


@dataclass
class Alert:
    virtual_patient_id: str
    trigger_event_time: datetime
    alert_type: str        # hypo | hyper | sensor_dropout | rapid_change | custom
    severity: str          # info | warn | critical
    rule_id: str
    detector: str          # rules | mad | cross_signal
    score: float | None
    payload: dict


@dataclass
class MetricWindow:
    """Bounded window of non-flagged past values, MAD state."""
    window: deque = field(default_factory=lambda: deque(maxlen=MAD_WINDOW))
    warmup_seen: int = 0

    def update(self, value: float) -> None:
        self.window.append(value)

    def mad_zscore(self, value: float) -> float | None:
        if len(self.window) < 5:
            return None
        med = statistics.median(self.window)
        mad = statistics.median(abs(x - med) for x in self.window)
        if mad < MAD_MIN_MAD_EPSILON:
            return None
        return 0.6745 * (value - med) / mad


@dataclass
class RocPending:
    """A candidate ROC-spike waiting on the next reading to confirm return-to-baseline."""
    candidate_ts: datetime
    candidate_cgm: float
    prior_cgm: float       # cgm at T-1 (the pre-jump value)
    jump_magnitude: float  # abs(candidate_cgm - prior_cgm)


@dataclass
class VpState:
    metrics: dict[str, MetricWindow] = field(default_factory=dict)
    last_cgm: float | None = None
    stuck_run: int = 0
    # ROC bookkeeping.
    prev_cgm: float | None = None
    prev_ts: datetime | None = None
    roc_pending: RocPending | None = None

    def m(self, name: str) -> MetricWindow:
        w = self.metrics.get(name)
        if w is None:
            w = MetricWindow()
            self.metrics[name] = w
        return w


# -----------------------------------------------------------------------------
# detector
# -----------------------------------------------------------------------------

class Detector:
    """Stateful per-vp detector. One Detector instance per worker."""

    def __init__(self):
        self._vps: dict[str, VpState] = {}

    # -- state helpers --

    def state(self, vp_id: str) -> VpState:
        s = self._vps.get(vp_id)
        if s is None:
            s = VpState()
            self._vps[vp_id] = s
        return s

    def prewarm(self, vp_id: str, cgm_values: Iterable[float]) -> None:
        """Feed historical CGM into a vp's MAD window without emitting alerts.
        Used on worker restart to reduce cold-start FP rate."""
        s = self.state(vp_id)
        w = s.m("cgm_mgdl")
        for v in cgm_values:
            if v is None:
                continue
            if v in CLAMP_VALUES:
                continue
            w.update(float(v))
            w.warmup_seen += 1
            s.last_cgm = float(v)

    # -- main entry point --

    def process(self, r: Reading) -> list[Alert]:
        s = self.state(r.virtual_patient_id)
        alerts: list[Alert] = []
        alerts.extend(self._layer1_rules(r, s))
        alerts.extend(self._layer1b_roc_spike(r, s))
        alerts.extend(self._layer2_mad(r, s))
        alerts = self._layer3_cross_signal(r, s, alerts)
        # Bookkeeping for stuck + ROC detection (runs after all layers).
        if r.cgm_mgdl is not None:
            if s.last_cgm is not None and r.cgm_mgdl == s.last_cgm:
                s.stuck_run += 1
            else:
                s.stuck_run = 1
            s.last_cgm = r.cgm_mgdl
            s.prev_cgm = r.cgm_mgdl
            s.prev_ts = r.event_time
        return alerts

    # -- Layer 1: rules --

    def _layer1_rules(self, r: Reading, s: VpState) -> list[Alert]:
        out: list[Alert] = []

        # Sensor dropout: NULL cgm.
        if r.cgm_mgdl is None:
            out.append(Alert(
                virtual_patient_id=r.virtual_patient_id,
                trigger_event_time=r.event_time,
                alert_type="sensor_dropout",
                severity="warn",
                rule_id="rules.cgm_null",
                detector="rules",
                score=None,
                payload={"reason": "cgm_mgdl is NULL"},
            ))
            return out

        # Physiological plausibility (excluding clamp values which are legitimate).
        cgm = float(r.cgm_mgdl)
        lo, hi = RANGE["cgm_mgdl"]
        if cgm not in CLAMP_VALUES and (cgm < lo or cgm > hi):
            out.append(Alert(
                virtual_patient_id=r.virtual_patient_id,
                trigger_event_time=r.event_time,
                alert_type="custom",
                severity="critical",
                rule_id="rules.cgm_out_of_range",
                detector="rules",
                score=float(cgm),
                payload={"cgm_mgdl": cgm, "range": [lo, hi]},
            ))

        # Clinical thresholds. Clamp values ARE alerted (underlying glucose is
        # at least as extreme as the clamp) — clamp-aware only for MAD/spike
        # interpretation, not for clinical alerting.
        if cgm <= CGM_HYPO_MGDL:
            sev = "critical" if cgm <= CGM_SEVERE_HYPO else "warn"
            out.append(Alert(
                virtual_patient_id=r.virtual_patient_id,
                trigger_event_time=r.event_time,
                alert_type="hypo",
                severity=sev,
                rule_id="rules.hypo_threshold",
                detector="rules",
                score=float(CGM_HYPO_MGDL - cgm),
                payload={"cgm_mgdl": cgm, "threshold": CGM_HYPO_MGDL, "clamped": cgm in CLAMP_VALUES},
            ))
        elif cgm >= CGM_HYPER_MGDL:
            sev = "critical" if cgm >= CGM_SEVERE_HYPER else "warn"
            out.append(Alert(
                virtual_patient_id=r.virtual_patient_id,
                trigger_event_time=r.event_time,
                alert_type="hyper",
                severity=sev,
                rule_id="rules.hyper_threshold",
                detector="rules",
                score=float(cgm - CGM_HYPER_MGDL),
                payload={"cgm_mgdl": cgm, "threshold": CGM_HYPER_MGDL, "clamped": cgm in CLAMP_VALUES},
            ))

        # Non-CGM range checks (produce 'custom' with detector='rules').
        for m in ("heart_rate_bpm", "systolic_bp_mmhg", "diastolic_bp_mmhg", "spo2_pct"):
            v = getattr(r, m)
            if v is None:
                continue
            lo, hi = RANGE[m]
            if v < lo or v > hi:
                out.append(Alert(
                    virtual_patient_id=r.virtual_patient_id,
                    trigger_event_time=r.event_time,
                    alert_type="custom",
                    severity="warn",
                    rule_id=f"rules.{m}_out_of_range",
                    detector="rules",
                    score=float(v),
                    payload={m: v, "range": [lo, hi]},
                ))

        # Stuck-value rule: 3 consecutive identical non-clamp CGM readings.
        # This uses s.last_cgm and s.stuck_run BEFORE process() updates them.
        if (s.last_cgm is not None and cgm == s.last_cgm
                and cgm not in CLAMP_VALUES
                and s.stuck_run + 1 >= STUCK_CONSECUTIVE):
            out.append(Alert(
                virtual_patient_id=r.virtual_patient_id,
                trigger_event_time=r.event_time,
                alert_type="custom",
                severity="warn",
                rule_id="rules.cgm_stuck",
                detector="rules",
                score=float(s.stuck_run + 1),
                payload={"cgm_mgdl": cgm, "consecutive": s.stuck_run + 1},
            ))
        return out

    # -- Layer 1b: rate-of-change spike rule --
    # Two-reading rule: at reading T, note a candidate if |Δcgm/Δt| > threshold.
    # At reading T+1, confirm the candidate iff cgm[T+1] returns toward cgm[T-1]
    # within ROC_RETURN_FRACTION of the jump magnitude. This distinguishes a
    # single-point spike (which snaps back) from a postprandial climb (which
    # continues rising) or a real hypo/hyper trend (which drifts).
    #
    # Alert is emitted for reading T (the spike itself), not T+1, so downstream
    # consumers see the correct trigger_event_time. Trade-off: detection is
    # delayed by one sampling interval.

    def _layer1b_roc_spike(self, r: Reading, s: VpState) -> list[Alert]:
        # Rate-of-change is expressed per minute of *source* time. In replay
        # mode event_time is compressed by the speed factor (10x), so we can't
        # use event_time deltas directly — a 5-mg/dL organic drift looks like
        # 100 mg/dL/min in wall time under 10x replay. Instead we use the
        # source sampling cadence (sampling_sec) as the "1 tick = 1 sample"
        # dt, which matches how the ROC threshold was calibrated against
        # raw.readings_raw. This also works unchanged in production (speed=1).
        if r.cgm_mgdl is None:
            s.roc_pending = None  # dropout resets pending check
            return []
        cgm = float(r.cgm_mgdl)
        source_dt_min = r.sampling_sec / 60.0
        alerts: list[Alert] = []

        # 1) If a candidate is pending from the previous reading, decide.
        if s.roc_pending is not None:
            p = s.roc_pending
            distance_from_prior = abs(cgm - p.prior_cgm)
            if distance_from_prior <= ROC_RETURN_FRACTION * p.jump_magnitude:
                alerts.append(Alert(
                    virtual_patient_id=r.virtual_patient_id,
                    trigger_event_time=p.candidate_ts,
                    alert_type="custom",
                    severity="critical",
                    rule_id="rules.cgm_roc_spike",
                    detector="rules",
                    score=p.jump_magnitude,
                    payload={
                        "prior_cgm": p.prior_cgm,
                        "spike_cgm": p.candidate_cgm,
                        "next_cgm": cgm,
                        "jump_mgdl": p.jump_magnitude,
                        "return_distance_mgdl": distance_from_prior,
                    },
                ))
            s.roc_pending = None

        # 2) Consider the current reading as a new candidate.
        if s.prev_cgm is not None and source_dt_min > 0:
            delta = cgm - s.prev_cgm
            roc = abs(delta) / source_dt_min
            if roc > ROC_SPIKE_MGDL_PER_MIN and cgm not in CLAMP_VALUES:
                s.roc_pending = RocPending(
                    candidate_ts=r.event_time,
                    candidate_cgm=cgm,
                    prior_cgm=s.prev_cgm,
                    jump_magnitude=abs(delta),
                )
        return alerts

    # -- Layer 2: rolling MAD --
    # Runs on CGM, HR, and SpO2. CGM MAD is retained for continuity with the
    # existing detector chain and eval; the sweep in verification/mad_sweep.py
    # has shown it produces unshippable FP rates on CGM specifically, so the
    # recommended production configuration drops the CGM MAD alerts (the
    # detector still emits them but the eval mapping now excludes them from
    # 'spike' detection). MAD on HR/SpO2 is retained because postprandial
    # excursions don't apply to those metrics.

    _MAD_METRICS = (
        ("cgm_mgdl",       "rapid_change", "mad.cgm_z_gt_3_5",  True),   # skip clamp values
        ("heart_rate_bpm", "custom",       "mad.hr_z_gt_3_5",   False),
        ("spo2_pct",       "custom",       "mad.spo2_z_gt_3_5", False),
    )

    def _layer2_mad(self, r: Reading, s: VpState) -> list[Alert]:
        alerts: list[Alert] = []
        for metric_name, alert_type, rule_id, skip_clamp in self._MAD_METRICS:
            value = getattr(r, metric_name)
            if value is None:
                continue
            v = float(value)
            if skip_clamp and v in CLAMP_VALUES:
                continue
            w = s.m(metric_name)
            if w.warmup_seen < MAD_WARMUP:
                w.update(v)
                w.warmup_seen += 1
                continue
            z = w.mad_zscore(v)
            if z is None:
                w.update(v)
                continue
            if abs(z) > MAD_Z_THRESHOLD:
                alerts.append(Alert(
                    virtual_patient_id=r.virtual_patient_id,
                    trigger_event_time=r.event_time,
                    alert_type=alert_type,
                    severity="warn",
                    rule_id=rule_id,
                    detector="mad",
                    score=float(abs(z)),
                    payload={metric_name: v, "z": z,
                             "window_median": statistics.median(w.window),
                             "window_size": len(w.window)},
                ))
                # Flagged: do NOT add to the window.
            else:
                w.update(v)
        return alerts

    # -- Layer 3: cross-signal --

    def _layer3_cross_signal(self, r: Reading, s: VpState, prior: list[Alert]) -> list[Alert]:
        """Reclassifies primary CGM alerts using HR context, and appends one
        cross-signal alert per triggering situation. Prior alerts pass through
        unchanged.
        """
        if not prior:
            return prior
        # Look for a MAD anomaly on CGM.
        mad_alert = next((a for a in prior if a.detector == "mad"), None)
        if mad_alert is None:
            # Also handle the clinical hypo case with HR context.
            hypo_alert = next((a for a in prior if a.alert_type == "hypo"), None)
            if hypo_alert is None or r.heart_rate_bpm is None:
                return prior
            hr = float(r.heart_rate_bpm)
            if hr >= HR_ELEVATED_BPM:
                prior.append(Alert(
                    virtual_patient_id=r.virtual_patient_id,
                    trigger_event_time=r.event_time,
                    alert_type="hypo",
                    severity="critical",
                    rule_id="cross_signal.hypo_confirmed_by_hr",
                    detector="cross_signal",
                    score=hr,
                    payload={"hr": hr, "cgm": r.cgm_mgdl, "verdict": "clinical_confirmed"},
                ))
            return prior

        # MAD anomaly present — HR context decides sensor vs clinical.
        if r.heart_rate_bpm is None:
            return prior
        hr = float(r.heart_rate_bpm)
        if HR_NORMAL_LO <= hr <= HR_NORMAL_HI:
            verdict = "sensor_error"
            alert_type = "custom"
        else:
            verdict = "possible_clinical"
            alert_type = "rapid_change"
        prior.append(Alert(
            virtual_patient_id=r.virtual_patient_id,
            trigger_event_time=r.event_time,
            alert_type=alert_type,
            severity="warn",
            rule_id=f"cross_signal.mad_hr_{verdict}",
            detector="cross_signal",
            score=mad_alert.score,
            payload={"hr": hr, "cgm": r.cgm_mgdl, "mad_z": mad_alert.score, "verdict": verdict},
        ))
        return prior


# =============================================================================
# Layer 4 — device-batch analyzer
# =============================================================================
# Not per-reading. Runs periodically. Compares each device batch's median
# glucose against the fleet median over a rolling window. If the batch is
# systematically biased (as with the sim-v1-batch42 firmware in this codebase),
# every recent reading from that batch is labeled with detector='batch_analysis'
# and alert_type='custom'. This is the intended way to catch firmware_offset —
# per-vp online methods can't see a constant bias because it tracks the vp's
# own median.

BATCH_WINDOW_SEC = 120                    # rolling analysis window (seconds)
BATCH_THRESHOLD_MGDL = 8.0                # |batch_median - fleet_median| > this -> alert
BATCH_MIN_BATCH_SIZE = 30                 # need this many readings to trust the median

class BatchAnalyzer:
    """Cross-vp batch analyzer. Consumes DB directly (via processor_role).
    Alerts are emitted once per (vp, event_time)."""

    def __init__(self):
        self._alerted: set[tuple[str, datetime]] = set()

    def analyze(self, conn) -> list[Alert]:
        """Query the DB, decide which batches are offset, emit alerts for
        new (unalerted) readings from those batches."""
        alerts: list[Alert] = []
        with conn.cursor() as cur:
            # Per-firmware median and count over the window.
            cur.execute("""
                SELECT d.firmware_version, count(*), percentile_cont(0.5) WITHIN GROUP (ORDER BY v.cgm_mgdl)
                  FROM live.vitals_readings v
                  JOIN live.device d ON d.device_id = v.device_id
                 WHERE v.event_time > now() - make_interval(secs => %s)
                   AND v.cgm_mgdl IS NOT NULL
                 GROUP BY d.firmware_version
            """, (BATCH_WINDOW_SEC,))
            by_batch = cur.fetchall()  # [(fw, n, median), ...]

            if not by_batch:
                return alerts

            # Fleet median = weighted median across all readings in window
            cur.execute("""
                SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY v.cgm_mgdl)
                  FROM live.vitals_readings v
                 WHERE v.event_time > now() - make_interval(secs => %s)
                   AND v.cgm_mgdl IS NOT NULL
            """, (BATCH_WINDOW_SEC,))
            fleet_median = cur.fetchone()[0]
            if fleet_median is None:
                return alerts

            offset_batches = [
                (fw, n, float(m) - float(fleet_median))
                for (fw, n, m) in by_batch
                if n >= BATCH_MIN_BATCH_SIZE and abs(float(m) - float(fleet_median)) > BATCH_THRESHOLD_MGDL
            ]
            if not offset_batches:
                return alerts

            # For each offset batch, pull the readings in the window and emit
            # alerts for any (vp, event_time) we haven't alerted yet.
            for fw, n_readings, deviation in offset_batches:
                cur.execute("""
                    SELECT v.virtual_patient_id, v.event_time, v.cgm_mgdl
                      FROM live.vitals_readings v
                      JOIN live.device d ON d.device_id = v.device_id
                     WHERE d.firmware_version = %s
                       AND v.event_time > now() - make_interval(secs => %s)
                       AND v.cgm_mgdl IS NOT NULL
                """, (fw, BATCH_WINDOW_SEC))
                for vp_id, ev_time, cgm_val in cur.fetchall():
                    key = (vp_id, ev_time)
                    if key in self._alerted:
                        continue
                    self._alerted.add(key)
                    alerts.append(Alert(
                        virtual_patient_id=vp_id,
                        trigger_event_time=ev_time,
                        alert_type="custom",
                        severity="warn",
                        rule_id="batch_analysis.firmware_offset",
                        detector="batch_analysis",
                        score=float(deviation),
                        payload={
                            "firmware_version": fw,
                            "batch_median_mgdl": float(cgm_val),
                            "fleet_median_mgdl": float(fleet_median),
                            "deviation_mgdl": float(deviation),
                            "n_readings_in_batch": int(n_readings),
                        },
                    ))
        return alerts

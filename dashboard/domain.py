"""Plain-language translation layer.

The dashboard never shows internal identifiers, rule ids, detector names,
or column names to the operator. This module maps the pipeline's technical
signals into the sentences the operator reads.

Everything a page renders about a person's situation flows through here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

# --------------------------------------------------------------------------
# Rule sets: which alerts point at a sensor fault vs a real health event.
# --------------------------------------------------------------------------
SENSOR_RULES = frozenset({
    "rules.cgm_null",
    "rules.cgm_stuck",
    "rules.cgm_roc_spike",
    "rules.cgm_out_of_range",
    "cross_signal.mad_hr_sensor_error",
    "batch_analysis.firmware_offset",
    "rules.heart_rate_bpm_out_of_range",
    "rules.systolic_bp_mmhg_out_of_range",
    "rules.diastolic_bp_mmhg_out_of_range",
    "rules.spo2_pct_out_of_range",
})
CLINICAL_RULES = frozenset({
    "cross_signal.hypo_confirmed_by_hr",
    "cross_signal.mad_hr_possible_clinical",
    "rules.hypo_threshold",
    "rules.hyper_threshold",
})


@dataclass
class Situation:
    """Everything a Page 1 row / Page 2 header needs, in plain language."""
    vp_id: str
    cgm_now: float | None
    age_sec: float | None
    severity: str          # 'emergency' | 'sensor' | 'watch' | 'recovering'
    verdict_label: str     # 'Yes, real' | 'No, bad sensor' | 'Not sure' | 'Checking device'
    verdict_chip_class: str
    going: str             # 'down fast' | 'down' | 'up slowly' | 'steady' | 'recovering' | 'frozen NN min'
    going_dir: str         # 'down' | 'up' | 'flat'
    headline: str          # 'Blood sugar is dangerously low'
    evidence: str          # 'Heart rate is climbing, which is what the body does when sugar drops.'
    is_untrustworthy: bool # strike-through the sugar value


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------
def _verdict(rule_id: str | None, detector: str | None,
             device_offset: float | None, batch_alerted: bool) -> tuple[str, str]:
    """Return (label, chip css class)."""
    # Sensor-fault verdicts (strong: the detector said so).
    if rule_id in SENSOR_RULES:
        return ("No, bad sensor", "chip-sensor")
    if detector == "batch_analysis":
        return ("No, bad sensor", "chip-sensor")
    # Clinical-verdict (strong: cross-signal confirmed).
    if rule_id in ("cross_signal.hypo_confirmed_by_hr", "cross_signal.mad_hr_possible_clinical"):
        return ("Yes, real", "chip-real")
    # The batch analyzer runs on a 120 s window. If this person is on a device
    # with a non-zero firmware_offset and the batch analyzer hasn't caught it
    # yet, we're still checking the device — not "not sure".
    if device_offset is not None and abs(device_offset) > 0 and not batch_alerted:
        return ("Checking device", "chip-checking")
    # Bare threshold with no cross-signal partner yet.
    if rule_id in ("rules.hypo_threshold", "rules.hyper_threshold"):
        return ("Not sure", "chip-unsure")
    # Everything else: not sure.
    return ("Not sure", "chip-unsure")


# --------------------------------------------------------------------------
# Severity — from the value + travel, NOT the rule.
# --------------------------------------------------------------------------
def _severity(cgm_now: float | None, delta_per_min: float | None,
              verdict_label: str) -> str:
    """Return one of: 'emergency' | 'sensor' | 'watch' | 'recovering'.

    Buckets (matches the split the operator sees on the header):
      emergency  = clinical action needed now.
                   cgm < 70, cgm > 300, or falling fast (>= 3 mg/dL/min drop).
      sensor     = confirmed broken monitor (verdict overrides any value).
      watch      = out of safe range but holding steady (181-300 in-value).
      recovering = inside safe range (or on its way back inside it).

    Rationale: ~22 % of raw readings in both cohorts sit outside 70-180 — that
    is the diabetic base rate, not an anomaly. Only the emergency subset
    warrants "action right now".
    """
    if verdict_label == "No, bad sensor":
        return "sensor"
    if cgm_now is None:
        return "watch"
    v = float(cgm_now)
    falling_fast = delta_per_min is not None and delta_per_min <= -3.0
    if v < 70 or v > 300 or falling_fast:
        return "emergency"
    if v > 180:
        return "watch"
    return "recovering"


# --------------------------------------------------------------------------
# Trend words ("going" column)
# --------------------------------------------------------------------------
def _going(cgm_now: float | None, delta_per_min: float | None,
           stuck_consecutive: int | None, sampling_sec: int | None,
           had_recent_hypo: bool) -> tuple[str, str]:
    """Return (phrase, direction). direction is 'down'|'up'|'flat'."""
    if stuck_consecutive and stuck_consecutive >= 15 and sampling_sec:
        minutes = int(round(stuck_consecutive * sampling_sec / 60))
        return (f"frozen {minutes} min", "flat")
    if delta_per_min is None or cgm_now is None:
        return ("steady", "flat")
    d = float(delta_per_min)
    if abs(d) < 0.5:
        return ("steady", "flat")
    if d <= -3.0:
        return ("down fast", "down")
    if d < 0:
        return ("down", "down")
    if had_recent_hypo and cgm_now >= 70 and d > 0.5:
        return ("recovering", "up")
    if d >= 3.0:
        return ("up fast", "up")
    return ("up slowly", "up")


# --------------------------------------------------------------------------
# What happened — headline + evidence
# --------------------------------------------------------------------------
def _payload_get(p: Any, key: str, default=None):
    """payload_json comes back as str or dict depending on driver."""
    if p is None:
        return default
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except (json.JSONDecodeError, TypeError):
            return default
    if isinstance(p, dict):
        return p.get(key, default)
    return default


def _num(x: Any) -> float | None:
    """Best-effort float coercion for payload values."""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _phrase(rule_id: str | None, detector: str | None, payload: Any,
            cgm_now: float | None, hr_now: float | None,
            device_offset: float | None) -> tuple[str, str]:
    """Return (headline, evidence). The evidence line must state the numbers
    that triggered the alert whenever we have them. Never uses rule_id or
    detector wording."""
    # ---- CGM null / dropout
    if rule_id == "rules.cgm_null":
        return (
            "The monitor sent nothing",
            "No reading came through at all — the sensor is loose or the "
            "transmitter is out of range.",
        )
    # ---- CGM impossible value (rules.cgm_out_of_range)
    if rule_id == "rules.cgm_out_of_range":
        v = _num(_payload_get(payload, "cgm_mgdl"))
        rng = _payload_get(payload, "range")
        if v is not None and isinstance(rng, (list, tuple)) and len(rng) == 2:
            return (
                f"Reported {v:.0f} mg/dL — above what the body can reach",
                f"Any real reading must sit between {int(rng[0])} and {int(rng[1])}; "
                f"this one is outside that range.",
            )
        return (
            "Reading is above what the body can reach",
            "Values this extreme come from the sensor, not the person.",
        )
    # ---- Single-point spike (ROC): payload has prior_cgm, spike_cgm, next_cgm
    if rule_id == "rules.cgm_roc_spike":
        prior = _num(_payload_get(payload, "prior_cgm"))
        spike = _num(_payload_get(payload, "spike_cgm"))
        after = _num(_payload_get(payload, "next_cgm"))
        jump  = _num(_payload_get(payload, "jump_mgdl"))
        if spike is not None and prior is not None and after is not None:
            jump_i = int(round(abs(jump))) if jump is not None else int(round(spike - prior))
            return (
                "This number is too high to be real",
                f"Jumped {jump_i} points in one reading (from "
                f"{prior:.0f} to {spike:.0f}), then dropped back to {after:.0f}.",
            )
        return (
            "This number is too high to be real",
            "Real blood sugar can't move this fast, then reset.",
        )
    # ---- Stuck: payload has cgm_mgdl and consecutive
    if rule_id == "rules.cgm_stuck":
        n   = _num(_payload_get(payload, "consecutive"))
        v   = _num(_payload_get(payload, "cgm_mgdl"))
        n_i = int(n) if n is not None else None
        v_txt = f"{v:.0f} mg/dL" if v is not None else "the same number"
        if n_i is not None:
            return (
                f"Held at {v_txt} for {n_i} readings in a row",
                "Real blood sugar always moves a little; this one has not changed at all.",
            )
        return (
            "Same reading over and over",
            "Real blood sugar always moves a little; this one has not changed at all.",
        )
    # ---- Firmware offset: payload has deviation_mgdl, fleet_median_mgdl, n_readings_in_batch
    if detector == "batch_analysis" or rule_id == "batch_analysis.firmware_offset":
        dev = _num(_payload_get(payload, "deviation_mgdl"))
        if dev is None:
            dev = _num(device_offset) or 0.0
        n_batch = _num(_payload_get(payload, "n_readings_in_batch"))
        pts     = int(round(abs(dev)))
        direction = "higher" if dev > 0 else "lower"
        headline = ("This monitor's readings look inflated" if dev > 0
                    else "This monitor's readings look deflated")
        if n_batch is not None:
            return (
                headline,
                f"Its batch of monitors (about {int(n_batch)} recent readings) "
                f"is running {pts} mg/dL {direction} than the rest of the fleet.",
            )
        return (
            headline,
            f"Its batch of monitors is running {pts} mg/dL {direction} than the rest.",
        )
    # ---- Cross-signal: hypo confirmed by racing heart
    if rule_id == "cross_signal.hypo_confirmed_by_hr":
        hr  = _num(_payload_get(payload, "hr"))
        cgm = _num(_payload_get(payload, "cgm")) or cgm_now
        if hr is not None and cgm is not None:
            return (
                "Dangerously low blood sugar, and the pulse agrees",
                f"Blood sugar {cgm:.0f} mg/dL with heart rate {hr:.0f} bpm — "
                f"the body's stress response backs the reading up.",
            )
        return (
            "Dangerously low blood sugar, and the pulse agrees",
            "The heart rate has climbed, which is what the body does when sugar drops.",
        )
    if rule_id == "cross_signal.mad_hr_sensor_error":
        hr  = _num(_payload_get(payload, "hr"))
        cgm = _num(_payload_get(payload, "cgm")) or cgm_now
        z   = _num(_payload_get(payload, "mad_z"))
        if cgm is not None and hr is not None and z is not None:
            return (
                "Sudden change on the monitor, calm pulse",
                f"Blood sugar swung {abs(z):.1f}× its usual jitter (to {cgm:.0f} mg/dL) "
                f"while the heart rate held at {hr:.0f} bpm — a real change would show in the pulse.",
            )
        if cgm is not None and hr is not None:
            return (
                "Sudden change on the monitor, calm pulse",
                f"Blood sugar shifted to {cgm:.0f} mg/dL while the heart rate held "
                f"at {hr:.0f} bpm — a real change would show in the pulse.",
            )
        return (
            "Sudden change with a calm pulse",
            "A real drop or spike would show up in the pulse; this looks like the sensor.",
        )
    if rule_id == "cross_signal.mad_hr_possible_clinical":
        hr  = _num(_payload_get(payload, "hr"))
        cgm = _num(_payload_get(payload, "cgm")) or cgm_now
        if cgm is not None and hr is not None:
            return (
                "Sudden change, and the pulse changed too",
                f"Blood sugar shifted to {cgm:.0f} mg/dL and the heart rate is "
                f"at {hr:.0f} bpm — both signals moved together.",
            )
        return (
            "Sudden change, and the pulse changed too",
            "Both signals moved together, so this is likely real.",
        )
    # ---- Threshold rules (fallback, with HR context if available)
    if rule_id == "rules.hypo_threshold":
        v = _num(_payload_get(payload, "cgm_mgdl")) or cgm_now
        v_i = int(round(v)) if v is not None else None
        if v_i is not None and v_i <= 54:
            headline = f"Blood sugar {v_i} mg/dL — dangerously low"
            ev = "Well below the safe range of 70–180."
        elif v_i is not None:
            headline = f"Blood sugar {v_i} mg/dL — below safe range"
            ev = "Left too long, this can cause fainting."
        else:
            headline = "Blood sugar is below safe range"
            ev = "Left too long, this can cause fainting."
        if hr_now is not None and hr_now >= 100:
            ev = (f"Heart rate at {int(round(hr_now))} bpm — the body's "
                  f"stress response backs the reading up.")
        return (headline, ev)
    if rule_id == "rules.hyper_threshold":
        v = _num(_payload_get(payload, "cgm_mgdl")) or cgm_now
        v_i = int(round(v)) if v is not None else None
        if v_i is not None and v_i >= 300:
            return (
                f"Blood sugar {v_i} mg/dL — far above the safe range",
                "Sustained highs like this damage blood vessels over time.",
            )
        if v_i is not None:
            return (
                f"Blood sugar {v_i} mg/dL — above the safe range",
                "Repeated highs slowly damage blood vessels.",
            )
        return ("Blood sugar is above the safe range",
                "Repeated highs slowly damage blood vessels.")
    # ---- MAD on HR / SpO2
    if rule_id == "mad.hr_z_gt_3_5":
        z = _num(_payload_get(payload, "z"))
        v = _num(_payload_get(payload, "heart_rate_bpm"))
        if v is not None and z is not None:
            return (
                f"Heart rate jumped to {int(round(v))} bpm",
                f"That's {abs(z):.1f}× this person's usual variation.",
            )
        return ("Heart rate jumped out of pattern",
                "The change is bigger than this person's usual variation.")
    if rule_id == "mad.spo2_z_gt_3_5":
        v = _num(_payload_get(payload, "spo2_pct"))
        z = _num(_payload_get(payload, "z"))
        if v is not None and z is not None:
            return (
                f"Oxygen dipped to {v:.1f}%",
                f"That's {abs(z):.1f}× this person's usual variation.",
            )
        return ("Oxygen level dipped out of pattern",
                "The change is bigger than this person's usual variation.")
    # ---- Non-CGM range checks
    if rule_id == "rules.heart_rate_bpm_out_of_range":
        v = _num(_payload_get(payload, "heart_rate_bpm"))
        if v is not None:
            return (f"Heart rate {int(round(v))} bpm — outside normal limits",
                    "Above or below what's medically plausible.")
        return ("Heart rate is outside normal limits",
                "The reading is above or below what's medically plausible.")
    if rule_id == "rules.spo2_pct_out_of_range":
        v = _num(_payload_get(payload, "spo2_pct"))
        if v is not None:
            return (f"Oxygen reading {v:.1f}% — outside normal limits",
                    "Below what's medically plausible; treat as a bad reading.")
        return ("Oxygen reading is outside normal limits",
                "Below what's medically plausible; likely a bad reading.")
    if rule_id in ("rules.systolic_bp_mmhg_out_of_range",
                    "rules.diastolic_bp_mmhg_out_of_range"):
        return ("Blood pressure is outside normal limits",
                "Above or below what's medically plausible.")
    # ---- Fallback (should be rare)
    return ("Something is off with this reading",
            "The system flagged this, but we can't summarise it without checking directly.")


# --------------------------------------------------------------------------
# The main builder
# --------------------------------------------------------------------------
def build_situation(row: pd.Series) -> Situation:
    """Build a Situation from a row of the merged Page 1 query.

    Expected columns:
      vp, cgm_now, event_last, hr_now, spo2_pct, delta_per_min,
      rule_id, detector, payload_json, alert_type, alert_severity,
      firmware_offset, batch_alerted, stuck_consecutive, sampling_sec,
      had_recent_hypo
    """
    cgm_now = None if pd.isna(row.get("cgm_now")) else float(row["cgm_now"])
    hr_now  = None if pd.isna(row.get("hr_now"))  else float(row["hr_now"])
    delta_per_min = None if pd.isna(row.get("delta_per_min")) else float(row["delta_per_min"])
    device_offset = None if pd.isna(row.get("firmware_offset")) else float(row["firmware_offset"])
    stuck = None
    if not pd.isna(row.get("stuck_consecutive")):
        stuck = int(row["stuck_consecutive"])
    sampling = None
    if not pd.isna(row.get("sampling_sec")):
        sampling = int(row["sampling_sec"])
    payload = row.get("payload_json")
    rule_id = None if pd.isna(row.get("rule_id")) else row.get("rule_id")
    detector = None if pd.isna(row.get("detector")) else row.get("detector")

    verdict_label, verdict_class = _verdict(
        rule_id, detector, device_offset, bool(row.get("batch_alerted", False)),
    )
    severity = _severity(cgm_now, delta_per_min, verdict_label)
    going, going_dir = _going(cgm_now, delta_per_min, stuck, sampling,
                              bool(row.get("had_recent_hypo", False)))
    headline, evidence = _phrase(rule_id, detector, payload, cgm_now, hr_now, device_offset)
    # A reading judged untrustworthy is struck through: any sensor verdict
    # OR a NULL reading (the monitor sent nothing).
    is_untrustworthy = verdict_label == "No, bad sensor" or cgm_now is None

    return Situation(
        vp_id=row["vp"],
        cgm_now=cgm_now,
        age_sec=None if pd.isna(row.get("age_sec")) else float(row["age_sec"]),
        severity=severity,
        verdict_label=verdict_label,
        verdict_chip_class=verdict_class,
        going=going,
        going_dir=going_dir,
        headline=headline,
        evidence=evidence,
        is_untrustworthy=is_untrustworthy,
    )


# --------------------------------------------------------------------------
# Person context — cohort + type, never invented values
# --------------------------------------------------------------------------
def person_line(cohort: str | None, diabetes_type: str | None,
                age_years: float | None, duration_years: float | None) -> str:
    """Return the second-line context under 'Person 0142'.

    Never invents values. If raw.patient has age/duration for a Shanghai
    patient, use them; otherwise fall back to the type.
    """
    if age_years is not None and duration_years is not None:
        dtype = "Type 1" if diabetes_type == "T1" else "Type 2"
        return f"{dtype}, age {int(age_years)}, {int(round(duration_years))} y with it"
    if cohort == "AZT1D":
        return "Type 1, on an insulin pump"
    if diabetes_type == "T2":
        return "Type 2 diabetes"
    if diabetes_type == "T1":
        return "Type 1 diabetes"
    return "no history on file"


# --------------------------------------------------------------------------
# Severity → colour token (used by CSS-inline styles)
# --------------------------------------------------------------------------
SEVERITY_COLOR = {
    "emergency":  "#A8102F",
    "sensor":     "#3F3B96",
    "watch":      "#C97A12",
    "recovering": "#1F7A5C",
}
SEVERITY_RANK = {"emergency": 0, "sensor": 1, "watch": 2, "recovering": 3}


def age_sentence(sec: float | None) -> str:
    if sec is None:
        return "—"
    s = int(round(sec))
    if s < 90:
        return f"{s}s old"
    if s < 3600:
        return f"{s // 60}m old"
    return f"{s // 3600}h old"


def age_from_ts(ts: datetime | None) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


# --------------------------------------------------------------------------
# Batch-bias: shared query used by Page 1 (device-check card) and Page 3
# (headline + table). Both pages call load_batch_state() so the numbers match.
# --------------------------------------------------------------------------
# Deviation is computed as (this batch's median) − (median of every OTHER
# batch's readings). That's the "how many points higher than the rest" the
# operator sees, and it doesn't sag when the suspect batch has a lot of vps
# and biases the fleet median toward itself.
BATCH_BIAS_SQL = """
  WITH per_batch AS (
    SELECT d.firmware_version AS fw,
           count(DISTINCT d.virtual_patient_id) AS n_devices,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY v.cgm_mgdl_mean) AS median_cgm,
           count(v.cgm_mgdl_mean)               AS n_readings
      FROM live.device d
      LEFT JOIN live.vitals_1min v
        ON v.virtual_patient_id = d.virtual_patient_id
       AND v.minute_start_utc > now() - interval '15 minutes'
       AND v.cgm_mgdl_mean IS NOT NULL
     GROUP BY d.firmware_version
  ),
  fleet AS (
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY cgm_mgdl_mean) AS median_fleet
      FROM live.vitals_1min
     WHERE minute_start_utc > now() - interval '15 minutes'
       AND cgm_mgdl_mean IS NOT NULL
  ),
  -- median across every reading that did NOT come from this batch
  others_median AS (
    SELECT b.fw,
           (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY v2.cgm_mgdl_mean)
              FROM live.vitals_1min v2
              JOIN live.device d2 ON d2.virtual_patient_id = v2.virtual_patient_id
             WHERE d2.firmware_version <> b.fw
               AND v2.minute_start_utc > now() - interval '15 minutes'
               AND v2.cgm_mgdl_mean IS NOT NULL)  AS median_others
      FROM per_batch b
  )
  SELECT p.fw,
         p.n_devices,
         p.median_cgm,
         o.median_others,
         f.median_fleet,
         (p.median_cgm - f.median_fleet)  AS deviation_from_fleet,
         (p.median_cgm - o.median_others) AS deviation_from_others
    FROM per_batch p
    LEFT JOIN others_median o USING (fw)
    CROSS JOIN fleet f
   ORDER BY p.n_devices DESC
"""

# Batch is called a "suspect" when its median is >= this many mg/dL away from
# the rest. Matches the batch analyzer's BATCH_THRESHOLD_MGDL in
# processor/outliers.py so a flagged batch here matches what live.alerts holds.
BATCH_SUSPECT_THRESHOLD_MGDL = 8.0


def batch_suspect_from_df(df: "pd.DataFrame"):
    """Given a DataFrame from BATCH_BIAS_SQL, return (suspect_row, others_row).

    Suspect selection uses |deviation_from_fleet| (weighted by every reading)
    rather than |deviation_from_others|. With only two batches those two
    numbers are almost equal in magnitude but opposite in sign, so using the
    fleet-weighted median unambiguously picks the minority (the group whose
    median is furthest from the population's median).
    """
    if df.empty:
        return None, None
    d = df.dropna(subset=["median_cgm", "deviation_from_fleet"]).copy()
    if d.empty:
        return None, None
    d["abs_dev_fleet"] = d["deviation_from_fleet"].abs()
    suspects = d[d["abs_dev_fleet"] >= BATCH_SUSPECT_THRESHOLD_MGDL].sort_values(
        "abs_dev_fleet", ascending=False,
    )
    if suspects.empty:
        return None, None
    suspect = suspects.iloc[0]
    others = d[d["fw"] != suspect["fw"]]
    return suspect, others

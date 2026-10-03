"""Online glucose forecasting — 15-minute horizon, streaming, prequential.

Runs inside processor/worker.py, per-vp, per micro-batch. Predictions are
staged in `pending_writes` and flushed by the worker inside the same
transaction as alerts and worker_cursor (see WHY_NOT_SEPARATE_CONSUMER).

Four models are scored on every reading:
  - persistence           :  ŷ = current glucose
  - linear_trend          :  ŷ = current + slope15 * horizon_minutes
  - river_linreg          :  StandardScaler | LinearRegression (River)
  - river_ht              :  HoeffdingAdaptiveTreeRegressor    (River)

Labels arrive 15 event-time minutes later (a genuine delayed-label problem).
Pending predictions live in a per-vp deque capped at MAX_QUEUE; when a new
reading's event_time is within TOLERANCE_SEC of a pending prediction's
target_time, that pending entry resolves. On resolution:
  1. Compute abs_error and record actual for every model in one shot.
  2. Feed (features, actual) to River models via learn_one — prequential.

The features and label MUST NOT include the current reading's own value
when used as a label (prequential requires: score first with what we knew,
then learn from what we now know). The test in tests/test_ml.py checks this
alignment on a known-function synthetic patient.
"""
from __future__ import annotations

import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from river import compose, linear_model, optim, preprocessing, tree

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HORIZON_MIN            = 15                     # forecast horizon in SOURCE time
HORIZON_SEC            = HORIZON_MIN * 60       # (same, seconds — source time)
TOLERANCE_SEC_SOURCE   = 60                     # label-time tolerance in SOURCE seconds
# Reasoning for TOLERANCE_SEC_SOURCE=60: AZT1D readings are 5-minute source
# cadence, so a reading within ±60 s of a prediction's target source time is
# the correct label under any reasonable clock jitter — the next sample is
# 5 min away. For Shanghai (15-min cadence) the tolerance is even more
# conservative. In event_time terms the tolerance is scaled by 1/speed.
MAX_QUEUE_PER_VP       = 120                    # was 40; raised after seeing
                                                # queue-cap evictions dominate
                                                # at 10x replay (horizon = 90s
                                                # event-time, cadence 30s
                                                # event-time → only ~3 slots
                                                # of lookahead were available).
                                                # ~10 h at AZ 5-min cadence.
LAG_MINUTES            = (5, 10, 15)            # glucose lags in feature vector
SLOPE_WINDOW_MIN       = 30                     # slope window used by the model's
                                                # feature `slope_15min` (name kept
                                                # for schema stability, length now
                                                # 30 min to reduce slope-noise)
ROLLING_WINDOW_MIN     = 60                     # rolling mean/std window
KEEP_RECENT_MIN        = 65                     # per-vp buffer size (a bit > rolling window)

# linear_trend extrapolation clip. Published physiologic maxima for sustained
# glucose rate-of-change are around ±4 mg/dL/min (dawn phenomenon, post-prandial
# or fast-acting insulin). Over the 15-min horizon that bounds the plausible
# change at ±60 mg/dL. Clipping avoids the pathological case where a transient
# steep slope (common on noisy 5-min CGM) is projected 15 min out and lands
# well outside the physiologic envelope.
LINEAR_TREND_MAX_DELTA_MGDL = 60.0

# Alerts whose presence on the CURRENT reading marks it as an untrustworthy
# CGM value (do not learn from it, do not use it as a label).
UNTRUSTWORTHY_RULE_IDS = frozenset({
    "rules.cgm_null",
    "rules.cgm_out_of_range",
    "rules.cgm_stuck",
    "rules.cgm_roc_spike",
    "mad.cgm_z_gt_3_5",
    "batch_analysis.firmware_offset",
})


WHY_NOT_SEPARATE_CONSUMER = """
A separate ML consumer would need its OWN cursor and its own dedup state, and
would drift from the worker's cursor under any restart / rebalance / late-
arrival scenario. Worse, its writes to live.predictions could not be committed
in the same transaction as the alerts and the reading-processed cursor, so a
dashboard querying both would occasionally see either a prediction with no
alert or an alert with no prediction for the same reading. Extending the
existing worker keeps a single cursor system, single dedup, and one atomic
commit per micro-batch — features + score + prediction row all reflect the
same view of the reading.
"""


# ---------------------------------------------------------------------------
# Clarke Error Grid
# ---------------------------------------------------------------------------
# Zone boundaries per:
#   Clarke, W.L., et al. (1987). Evaluating clinical accuracy of systems for
#   self-monitoring of blood glucose. Diabetes Care 10(5):622-628.
# Implementation follows the widely-used piecewise definition summarized in:
#   https://en.wikipedia.org/wiki/Clarke_Error_Grid  (accessed via boundaries
#   verified against Clarke 1987 Table 1). Units mg/dL. Reference = actual,
#   prediction = model output. Returned zone is one of "A","B","C","D","E".

def clarke_error_grid_zone(reference: float, predicted: float) -> str:
    r = float(reference)
    p = float(predicted)
    if r < 0 or p < 0:
        return "E"
    # Zone A: within 20% of reference, OR both below 70.
    if (r <= 70 and p <= 70) or abs(p - r) <= 0.2 * r:
        return "A"
    # Zone E: dangerous inversions (predicted hypo, actual hyper, or vice versa).
    if (r >= 180 and p <= 70) or (r <= 70 and p >= 180):
        return "E"
    # Zone D: fails to detect hypo/hyper.
    if (r >= 240 and 70 <= p <= 180) or (r <= 70 and 70 <= p <= 180):
        return "D"
    # Zone C: over-correction risk.
    if (70 <= r <= 290 and p >= r + 110) or (130 <= r <= 180 and p <= (7 / 5) * r - 182):
        return "C"
    # Otherwise Zone B (benign error).
    return "B"


# ---------------------------------------------------------------------------
# Model wrappers (uniform predict/learn interface)
# ---------------------------------------------------------------------------

class BaselineModel:
    """No online learning; deterministic function of features."""
    name = "baseline"
    def predict(self, features: dict) -> float:
        raise NotImplementedError
    def learn(self, features: dict, actual: float) -> None:
        pass  # no-op


class PersistenceModel(BaselineModel):
    name = "persistence"
    def predict(self, features: dict) -> float:
        v = features.get("cgm")
        return float(v) if v is not None else float("nan")


class LinearTrendModel(BaselineModel):
    name = "linear_trend"
    def predict(self, features: dict) -> float:
        cgm = features.get("cgm")
        slope = features.get("slope_15min")  # source-time mg/dL per min, measured over SLOPE_WINDOW_MIN
        if cgm is None or slope is None:
            return float("nan") if cgm is None else float(cgm)
        # Project HORIZON_MIN source minutes ahead, clipped to a physiologic
        # plausible delta so a noisy transient slope can't send the forecast
        # outside the sensor range.
        delta = float(slope) * HORIZON_MIN
        if delta >  LINEAR_TREND_MAX_DELTA_MGDL: delta =  LINEAR_TREND_MAX_DELTA_MGDL
        if delta < -LINEAR_TREND_MAX_DELTA_MGDL: delta = -LINEAR_TREND_MAX_DELTA_MGDL
        return float(cgm) + delta


class RiverModel:
    """River pipeline with online predict/learn.
    Non-finite predictions are logged and skipped so they can't poison the
    running metric; non-finite labels are not learned from."""
    def __init__(self, name: str, pipeline, warmup_examples: int = 0):
        self.name = name
        self._pipeline = pipeline
        self._warmup_examples = warmup_examples
        self._n_learned = 0
        self._n_pred_nonfinite = 0
        self._n_pred_warmup_skipped = 0

    def predict(self, features: dict) -> float:
        # Warm-up gate: for models that need many examples to form a usable
        # prediction (e.g. HoeffdingAdaptiveTree needs splits), skip emission
        # until the global model has learned from `warmup_examples` labels.
        if self._n_learned < self._warmup_examples:
            self._n_pred_warmup_skipped += 1
            return float("nan")
        try:
            v = self._pipeline.predict_one(features)
        except Exception:
            self._n_pred_nonfinite += 1
            return float("nan")
        if v is None:
            return float("nan")
        v = float(v)
        if not math.isfinite(v):
            # River LinReg can produce inf after divergence; never let an
            # inf prediction land in live.predictions.
            self._n_pred_nonfinite += 1
            return float("nan")
        return v

    def learn(self, features: dict, actual: float) -> None:
        if not math.isfinite(actual):
            return
        try:
            self._pipeline.learn_one(features, float(actual))
            self._n_learned += 1
        except Exception:
            pass


def build_models() -> dict[str, Any]:
    # river_linreg: target standardization + explicit small SGD learning rate.
    #
    # Root cause of the 1e10 divergence we saw: `linear_model.LinearRegression`
    # defaults to `optim.SGD(0.01)`, and the target (glucose in mg/dL) spans
    # 40-400. StandardScaler normalizes features but NOT the target. So each
    # gradient step is proportional to (ŷ - y) * x_scaled with |y| up to ~400.
    # Early updates push the weights huge; subsequent predictions overshoot,
    # feeding bigger residuals back in — the loop diverges.
    #
    # TargetStandardScaler alone was tried first and was not enough: during
    # the scaler's own warm-up a few early examples still see unscaled target
    # and blow the weights. Lowering the SGD learning rate 10× (0.01 → 1e-3)
    # on top of target standardization keeps the per-step update bounded.
    linreg = preprocessing.TargetStandardScaler(
        regressor=compose.Pipeline(
            preprocessing.StandardScaler(),
            linear_model.LinearRegression(optimizer=optim.SGD(1e-3)),
        )
    )
    # river_ht: warm-up gate — don't emit a prediction until the global tree
    # has learned from HT_WARMUP_EXAMPLES labels. Below this count the tree is
    # a single leaf outputting the running mean, which produces wild per-vp
    # errors because the global mean is far from any one vp's current value.
    # river_ht: warm-up gate — don't emit a prediction until the global tree
    # has learned from HT_WARMUP_EXAMPLES labels. Below this count the tree
    # is a single leaf outputting the running mean; applied globally across
    # 2000 vps this produces huge per-vp errors because the fleet mean is
    # not close to any one patient's current reading.
    #
    # HoeffdingAdaptiveTreeRegressor needs enough samples to attempt a split.
    # The library's default grace_period (which controls when a split test
    # is attempted) is 200 at the leaf — we set the gate at 500 global
    # examples to make sure the root split has had a shot before any
    # prediction is scored.
    ht = tree.HoeffdingAdaptiveTreeRegressor(seed=42)
    return {
        "persistence":  PersistenceModel(),
        "linear_trend": LinearTrendModel(),
        "river_linreg": RiverModel("river_linreg", linreg),
        "river_ht":     RiverModel("river_ht", ht, warmup_examples=500),
    }


# ---------------------------------------------------------------------------
# Per-vp state
# ---------------------------------------------------------------------------

@dataclass
class VpMlContext:
    """Static context per vp: timezone, cadence, cohort, replay speed.
    Loaded once at startup from live.virtual_patient × raw.readings_raw."""
    source_tz: str = "UTC"            # IANA name; used for local hour features
    sampling_sec: int = 300           # source cadence
    diabetes_type: str = "T1"         # T1 / T2 categorical feature
    # Replay speed factor (10x -> event_time compressed by 10x). HORIZON is
    # defined in SOURCE time; event_time horizon = HORIZON_SEC / speed_factor.
    # In production (speed=1) event_time == source time, so this becomes a
    # no-op. In 10x replay the target event_time is only 90s ahead, not 15min.
    speed_factor: float = 1.0

    def horizon_event_td(self) -> timedelta:
        return timedelta(seconds=HORIZON_SEC / max(self.speed_factor, 1e-9))

    def tolerance_event_td(self) -> timedelta:
        return timedelta(seconds=TOLERANCE_SEC_SOURCE / max(self.speed_factor, 1e-9))


@dataclass
class PendingPrediction:
    source_event_time: datetime       # event_time of the READING we predicted from
    target_event_time: datetime       # source + horizon
    predicted_by_model: dict          # {model_name: predicted_value}
    features: dict                    # snapshot at predict time — for learning
    latency_ms: int = 0               # score-time latency
    made_at: float = field(default_factory=time.monotonic)


@dataclass
class VpMlState:
    ctx: VpMlContext = field(default_factory=VpMlContext)
    # Buffer of recent trustworthy CGM readings for lag/slope/rolling features.
    # (event_time, cgm) — capped by wall clock, keeps roughly KEEP_RECENT_MIN.
    recent: deque = field(default_factory=deque)
    # FIFO of pending predictions, capped by MAX_QUEUE_PER_VP.
    pending: deque = field(default_factory=deque)
    # Bookkeeping counters (reset each pipeline run) — broken out by cause.
    n_predictions_made: int = 0
    n_resolved: int = 0
    n_evicted_queue_cap: int = 0
    n_evicted_label_too_late: int = 0
    n_evicted_label_untrustworthy: int = 0
    n_excluded_untrustworthy: int = 0


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class MlPipeline:
    """One MlPipeline per worker. Holds models + per-vp state."""

    def __init__(self):
        self.models = build_models()
        self.states: dict[str, VpMlState] = {}
        # Staged writes: rows to INSERT into live.predictions, and rows to
        # UPDATE (resolve labels). Flushed by worker inside its txn.
        self.pending_inserts: list[tuple] = []
        self.pending_resolutions: list[tuple] = []
        # Aggregate counters for the run (reset per worker session).
        self.n_predictions_made_total = 0
        self.n_resolved_total = 0
        self.n_evicted_queue_cap_total = 0
        self.n_evicted_label_too_late_total = 0
        self.n_evicted_label_untrustworthy_total = 0
        self.n_excluded_untrustworthy_total = 0
        self._sample_vector_printed = False
        self._sample_vector_vp: str | None = None

    # -- state helpers --

    def state(self, vp_id: str) -> VpMlState:
        s = self.states.get(vp_id)
        if s is None:
            s = VpMlState()
            self.states[vp_id] = s
        return s

    def set_context(self, vp_id: str, source_tz: str, sampling_sec: int,
                    diabetes_type: str, speed_factor: float = 1.0) -> None:
        s = self.state(vp_id)
        s.ctx.source_tz = source_tz
        s.ctx.sampling_sec = int(sampling_sec)
        s.ctx.diabetes_type = diabetes_type
        s.ctx.speed_factor = float(speed_factor)

    # -- feature extraction --

    def _lookup_lag(self, recent: deque, target_time: datetime, tol_sec: int = 45) -> float | None:
        """Return the CGM value at (target_time - lag) if a reading exists within tol_sec."""
        for ts, cgm in reversed(recent):
            dt = abs((ts - target_time).total_seconds())
            if dt <= tol_sec:
                return float(cgm)
            if ts < target_time - timedelta(seconds=tol_sec):
                return None
        return None

    def _build_features(self, r, s: VpMlState) -> dict:
        """Build the feature dict. Only numeric features (so River's
        StandardScaler can see all of them) and constant features are dropped
        entirely rather than fed as zeros. Categorical `diabetes_type` is
        one-hot encoded. Source for every feature is in feature_sources()."""
        f: dict[str, float | int] = {}
        cgm = float(r.cgm_mgdl) if r.cgm_mgdl is not None else None
        f["cgm"] = cgm if cgm is not None else float("nan")

        # Lags at 5/10/15 minutes back. Lags use SOURCE-time offsets so a
        # replayed 10x stream (event_time compressed) still looks up the right
        # physical past readings.
        source_dt = max(s.ctx.speed_factor, 1e-9)
        for lag_min in LAG_MINUTES:
            target = r.event_time - timedelta(minutes=lag_min / source_dt)
            v = self._lookup_lag(s.recent, target,
                                 tol_sec=int(45 / source_dt))
            f[f"lag_{lag_min}min"] = v if v is not None else (cgm if cgm is not None else float("nan"))

        # Slope: SOURCE-minute units. Scale window by speed so we actually look
        # at 15 source minutes of history, not 15 compressed event-time minutes.
        slope = self._slope(s.recent, r.event_time,
                            window_min=SLOPE_WINDOW_MIN / source_dt,
                            report_in_source_time=True,
                            speed=source_dt)
        f["slope_15min"] = slope if slope is not None else 0.0

        # Rolling mean/std over last ROLLING_WINDOW_MIN source minutes.
        window_td = timedelta(minutes=ROLLING_WINDOW_MIN / source_dt)
        vals = [v for ts, v in s.recent
                if r.event_time - window_td <= ts <= r.event_time]
        if len(vals) >= 2:
            f["roll_mean_60min"] = float(statistics.mean(vals))
            f["roll_std_60min"] = float(statistics.pstdev(vals))
        else:
            f["roll_mean_60min"] = cgm if cgm is not None else float("nan")
            f["roll_std_60min"] = 0.0

        f["heart_rate_bpm"] = float(r.heart_rate_bpm) if r.heart_rate_bpm is not None else 70.0

        # Local hour sin/cos in the patient's source timezone. For replay the
        # event_time hour is distorted by the speed factor (10x replay means
        # 24 wall hours = 240 source hours), so we convert to source time first.
        source_delta = (r.event_time - s.ctx.__dict__.get("_wall_start", r.event_time)).total_seconds() * source_dt
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(s.ctx.source_tz)
            local = r.event_time.astimezone(tz)
            hour = local.hour + local.minute / 60.0
        except Exception:
            hour = r.event_time.hour + r.event_time.minute / 60.0
        f["hour_sin"] = math.sin(2 * math.pi * hour / 24.0)
        f["hour_cos"] = math.cos(2 * math.pi * hour / 24.0)

        # Patient/cohort context. `diabetes_type` is one-hot (dropping one
        # dummy for the baseline is not necessary for tree learners; for
        # River LinearRegression StandardScaler + bias absorb the collinearity.)
        f["sampling_sec"] = int(s.ctx.sampling_sec)
        f["is_t1"] = 1.0 if s.ctx.diabetes_type == "T1" else 0.0
        f["is_t2"] = 1.0 if s.ctx.diabetes_type == "T2" else 0.0

        return f

    def _slope(self, recent: deque, now_ts: datetime, window_min: float,
               report_in_source_time: bool = False, speed: float = 1.0
               ) -> float | None:
        """Least-squares slope (mg/dL per minute) over `window_min` event-time
        minutes of the most recent buffer. If report_in_source_time, divide
        the x-axis by `speed` so the output is mg/dL per SOURCE minute (so the
        threshold-free slope value stays meaningful under 10x replay)."""
        cutoff = now_ts - timedelta(minutes=window_min)
        pts = [(ts, v) for ts, v in recent if ts >= cutoff]
        if len(pts) < 2:
            return None
        t0 = pts[0][0]
        time_scale = (1.0 / speed) if report_in_source_time else 1.0
        xs = [(ts - t0).total_seconds() / 60.0 * time_scale for ts, _ in pts]
        ys = [v for _, v in pts]
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
        den = sum((xs[i] - mean_x) ** 2 for i in range(n))
        if den == 0:
            return 0.0
        return num / den

    def feature_sources(self) -> list[tuple[str, str]]:
        """Return (feature_name, source) for the report. Audit trail — used to
        prove no leakage. `injected_type` is not in this list; nothing in the
        feature builder reads it."""
        return [
            ("cgm",              "reading.cgm_mgdl"),
            ("lag_5min",         "per-vp buffer lookup at event_time - 5 source min"),
            ("lag_10min",        "per-vp buffer lookup at event_time - 10 source min"),
            ("lag_15min",        "per-vp buffer lookup at event_time - 15 source min"),
            ("slope_15min",      "least-squares slope (mg/dL per SOURCE min) over last 15 source min"),
            ("roll_mean_60min",  "mean of per-vp buffer over last 60 source min"),
            ("roll_std_60min",   "population std of per-vp buffer over last 60 source min"),
            ("heart_rate_bpm",   "reading.heart_rate_bpm"),
            ("hour_sin",         "sin(2π·hour/24), event_time.astimezone(vp.source_tz)"),
            ("hour_cos",         "cos(2π·hour/24), event_time.astimezone(vp.source_tz)"),
            ("sampling_sec",     "live.virtual_patient.sampling_sec (via vitals_readings)"),
            ("is_t1",            "one-hot of raw.readings_raw.diabetes_type"),
            ("is_t2",            "one-hot of raw.readings_raw.diabetes_type"),
        ]

    # -- main entrypoint --

    def process_reading(self, r, alert_rule_ids_this_reading: set[str]) -> None:
        """Called for every reading, in event_time order per vp.

        Sequence (prequential + delayed-label-safe):
          1. Try to RESOLVE any pending predictions this reading is the label for.
             - If the reading is untrustworthy, skip resolution (label rejected).
          2. Build features from state that IS current (before this reading is
             added to the buffer, no future info).
          3. SCORE with all models, stage a pending prediction.
          4. Only then, if the reading is trustworthy, add it to the per-vp
             buffer so future features/lookups can see it.
        """
        s = self.state(r.virtual_patient_id)
        untrustworthy = bool(alert_rule_ids_this_reading & UNTRUSTWORTHY_RULE_IDS)

        # ---- Step 1: resolve pending predictions whose target matches this reading. ----
        if not untrustworthy and r.cgm_mgdl is not None:
            self._resolve_pending(r, s)
        elif untrustworthy:
            s.n_excluded_untrustworthy += 1
            self.n_excluded_untrustworthy_total += 1
            # Also evict any pending predictions whose target time is this
            # reading — we can't trust this label, so those predictions
            # become unresolved (dropped, not learned from, counted).
            self._evict_pending_at(r.event_time, s, reason="label_untrustworthy")

        # ---- Step 2: build features. ----
        if r.cgm_mgdl is None:
            # Can't score without a current CGM value; nothing to do.
            return
        t0 = time.monotonic()
        features = self._build_features(r, s)

        # Print one complete feature vector per worker (first vp seen) so the
        # operator can see exactly what the models consume.
        if not self._sample_vector_printed:
            self._sample_vector_vp = r.virtual_patient_id
            self._sample_vector_printed = True
            import sys as _sys, json as _json
            print(f"[ml] first feature vector (vp={r.virtual_patient_id}, "
                  f"event_time={r.event_time.isoformat()}, "
                  f"speed_factor={s.ctx.speed_factor}):", file=_sys.stderr, flush=True)
            print(_json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                               for k, v in features.items()}, indent=2),
                  file=_sys.stderr, flush=True)

        # ---- Step 3: score with all models, queue for later learning. ----
        preds = {}
        for name, model in self.models.items():
            preds[name] = model.predict(features)
        latency_ms = int((time.monotonic() - t0) * 1000)

        pending = PendingPrediction(
            source_event_time=r.event_time,
            target_event_time=r.event_time + s.ctx.horizon_event_td(),
            predicted_by_model=preds,
            features=features,
            latency_ms=latency_ms,
        )

        # Stage inserts NOW (with actual=NULL) so the dashboard sees them
        # immediately. UPDATE happens on resolution.
        for name, y in preds.items():
            if y != y:  # NaN check
                continue
            self.pending_inserts.append((
                r.virtual_patient_id,
                name,
                "v1",
                r.event_time,              # input_window_end
                HORIZON_SEC,
                pending.target_event_time, # predicted_ts_utc
                float(y),                  # predicted_cgm_mgdl
                latency_ms,
            ))
            s.n_predictions_made += 1
            self.n_predictions_made_total += 1

        # Cap queue: evict oldest if too many pending.
        while len(s.pending) >= MAX_QUEUE_PER_VP:
            s.pending.popleft()
            s.n_evicted_queue_cap += 1
            self.n_evicted_queue_cap_total += 1
        s.pending.append(pending)

        # ---- Step 4: add reading to the buffer (only if trustworthy). ----
        if not untrustworthy:
            cutoff = r.event_time - timedelta(minutes=KEEP_RECENT_MIN)
            s.recent.append((r.event_time, float(r.cgm_mgdl)))
            while s.recent and s.recent[0][0] < cutoff:
                s.recent.popleft()

    def _resolve_pending(self, r, s: VpMlState) -> None:
        """Resolve any pending predictions whose target_event_time is within
        tolerance of this reading's event_time. Score + learn."""
        actual = float(r.cgm_mgdl)
        target_ts = r.event_time
        tol_s = s.ctx.tolerance_event_td().total_seconds()
        # Iterate from the front (oldest); pending is FIFO by target.
        while s.pending:
            p = s.pending[0]
            dt = (p.target_event_time - target_ts).total_seconds()
            if abs(dt) <= tol_s:
                # Match. Resolve.
                s.pending.popleft()
                for name, y_hat in p.predicted_by_model.items():
                    model = self.models[name]
                    # Always feed the online learner, even for predictions
                    # that were skipped as NaN (warm-up gate or non-finite
                    # guard). Skipping learn too would stall the warmup
                    # counter and the gate would never open.
                    model.learn(p.features, actual)
                    if y_hat != y_hat:  # NaN emission; nothing to score
                        continue
                    abs_err = abs(actual - y_hat)
                    self.pending_resolutions.append((
                        actual, abs_err,        # SET clause
                        r.virtual_patient_id, p.source_event_time, name,  # WHERE
                    ))
                s.n_resolved += 1
                self.n_resolved_total += 1
            elif dt < -tol_s:
                # Target is now in the past by more than tolerance — the
                # label window closed without a reading landing in it.
                s.pending.popleft()
                s.n_evicted_label_too_late += 1
                self.n_evicted_label_too_late_total += 1
            else:
                # dt > tol_s: target still in the future. Stop.
                break

    def _evict_pending_at(self, target_ts: datetime, s: VpMlState, reason: str) -> None:
        """Drop any pending whose target_event_time is at target_ts ± tolerance
        (called when the incoming reading that would have been the label is
        untrustworthy)."""
        keep = deque()
        tol_s = s.ctx.tolerance_event_td().total_seconds()
        while s.pending:
            p = s.pending.popleft()
            if abs((p.target_event_time - target_ts).total_seconds()) <= tol_s:
                s.n_evicted_label_untrustworthy += 1
                self.n_evicted_label_untrustworthy_total += 1
                continue
            keep.append(p)
        s.pending = keep

    def count_still_pending(self) -> int:
        """Sum of pending predictions across all vps — i.e. predictions whose
        label window hasn't closed yet. On worker shutdown this becomes the
        'run ended before label arrived' bucket."""
        return sum(len(s.pending) for s in self.states.values())

    # -- flush (called by worker inside its txn) --

    def drain_writes(self) -> tuple[list[tuple], list[tuple]]:
        """Return (inserts, resolutions) accumulated since last drain. The
        caller (worker) will write them inside the alerts/cursor transaction
        and only then clear the buffers."""
        inserts = self.pending_inserts
        resolutions = self.pending_resolutions
        self.pending_inserts = []
        self.pending_resolutions = []
        return inserts, resolutions

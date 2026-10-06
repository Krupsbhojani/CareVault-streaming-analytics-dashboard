"""Online ML correctness tests.

The critical one is test_feature_label_alignment: an off-by-one between the
features we score with and the label we learn from would make any model look
spectacular for the wrong reason. We assert that for a patient whose glucose
follows a known function g(t), the `actual` recorded for a prediction made at
input_time t is exactly g(t + HORIZON_MIN) — not g(t + HORIZON_MIN ± one
sample) or g(t) itself.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from processor.ml import (  # noqa: E402
    HORIZON_MIN, HORIZON_SEC, MlPipeline, TOLERANCE_SEC_SOURCE,
    UNTRUSTWORTHY_RULE_IDS, SLOPE_WINDOW_MIN, clarke_error_grid_zone,
)


@dataclass
class FakeReading:
    """Minimal stand-in for processor.outliers.Reading."""
    virtual_patient_id: str
    event_time: datetime
    cgm_mgdl: float | None
    heart_rate_bpm: int | None
    systolic_bp_mmhg: int | None
    diastolic_bp_mmhg: int | None
    spo2_pct: float | None
    sampling_sec: int
    source_trace_ref: str = ""
    device_id: str = ""
    ingested_at: datetime | None = None


def _mk_reading(vp: str, t: datetime, cgm: float, sampling_sec: int = 300) -> FakeReading:
    return FakeReading(
        virtual_patient_id=vp, event_time=t, cgm_mgdl=cgm,
        heart_rate_bpm=70, systolic_bp_mmhg=118, diastolic_bp_mmhg=76,
        spo2_pct=97.0, sampling_sec=sampling_sec, ingested_at=t,
    )


# ============================================================================
# Feature/label alignment on a known function
# ============================================================================

def test_feature_label_alignment():
    """Known-function stream. For each prediction we scored at input_time t, the
    recorded `actual` must equal g(t + HORIZON_MIN) within numerical slack.
    Off-by-one failures hide here.
    """
    vp = "vp:synthetic"
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    sampling_sec = 300  # 5-min cadence

    # g(t_min) = 120 + 30 * sin(2*pi * t_min / 60)  — slow-moving, continuous,
    # unique values across the test window so an off-by-one is obvious.
    def g(t_min: float) -> float:
        return 120.0 + 30.0 * math.sin(2 * math.pi * t_min / 60.0)

    pipe = MlPipeline()
    pipe.set_context(vp, source_tz="UTC", sampling_sec=sampling_sec,
                     diabetes_type="T1", speed_factor=1.0)

    # Stream 90 minutes of readings (enough to resolve several 15-min horizons).
    for i in range(19):
        t_min = 5 * i
        ts = base + timedelta(minutes=t_min)
        r = _mk_reading(vp, ts, g(t_min), sampling_sec)
        pipe.process_reading(r, alert_rule_ids_this_reading=set())

    # Drain writes.
    inserts, resolutions = pipe.drain_writes()

    # Resolutions carry (actual, abs_err, vp, source_event_time, model_name).
    # Reconstruct: for each resolved prediction, source_event_time + HORIZON_MIN
    # should == target_event_time, and actual == g(target_event_time_minute).
    resolved_rows = [(src_evt, actual) for (actual, _err, _vp, src_evt, _mn) in resolutions]
    # Deduplicate by source_event_time (one entry per source — all 4 models
    # share the same actual).
    seen = {}
    for src, actual in resolved_rows:
        seen[src] = actual

    assert len(seen) >= 5, f"expected several resolved predictions, got {len(seen)}"

    for src_evt, actual_recorded in seen.items():
        src_t_min = (src_evt - base).total_seconds() / 60.0
        expected_target_t_min = src_t_min + HORIZON_MIN
        expected_actual = g(expected_target_t_min)
        # Allow a hair of float error. The actual comes directly from
        # r.cgm_mgdl which we fed as g(target_t_min) — so this is essentially
        # an exact equality.
        assert abs(actual_recorded - expected_actual) < 1e-9, (
            f"alignment broken at src_t={src_t_min:.1f}min: "
            f"recorded actual={actual_recorded}, "
            f"expected g({expected_target_t_min})={expected_actual}"
        )

    # Sanity: for an off-by-one, actual would equal g(target ± 5min). Prove it
    # does NOT by checking that g(target_t_min) differs from g(target_t_min ± 5).
    for src_evt, actual_recorded in seen.items():
        src_t_min = (src_evt - base).total_seconds() / 60.0
        wrong_by_one_forward  = g(src_t_min + HORIZON_MIN + 5)
        wrong_by_one_backward = g(src_t_min + HORIZON_MIN - 5)
        assert abs(actual_recorded - wrong_by_one_forward)  > 1e-6, "fn too smooth"
        assert abs(actual_recorded - wrong_by_one_backward) > 1e-6, "fn too smooth"


# ============================================================================
# Horizon is defined in SOURCE time, invariant to replay speed
# ============================================================================

@pytest.mark.parametrize("speed", [1.0, 10.0, 100.0])
def test_horizon_is_source_time_regardless_of_speed(speed):
    """At any replay speed, the gap between the prediction's input event_time
    and the label's event_time must equal (HORIZON_MIN / speed) in event-time
    seconds — i.e. exactly 15 SOURCE minutes."""
    vp = f"vp:speed_{int(speed)}"
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    sampling_sec = 300
    # Event-time delta between consecutive readings under this speed.
    event_dt_sec = sampling_sec / speed  # at 10x this is 30s event-time per 5-min source
    # How many readings between input and label at this speed?
    # HORIZON_SEC source == HORIZON_SEC/speed event-time == N * event_dt_sec event-time.
    # N = HORIZON_SEC/speed / event_dt_sec = HORIZON_SEC/speed * speed/sampling_sec = HORIZON_SEC/sampling_sec = 3 for AZ.
    n_steps_to_label = HORIZON_SEC // sampling_sec  # 3 readings
    # Feed enough readings to resolve the first prediction, and some extras.
    n_feed = n_steps_to_label + 5

    def g(i: int) -> float:
        return 100.0 + i  # unique values, strictly increasing — perfect for the diff check

    pipe = MlPipeline()
    pipe.set_context(vp, source_tz="UTC", sampling_sec=sampling_sec,
                     diabetes_type="T1", speed_factor=speed)

    for i in range(n_feed):
        ts = base + timedelta(seconds=i * event_dt_sec)
        r = _mk_reading(vp, ts, g(i), sampling_sec)
        pipe.process_reading(r, alert_rule_ids_this_reading=set())

    _, resolutions = pipe.drain_writes()
    assert resolutions, f"no resolutions at speed={speed}"

    # For each resolved prediction, actual must equal g(input_index + N).
    # Pick one and verify the SOURCE-time gap. actual == g(input_index + 3)
    # means source gap is 3 * sampling_sec = 15 source minutes.
    seen_source_idx: dict[datetime, float] = {}
    for (actual, _err, _vp, src_evt, _mn) in resolutions:
        seen_source_idx.setdefault(src_evt, actual)
    for src_evt, actual in seen_source_idx.items():
        input_i = round((src_evt - base).total_seconds() / event_dt_sec)
        expected_actual = g(input_i + n_steps_to_label)
        assert abs(actual - expected_actual) < 1e-9, (
            f"speed={speed}: input_i={input_i}, actual={actual}, "
            f"expected g({input_i + n_steps_to_label})={expected_actual} "
            f"(wrong by {actual - expected_actual})"
        )
        # And prove the gap is exactly 15 source minutes.
        source_gap_sec = n_steps_to_label * sampling_sec
        assert source_gap_sec == HORIZON_SEC, (
            f"test bookkeeping broken: {source_gap_sec}s != {HORIZON_SEC}s"
        )


# ============================================================================
# Slope is in SOURCE-min units at every replay speed
# ============================================================================
# Fourth event-time / source-time mixup caught: _slope was reporting values
# `speed²` too large under N× replay because the x-axis conversion was in the
# wrong direction. At speed=10 a true physiologic 2 mg/dL/source-min slope was
# arriving at LinearTrendModel as 200 mg/dL/source-min; the ±60 clip then
# saturated on every non-zero slope. The feature's own docstring/column name
# ("mg/dL per SOURCE minute") is the contract this test enforces at speeds
# 1, 10 and 100.

@pytest.mark.parametrize("speed", [1.0, 10.0, 100.0])
def test_slope_is_in_source_minute_units_at_any_speed(speed):
    """Construct a trace with glucose rising a known 2 mg/dL per SOURCE
    minute at the given replay speed. The slope feature must come back as
    2.0 mg/dL per source-min — invariant to replay speed, since the feature
    is defined in source time.
    """
    from processor.ml import MlPipeline
    vp = f"vp:slope_{int(speed)}"
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    sampling_sec = 300                            # 5-min source cadence
    event_dt_sec = sampling_sec / speed           # 300/speed seconds of event-time per source sample
    TRUE_SLOPE   = 2.0                            # mg/dL per source minute — ground truth
    cgm_at_step  = lambda i: 100.0 + TRUE_SLOPE * (i * sampling_sec / 60.0)

    # Prime enough source-samples that the slope window (SLOPE_WINDOW_MIN
    # source-min) is comfortably full. At source cadence of 5 min, 10 samples
    # covers 45 source-min of history — more than SLOPE_WINDOW_MIN=30.
    n_prime = 10

    pipe = MlPipeline()
    pipe.set_context(vp, source_tz="UTC", sampling_sec=sampling_sec,
                     diabetes_type="T1", speed_factor=speed)
    s = pipe.state(vp)

    # Seed the per-vp buffer directly so we test _slope's math in isolation
    # from the rest of process_reading's bookkeeping.
    for i in range(n_prime):
        s.recent.append((base + timedelta(seconds=i * event_dt_sec), cgm_at_step(i)))

    # Now ask _build_features to compute slope_15min for the (n_prime+1)-th
    # reading. This is the exact path linear_trend consumes.
    r = _mk_reading(
        vp,
        base + timedelta(seconds=n_prime * event_dt_sec),
        cgm_at_step(n_prime),
        sampling_sec,
    )
    feat = pipe._build_features(r, s)
    observed = feat["slope_15min"]
    assert observed == pytest.approx(TRUE_SLOPE, rel=1e-9, abs=1e-9), (
        f"speed={speed}: slope_15min = {observed!r} mg/dL per source-min, "
        f"expected {TRUE_SLOPE}. ratio observed/expected = "
        f"{observed/TRUE_SLOPE:.3g}x (= speed^2 if the x-axis conversion is inverted)."
    )


# ============================================================================
# hour_sin/hour_cos reflect SOURCE-patient local hour at any replay speed
# ============================================================================
# Fifth event-time / source-time mixup — the diurnal feature was taking
# `r.event_time.astimezone(tz).hour` directly, so under N× replay a vp
# cycled through 24 "source hours" in 24/N wall-hours. River models learn
# a scrambled time-of-day signal unless we convert back to source time.

@pytest.mark.parametrize("speed", [1.0, 10.0, 100.0])
def test_hour_feature_tracks_source_time_at_any_speed(speed):
    """At any replay speed, hour_sin and hour_cos must correspond to the
    source patient's local hour at the moment the reading was generated
    (source time), NOT the wall-clock-advanced event hour."""
    from processor.ml import MlPipeline
    vp = f"vp:hour_{int(speed)}"
    # Source trace starts at 03:00:00 UTC in the source patient's history.
    source_start = datetime(2020, 1, 1, 3, 0, 0, tzinfo=timezone.utc)
    # Replay was fired up at 06:00:00 UTC today (event-time clock).
    event_start  = datetime(2026, 10, 5, 6, 0, 0, tzinfo=timezone.utc)
    sampling_sec = 300
    event_dt_sec = sampling_sec / speed       # event-sec between consecutive source samples
    pipe = MlPipeline()
    pipe.set_context(vp, source_tz="UTC", sampling_sec=sampling_sec,
                     diabetes_type="T1", speed_factor=speed,
                     source_trace_start_utc=source_start,
                     time_offset_seconds=0,
                     first_event_time=event_start)
    s = pipe.state(vp)
    # Prime a few readings so the feature builder's lag/slope paths are happy.
    for i in range(3):
        s.recent.append((event_start + timedelta(seconds=i * event_dt_sec), 100.0 + i))
    # Score the fourth reading, 3 source-samples in = 3 × sampling_sec source-sec elapsed.
    # So at speed=10, event_elapsed = 3 × 30 = 90 s event; source_elapsed = 900 s = 15 min source.
    # Expected source_time = source_start + source_elapsed = source_start + i * sampling_sec.
    i_fourth = 3
    r = _mk_reading(
        vp,
        event_start + timedelta(seconds=i_fourth * event_dt_sec),
        103.0, sampling_sec,
    )
    feat = pipe._build_features(r, s)
    expected_source_t = source_start + timedelta(seconds=i_fourth * sampling_sec)
    expected_hour     = expected_source_t.hour + expected_source_t.minute / 60.0
    expected_sin      = math.sin(2 * math.pi * expected_hour / 24.0)
    expected_cos      = math.cos(2 * math.pi * expected_hour / 24.0)
    assert feat["hour_sin"] == pytest.approx(expected_sin, abs=1e-9), (
        f"speed={speed}: hour_sin = {feat['hour_sin']!r}, expected {expected_sin!r} "
        f"(source hour {expected_hour:.3f})"
    )
    assert feat["hour_cos"] == pytest.approx(expected_cos, abs=1e-9)
    # And prove the OLD (buggy) code would have produced a different value by
    # using the event-time hour directly — at speed=10 those are many hours
    # apart.
    if speed > 1.0:
        buggy_hour = r.event_time.hour + r.event_time.minute / 60.0
        buggy_sin  = math.sin(2 * math.pi * buggy_hour / 24.0)
        assert abs(feat["hour_sin"] - buggy_sin) > 1e-3, (
            f"speed={speed}: hour_sin matches event-time hour ({buggy_hour:.3f}) "
            f"— the source-time conversion is not being applied"
        )


# ============================================================================
# Untrustworthy readings are excluded from learning AND cannot be labels
# ============================================================================

def test_untrustworthy_reading_evicts_pending_label():
    """A reading flagged by a sensor-fault rule must not become a label for
    any pending prediction whose target_time matches. The pending must evict
    as unresolved."""
    vp = "vp:synthetic_evict"
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    pipe = MlPipeline()
    pipe.set_context(vp, "UTC", 300, "T1")

    # Three clean readings, then one that would have been the label but is
    # flagged as a stuck-sensor fault.
    pipe.process_reading(_mk_reading(vp, base, 100), set())
    pipe.process_reading(_mk_reading(vp, base + timedelta(minutes=5),  110), set())
    pipe.process_reading(_mk_reading(vp, base + timedelta(minutes=10), 115), set())
    # This one is exactly 15 min after the first — would be the first
    # prediction's label — but we flag it as a sensor fault.
    pipe.process_reading(
        _mk_reading(vp, base + timedelta(minutes=15), 999),
        alert_rule_ids_this_reading={"rules.cgm_roc_spike"},
    )

    _ins, resolutions = pipe.drain_writes()
    # The 15-min-ahead label for the t=0 prediction MUST NOT have resolved.
    first_pred_source = base
    for (actual, _err, _vp, src_evt, _mn) in resolutions:
        assert src_evt != first_pred_source, (
            "an untrustworthy reading was accepted as a label (leakage via sensor fault)"
        )
    assert pipe.n_excluded_untrustworthy_total >= 1


def test_untrustworthy_exclusion_rules_cover_known_faults():
    """Sanity: the exclusion set contains the rule_ids actually produced by
    the detector for sensor-fault classes we care about."""
    expected = {
        "rules.cgm_null",
        "rules.cgm_out_of_range",
        "rules.cgm_stuck",
        "rules.cgm_roc_spike",
        "batch_analysis.firmware_offset",
    }
    assert expected <= UNTRUSTWORTHY_RULE_IDS


# ============================================================================
# Clarke Error Grid spot checks
# ============================================================================

def test_clarke_grid_zones():
    # Perfect agreement -> A
    assert clarke_error_grid_zone(100, 100) == "A"
    # Both below hypo threshold -> A (per Clarke 1987)
    assert clarke_error_grid_zone(55, 60) == "A"
    # 20% error -> A (boundary)
    assert clarke_error_grid_zone(100, 119) == "A"
    assert clarke_error_grid_zone(100, 125) in ("B",)  # just outside A
    # Dangerous inversion: real hypo, predict hyper -> E
    assert clarke_error_grid_zone(50, 200) == "E"
    # Dangerous inversion: real hyper, predict hypo -> E
    assert clarke_error_grid_zone(250, 60) == "E"
    # Failure to detect hypo (predict normal) -> D
    assert clarke_error_grid_zone(60, 120) == "D"
    # Failure to detect hyper (predict normal) -> D
    assert clarke_error_grid_zone(260, 120) == "D"

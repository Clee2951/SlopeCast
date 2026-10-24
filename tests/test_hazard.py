"""Section 04 tests. A fake Claude client is used; no test calls the real API.

Covers dry/stable, wet, moving, stale stakes, timing fits, and every check applied to
Claude's answer (unknown zones, invented timing, risk below the rule floor, API failure)."""
import math
from datetime import timedelta

from backend.hazard import HazardEngine, inverse_velocity_fit
from backend.weather import build_snapshot
from tests.conftest import T0, calm_features, features

FIT_RULES = {"min_points": 8, "min_r2": 0.8, "min_velocity_deg_per_h": 2.0, "window_s": 900}


def weather(rain24=5, forecast24=5, stale=False):
    pairs = [(T0 - timedelta(hours=23 - i), rain24 / 24) for i in range(24)]
    pairs += [(T0 + timedelta(hours=1 + i), forecast24 / 24) for i in range(24)]
    s = build_snapshot("liugui-demo", pairs, T0, "test", {})
    s.stale = stale
    return s


def likelihoods(a):
    return {z.zone_id: z.likelihood for z in a.zone_impacts}


def engine(settings, claude=None):
    return HazardEngine(settings, call_claude=claude)


def test_dry_and_stable_is_monitoring(settings, ds):
    a = engine(settings).assess(calm_features(), {}, weather(), ds.zones, T0)
    assert a.risk_level == "monitoring"
    assert set(likelihoods(a).values()) == {"low"}
    assert a.timing_status == "unavailable"
    assert a.engine == "rules-only"


def test_wet_soil_and_heavy_rain_is_elevated(settings, ds):
    f = calm_features()
    f["N02"] = features("N02", wet=0.85)
    a = engine(settings).assess(f, {}, weather(rain24=120, forecast24=200), ds.zones, T0)
    assert a.risk_level == "elevated"
    assert likelihoods(a)["Z2"] == "medium"


def test_moving_stake_marks_zone_below_it_high(settings, ds):
    f = calm_features()
    f["N01"] = features("N01", wet=0.9, tilt=5.0, rate=30.0)
    a = engine(settings).assess(f, {}, weather(), ds.zones, T0)
    lk = likelihoods(a)
    assert lk["Z1"] == "high"
    assert lk["Z3"] == "low"                           # nothing moving above Z3
    assert lk["Z4"] == "medium"                        # needs two moving stakes for high
    assert a.risk_level == "urgent_review"
    z1 = next(z for z in a.zone_impacts if z.zone_id == "Z1")
    assert z1.evidence_nodes == ["N01"]


def test_two_moving_stakes_raise_the_combined_zone(settings, ds):
    f = calm_features()
    f["N01"] = features("N01", wet=0.9, tilt=5.0, rate=30.0)
    f["N02"] = features("N02", wet=0.9, tilt=5.0, rate=30.0)
    a = engine(settings).assess(f, {}, weather(), ds.zones, T0)
    assert likelihoods(a)["Z4"] == "high"


def test_stale_stake_is_a_gap_not_safety(settings, ds):
    f = calm_features()
    f["N03"] = features("N03", quality="degraded", age=120)
    a = engine(settings).assess(f, {}, weather(), ds.zones, T0)
    assert any(g.startswith("N03") for g in a.coverage_gaps)
    z3 = next(z for z in a.zone_impacts if z.zone_id == "Z3")
    assert any("not safety" in r for r in z3.reasons)
    assert a.quality == "degraded"


def test_stale_weather_is_a_gap(settings, ds):
    a = engine(settings).assess(calm_features(), {}, weather(stale=True), ds.zones, T0)
    assert any(g.startswith("weather") for g in a.coverage_gaps)


def accelerating(now, tf_min=30.0, A=3.0, n=16):
    """Tilt series following v = A / (tf - t) deg per minute, one point per minute."""
    pts = []
    for i in range(n):
        t = now - timedelta(minutes=n - 1 - i)
        m = -(n - 1 - i)
        pts.append((t, A * math.log(tf_min / (tf_min - m)) if m > -tf_min else 0))
    return pts


def test_inverse_velocity_fit_on_accelerating_tilt():
    fit = inverse_velocity_fit(accelerating(T0), T0, FIT_RULES)
    assert fit is not None and fit["r2"] > 0.95
    from datetime import datetime
    predicted = datetime.fromisoformat(fit["predicted_failure_utc"])
    assert abs((predicted - (T0 + timedelta(minutes=30))).total_seconds()) < 180   # true failure in 30 min
    assert fit["earliest_utc"] < fit["predicted_failure_utc"] < fit["latest_utc"]


def test_no_fit_for_steady_or_still_tilt():
    steady = [(T0 - timedelta(minutes=15 - i), 0.1 * i) for i in range(16)]       # constant 6 deg/h
    still = [(T0 - timedelta(minutes=15 - i), 0.0) for i in range(16)]
    assert inverse_velocity_fit(steady, T0, FIT_RULES) is None
    assert inverse_velocity_fit(still, T0, FIT_RULES) is None


def valid_answer(**over):
    ans = {"risk_level": "urgent_review",
           "zones": [{"zone_id": "Z1", "likelihood": "high", "evidence_nodes": ["N01"], "reasons": ["N01 tilting"]}],
           "coverage_gaps": [], "timing": {"status": "unavailable"}, "summary": "Z1 is likely to be hit."}
    ans.update(over)
    return ans


def moving_features():
    f = calm_features()
    f["N01"] = features("N01", wet=0.9, tilt=5.0, rate=30.0)
    return f


def test_claude_answer_used_when_valid(settings, ds):
    calls = []
    a = engine(settings, lambda system, ev: calls.append(ev) or valid_answer()).assess(moving_features(), {}, weather(), ds.zones, T0)
    assert a.engine.startswith("claude:")
    assert a.summary == "Z1 is likely to be hit."
    assert calls and {z["zone_id"] for z in calls[0]["zones"]} == set(ds.zones)
    assert calls[0]["rules"]["minimum_risk_level"] == "urgent_review"


def test_unknown_zone_is_dropped(settings, ds):
    ans = valid_answer(zones=[{"zone_id": "Z99", "likelihood": "high", "evidence_nodes": ["N01"], "reasons": ["x"]}])
    a = engine(settings, lambda s, e: ans).assess(moving_features(), {}, weather(), ds.zones, T0)
    assert "Z99" not in likelihoods(a)
    assert likelihoods(a)["Z1"] == "high"              # kept from the rules
    assert any("Z99" in n for n in a.notes)


def test_invented_timing_is_removed(settings, ds):
    ans = valid_answer(timing={"status": "experimental", "earliest_utc": "2026-10-24T13:00:00+00:00", "latest_utc": None, "basis": "guess"})
    a = engine(settings, lambda s, e: ans).assess(moving_features(), {}, weather(), ds.zones, T0)
    assert a.timing_status == "unavailable" and a.timing_window is None
    assert any("removed timing" in n for n in a.notes)


def test_timing_kept_only_with_a_fit(settings, ds):
    a = engine(settings, lambda s, e: valid_answer()).assess(moving_features(), {"N01": accelerating(T0)}, weather(), ds.zones, T0)
    assert a.timing_status == "experimental"
    assert "inverse velocity" in a.timing_window["basis"]


def test_risk_level_never_below_rule_floor(settings, ds):
    a = engine(settings, lambda s, e: valid_answer(risk_level="monitoring")).assess(moving_features(), {}, weather(), ds.zones, T0)
    assert a.risk_level == "urgent_review"
    assert any("rule floor" in n for n in a.notes)


def test_api_failure_falls_back_to_rules(settings, ds):
    def boom(system, ev):
        raise TimeoutError("no network")
    a = engine(settings, boom).assess(moving_features(), {}, weather(), ds.zones, T0)
    assert a.engine == "rules-fallback"
    assert likelihoods(a)["Z1"] == "high"
    assert "no network" in a.notes[0]


def test_garbage_answer_falls_back(settings, ds):
    a = engine(settings, lambda s, e: {"risk_level": "doom"}).assess(moving_features(), {}, weather(), ds.zones, T0)
    assert a.engine == "rules-fallback"


def test_rate_limit_and_change_detection(settings, ds):
    e = engine(settings)
    assert e.maybe_assess(calm_features(), {}, weather(), ds.zones, T0) is not None
    assert e.maybe_assess(calm_features(), {}, weather(), ds.zones, T0 + timedelta(seconds=5)) is None
    assert e.maybe_assess(moving_features(), {}, weather(), ds.zones, T0 + timedelta(seconds=6)) is not None  # escalation passes

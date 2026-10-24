"""Section 01 tests: packet parsing, disconnects, duplicates, resets and calibration.

Run: uv run pytest tests/test_sensors.py
"""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.sensors import (
    Gateway, NodeCalibration, PacketError, compute_features, load_calibration, parse_line,
    tilt_from_baseline, downslope_tilt,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sensor_packets.jsonl"
T0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)


def reading_line(seq=1, boot=111, up_ms=None, moist=400, acc=(0.0, 0.0, 1.0), flags=(), model="ADXL345"):
    ax, ay, az = acc if acc else (None, None, None)
    return json.dumps({
        "v": 1, "type": "reading", "node": "N01", "boot": boot, "seq": seq,
        "up_ms": up_ms if up_ms is not None else seq * 1000, "moist_raw": moist,
        "moist_min": moist - 3, "moist_max": moist + 3, "ax": ax, "ay": ay, "az": az,
        "acc_units": "g", "acc_model": model if acc else "none", "acc_n": 20 if acc else 0,
        "fw": "1.0.0", "flags": list(flags),
    })


def feed(gw, lines, start=T0, step_s=1.0, latency_s=0.0):
    out = []
    for i, line in enumerate(lines):
        out.append(gw.ingest_line(line, start + timedelta(seconds=i * step_s + latency_s)))
    return out


def cal(**kw):
    base = dict(node_id="N01", version="test-1", dry_raw=300, wet_raw=700,
                baseline_g=(0.0, 0.0, 1.0), downslope_axis=(1.0, 0.0, 0.0), accel_model="ADXL345")
    base.update(kw)
    return NodeCalibration(**base)


# ---------------------------------------------------------------- parsing
def test_parses_valid_reading():
    obj = parse_line(reading_line(seq=7))
    assert obj["seq"] == 7 and obj["ax"] == 0.0


@pytest.mark.parametrize("line, why", [
    ("", "empty"),
    ("ADXL3 init...", "noise"),
    ('{"v":1,"type":"reading","node":"N01","boot":1,"seq":4,"up_ms":42', "truncated"),
    ('[1,2,3]', "not object"),
    (reading_line().replace('"v": 1', '"v": 2'), "protocol"),
    (reading_line().replace('"seq": 1', '"seq": 0'), "seq"),
    (reading_line(moist=400).replace('"moist_raw": 400', '"moist_raw": 2000'), "adc range"),
    (reading_line().replace('"ay": 0.0', '"ay": null'), "partial vector"),
    (reading_line().replace('"acc_units": "g"', '"acc_units": "m/s2"'), "units"),
    (reading_line().replace('"seq": 1', '"seq": "1"'), "type"),
])
def test_rejects_bad_lines(line, why):
    with pytest.raises(PacketError):
        parse_line(line)


def test_missing_accel_is_null_not_zero():
    gw = Gateway()
    res = gw.ingest_line(reading_line(acc=None, flags=["accel_missing"]), T0)
    assert res.kind == "reading"
    assert res.reading.accel_g is None
    assert "accel_missing" in res.reading.health_flags


# ---------------------------------------------------------------- fixture end to end
def test_fixture_counts():
    gw = Gateway({"N01": "site-demo"})
    results = feed(gw, FIXTURE.read_text().splitlines())
    kinds = [r.kind for r in results]
    assert kinds.count("hello") == 2
    assert kinds.count("duplicate") == 1
    assert kinds.count("invalid") == 2          # boot noise + truncated line
    readings = [r.reading for r in results if r.kind == "reading"]
    assert [(r.session_id.split(":")[1], r.seq) for r in readings] == [
        ("1804289383", 1), ("1804289383", 2), ("1804289383", 3), ("1804289383", 4),
        ("1804289383", 5), ("846930886", 1), ("846930886", 2)]
    assert all(r.site_id == "site-demo" for r in readings)
    assert gw.stats["new_session"] == 2


def test_unknown_node_gets_no_site():
    gw = Gateway({"N02": "elsewhere"})
    res = gw.ingest_line(reading_line(), T0)
    assert res.reading.site_id is None


# ---------------------------------------------------------------- duplicates, resets, reconnects
def test_duplicate_stored_once():
    gw = Gateway()
    res = feed(gw, [reading_line(1), reading_line(2), reading_line(2), reading_line(1)])
    assert [r.kind for r in res] == ["reading", "reading", "duplicate", "duplicate"]


def test_reset_starts_new_session_and_keeps_history():
    gw = Gateway()
    res = feed(gw, [reading_line(1, boot=111), reading_line(2, boot=111),
                    reading_line(1, boot=222, up_ms=1000)])
    assert [r.kind for r in res] == ["reading", "reading", "reading"]
    assert res[0].reading.session_id != res[2].reading.session_id
    # the restarted node's seq 1 is not mistaken for a duplicate, and times keep moving forward
    assert res[2].reading.sample_time_utc > res[1].reading.sample_time_utc


def test_reconnect_without_reset_continues_session():
    gw = Gateway()
    a = feed(gw, [reading_line(1), reading_line(2)])
    # laptop unplugged for 30 s; the Uno kept running and counting (R4 does not reset on open)
    b = feed(gw, [reading_line(33)], start=T0 + timedelta(seconds=32))
    assert b[0].kind == "reading"
    assert b[0].reading.session_id == a[0].reading.session_id


def test_sample_time_uses_minimum_latency():
    gw = Gateway()
    slow = gw.ingest_line(reading_line(1, up_ms=1000), T0 + timedelta(seconds=0.5))   # 0.5 s late
    fast = gw.ingest_line(reading_line(2, up_ms=2000), T0 + timedelta(seconds=1.05))  # 0.05 s late
    # the anchor moves to the faster delivery, so later sample times are corrected
    assert abs((fast.reading.sample_time_utc - (T0 + timedelta(seconds=1.05))).total_seconds()) < 1e-6
    assert slow.reading.receipt_time_utc == T0 + timedelta(seconds=0.5)


def test_naive_receipt_time_rejected():
    with pytest.raises(ValueError):
        Gateway().ingest_line(reading_line(), datetime(2026, 10, 24, 12, 0, 0))


# ---------------------------------------------------------------- calibration and features
def test_calibration_file_uncalibrated_values_stay_none(tmp_path):
    p = tmp_path / "calibration.json"
    p.write_text(json.dumps({"version": "uncalibrated", "nodes": {"N01": {
        "accel_model": None, "moisture": {"dry_raw": None, "wet_raw": None},
        "tilt": {"baseline_g": None, "downslope_axis": None}}}}))
    c = load_calibration(p)["N01"]
    assert c.dry_raw is None and c.baseline_g is None


def test_repo_calibration_file_loads():
    path = Path(__file__).parents[1] / "config" / "calibration.json"
    assert "N01" in load_calibration(path)


def test_uncalibrated_features_are_degraded_not_zero():
    gw = Gateway()
    rs = [r.reading for r in feed(gw, [reading_line(i) for i in range(1, 11)])]
    f = compute_features(rs, cal(dry_raw=None, wet_raw=None, baseline_g=None), "N01", T0 + timedelta(seconds=10))
    assert f.wetness_index is None and f.tilt_deg is None
    assert f.quality == "degraded"
    assert "moisture_uncalibrated" in f.quality_reasons
    assert "tilt_baseline_missing" in f.quality_reasons


def test_wetness_index_from_dry_and_wet():
    gw = Gateway()
    rs = [r.reading for r in feed(gw, [reading_line(i, moist=500) for i in range(1, 11)])]
    f = compute_features(rs, cal(), "N01", T0 + timedelta(seconds=10))
    assert f.wetness_index == pytest.approx(0.5)
    assert f.quality == "ok"


def test_rising_wetness_trend():
    gw = Gateway()
    lines = [reading_line(i, moist=300 + 4 * i) for i in range(1, 121)]   # +4 counts per second
    rs = [r.reading for r in feed(gw, lines)]
    f = compute_features(rs, cal(), "N01", T0 + timedelta(seconds=120))
    # 4 counts/s over a 400-count range = 0.01 per s = 36 per hour
    assert f.wetness_trend_per_h == pytest.approx(36.0, rel=0.01)


def test_tilt_from_baseline_and_downslope_sign():
    lean = math.radians(5)
    g = (math.sin(lean), 0.0, math.cos(lean))
    assert tilt_from_baseline(g, (0, 0, 1)) == pytest.approx(5.0)
    assert downslope_tilt(g, (0, 0, 1), (1, 0, 0)) == pytest.approx(5.0)
    assert downslope_tilt(g, (0, 0, 1), (-1, 0, 0)) == pytest.approx(-5.0)


def test_tilt_works_for_upright_mounting():
    # stake holds the board upright: gravity along -y at install
    lean = math.radians(3)
    g = (0.0, -math.cos(lean), math.sin(lean))
    assert tilt_from_baseline(g, (0, -1, 0)) == pytest.approx(3.0)


def test_steady_tilt_trend():
    gw = Gateway()
    lines = []
    for i in range(1, 181):                                 # 0.1 deg per minute
        a = math.radians(0.1 * i / 60)
        lines.append(reading_line(i, acc=(math.sin(a), 0.0, math.cos(a))))
    rs = [r.reading for r in feed(gw, lines)]
    f = compute_features(rs, cal(), "N01", T0 + timedelta(seconds=180))
    assert f.tilt_trend_deg_per_h == pytest.approx(6.0, rel=0.02)
    assert f.tilt_downslope_deg > 0


def test_flagged_readings_excluded_and_degrade():
    gw = Gateway()
    lines = [reading_line(i, moist=500) for i in range(1, 9)]
    lines += [reading_line(9, moist=900, flags=["moisture_unstable"]),
              reading_line(10, moist=500, acc=None, flags=["accel_read_failed"])]
    rs = [r.reading for r in feed(gw, lines)]
    f = compute_features(rs, cal(), "N01", T0 + timedelta(seconds=10))
    assert f.wetness_index == pytest.approx(0.5)             # the 900 spike was ignored
    assert f.tilt_deg is not None
    assert f.quality == "degraded"


def test_stale_data_is_not_safe():
    gw = Gateway()
    rs = [r.reading for r in feed(gw, [reading_line(i) for i in range(1, 11)])]
    f = compute_features(rs, cal(), "N01", T0 + timedelta(seconds=10 + 120))
    assert f.quality == "unavailable"
    assert "stale_data" in f.quality_reasons


def test_no_readings_unavailable():
    f = compute_features([], cal(), "N01", T0)
    assert f.quality == "unavailable" and f.reading_count == 0


def test_accel_swap_invalidates_baseline():
    gw = Gateway()
    rs = [r.reading for r in feed(gw, [reading_line(i, model="LIS3DHTR") for i in range(1, 11)])]
    f = compute_features(rs, cal(), "N01", T0 + timedelta(seconds=10))
    assert f.tilt_deg is None
    assert "accel_model_differs_from_calibration" in f.quality_reasons

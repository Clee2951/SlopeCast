"""Section 02 tests: response parsing, rainfall totals, missing hours, observed vs forecast,
threshold crossings, and the source fallback chain (Apify -> Open-Meteo -> cache -> file)."""
import json
from datetime import timedelta
from pathlib import Path

from backend.weather import (WeatherService, build_snapshot, normalize, snapshot_from_scenario,
                             threshold_crossing)
from tests.conftest import T0

POLICY = {"threshold": {"name": "test", "a": 14.82, "b": -0.39, "min_duration_h": 1, "max_duration_h": 72}}


def hours(start, values):
    return [(start + timedelta(hours=i), v) for i, v in enumerate(values)]


def test_normalize_open_meteo_shape():
    items = {"utc_offset_seconds": 0, "hourly": {"time": ["2026-10-24T11:00", "2026-10-24T12:00"], "precipitation": [1.5, None]}}
    pairs = normalize(items)
    assert pairs[0] == (T0 - timedelta(hours=1), 1.5)
    assert pairs[1] == (T0, None)                      # missing stays missing


def test_normalize_row_shape_with_offset():
    rows = [{"time": "2026-10-24T20:00", "precipitation_mm": "4.2", "utc_offset_seconds": 8 * 3600},
            {"datetime": "2026-10-24T13:00:00Z", "rain": 0},
            {"something": "irrelevant"}]
    pairs = normalize(rows)
    assert (T0, 4.2) in pairs                          # 20:00 at UTC+8 is 12:00 UTC
    assert (T0 + timedelta(hours=1), 0.0) in pairs
    assert len(pairs) == 2


def test_totals_and_labels():
    pairs = hours(T0 - timedelta(hours=23), [1.0] * 24) + hours(T0 + timedelta(hours=1), [2.0] * 24)
    snap = build_snapshot("s", pairs, T0, "test", POLICY)
    assert snap.rain_1h_mm == 1.0
    assert snap.rain_6h_mm == 6.0
    assert snap.rain_24h_mm == 24.0
    assert snap.forecast_24h_mm == 48.0
    assert snap.rain_72h_mm is None                    # only 24 h of history: unknown, not small
    kinds = {h["kind"] for h in snap.hours}
    assert kinds == {"observed", "forecast"}
    assert all(h["kind"] == "observed" for h in snap.hours if h["time_utc"] <= T0.isoformat())


def test_missing_hours_make_totals_unknown():
    vals = [1.0] * 24
    vals[20] = None
    vals[21] = None
    snap = build_snapshot("s", hours(T0 - timedelta(hours=23), vals), T0, "test", POLICY)
    assert snap.rain_6h_mm is None                     # two hours missing in the last six
    assert snap.rain_24h_mm is None                    # allow_missing=1 for 24 h


def test_threshold_crossing_time():
    # dry until T0, then 30 mm/h: crosses 14.82 * 1^-0.39 in the first forecast hour
    pairs = hours(T0 - timedelta(hours=5), [0] * 6) + hours(T0 + timedelta(hours=1), [30, 30, 30])
    assert threshold_crossing(pairs, T0, 14.82, -0.39) == T0 + timedelta(hours=1)
    light = hours(T0 - timedelta(hours=5), [0] * 6) + hours(T0 + timedelta(hours=1), [1, 1, 1])
    assert threshold_crossing(light, T0, 14.82, -0.39) is None


def test_scenario_snapshot_is_simulated():
    snap = snapshot_from_scenario("s", [1, 2, 3, 4], T0 - timedelta(hours=2), T0, POLICY)
    assert snap.simulated and snap.rain_1h_mm == 2.0 and snap.forecast_24h_mm is None


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, body=None, headers=None, timeout=None):
        self.calls.append(url)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_apify_used_when_configured(settings):
    settings.apify_token = "t"
    settings.rainfall = dict(settings.rainfall, apify_actor_id="owner~weather")
    http = FakeHttp([[{"time": (T0 - timedelta(hours=1)).isoformat(), "precipitation": 3}]])
    snap = WeatherService(settings, http).fetch(T0)
    assert snap.source == "apify:owner~weather"
    assert "api.apify.com/v2/actors/owner~weather/run-sync-get-dataset-items" in http.calls[0]


def test_falls_back_to_open_meteo(settings):
    settings.apify_token = "t"
    settings.rainfall = dict(settings.rainfall, apify_actor_id="owner~weather")
    om = {"hourly": {"time": [(T0 - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")], "precipitation": [5.0]}}
    snap = WeatherService(settings, FakeHttp([OSError("apify down"), om])).fetch(T0)
    assert snap.source.startswith("open-meteo-direct")
    assert "apify down" in snap.error


def test_outage_returns_stale_cache_not_dry_weather(settings):
    om = {"hourly": {"time": [(T0 - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")], "precipitation": [5.0]}}
    svc = WeatherService(settings, FakeHttp([om, OSError("down")]))
    svc.fetch(T0)
    later = svc.fetch(T0 + timedelta(minutes=30))
    assert later.stale is True
    assert later.age_s == 1800
    assert "(cached)" in later.source


def test_no_source_uses_labelled_offline_file(settings):
    snap = WeatherService(settings, FakeHttp([OSError("down")])).fetch(T0)
    assert snap.stale and snap.source.startswith("offline-file")
    assert snap.rain_24h_mm is not None


def test_fixture_is_labelled():
    data = json.loads((Path(__file__).parent / "fixtures" / "weather.json").read_text())
    assert "synthetic" in data["provenance"]

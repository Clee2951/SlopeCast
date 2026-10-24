"""Section 02: rainfall for the site, from an Apify Actor.

fetch_snapshot() tries sources in order and always says which one it used:
  1. the Apify Actor in config/rainfall-policy.json (needs APIFY_TOKEN)
  2. fallback_source, Open-Meteo called directly (no key)
  3. the last good snapshot, marked stale
  4. the saved offline file, shifted to the current time and labelled as such

Values are converted to mm per hour and UTC, and every hour is labelled observed (at or
before now) or forecast. A missing hour stays missing; it is never counted as 0 mm.
"""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from backend.schemas import WeatherSnapshot

TIME_KEYS = ("time_utc", "time", "datetime", "date_time", "timestamp", "forecast_time", "hour", "date")
RAIN_KEYS = ("precipitation", "precipitation_mm", "rain", "rain_mm", "precip_mm", "precipitation (mm)", "precip")


class WeatherError(RuntimeError):
    pass


def _http_json(url: str, body: Optional[dict] = None, headers: Optional[dict] = None, timeout: float = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- parsing many response shapes
def _parse_time(v, utc_offset_s: int = 0) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, (int, float)):                      # unix seconds or ms
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
    s = str(v).strip().replace("Z", "+00:00")
    if len(s) == 16 and "T" in s:                        # "2026-10-24T13:00"
        s += ":00"
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone(timedelta(seconds=utc_offset_s)))
    return t.astimezone(timezone.utc)


def normalize(items, default_utc_offset_s: int = 0) -> list:
    """Turn an Actor's dataset items (or an Open-Meteo response) into [(utc_hour, mm or None)]."""
    if isinstance(items, dict):
        items = [items]
    out: dict = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        off = int(it.get("utc_offset_seconds", default_utc_offset_s) or 0)
        hourly = it.get("hourly")
        if isinstance(hourly, dict) and "time" in hourly:      # raw Open-Meteo shape
            rain = next((hourly[k] for k in RAIN_KEYS if k in hourly), None)
            if rain is None:
                continue
            for t, mm in zip(hourly["time"], rain):
                tt = _parse_time(t, off)
                if tt is not None:
                    out[tt] = None if mm is None else float(mm)
            continue
        tkey = next((k for k in TIME_KEYS if k in it), None)
        rkey = next((k for k in RAIN_KEYS if k in it), None)
        if tkey is None or rkey is None:
            continue
        tt = _parse_time(it[tkey], off)
        if tt is None:
            continue
        mm = it[rkey]
        try:
            out[tt] = None if mm in (None, "") else float(mm)
        except (TypeError, ValueError):
            out[tt] = None
    return sorted(out.items())


# ---------------------------------------------------------------- features
def _sum(hours: list, start: datetime, end: datetime, allow_missing: int = 0) -> Optional[float]:
    """Sum mm for hours with start < t <= end. None if more than allow_missing hours are absent."""
    expected = int(round((end - start).total_seconds() / 3600))
    vals = {t: mm for t, mm in hours if start < t <= end}
    present = [mm for mm in vals.values() if mm is not None]
    if expected - len(present) > allow_missing:
        return None
    return round(sum(present), 1)


def threshold_crossing(hours: list, now: datetime, a: float, b: float,
                       min_d: float = 1.0, max_d: float = 72.0) -> Optional[datetime]:
    """First hour end t > now where the mean intensity over some duration D ending at t
    reaches a * D^b (mm/h). Uses observed and forecast hours. This is a rainfall-threshold
    crossing, not a landslide time."""
    series = {t: mm for t, mm in hours}
    times = sorted(t for t in series if t > now)
    for t in times:
        total, d = 0.0, 0
        while d < max_d:
            v = series.get(t - timedelta(hours=d))
            if v is None:
                break
            total += v
            d += 1
            if d >= max(1, math.ceil(min_d)) and total / d >= a * d ** b:
                return t
    return None


def build_snapshot(site_id: str, pairs: list, now: datetime, source: str, policy: dict,
                   simulated: bool = False, error: Optional[str] = None, stale: bool = False) -> WeatherSnapshot:
    hour_now = now.replace(minute=0, second=0, microsecond=0)
    hours = [{"time_utc": t.isoformat(), "mm": mm, "kind": "observed" if t <= now else "forecast"} for t, mm in pairs]
    obs = [(t, mm) for t, mm in pairs if t <= now]
    fc = [(t, mm) for t, mm in pairs if t > now]
    th = policy.get("threshold", {})
    win = [mm for t, mm in pairs if hour_now - timedelta(hours=24) < t <= hour_now + timedelta(hours=24) and mm is not None]
    return WeatherSnapshot(
        snapshot_id=f"W-{now.strftime('%Y%m%dT%H%M%S')}",
        site_id=site_id,
        fetched_at_utc=now,
        source=source,
        hours=hours,
        rain_1h_mm=_sum(obs, hour_now - timedelta(hours=1), hour_now),
        rain_6h_mm=_sum(obs, hour_now - timedelta(hours=6), hour_now),
        rain_24h_mm=_sum(obs, hour_now - timedelta(hours=24), hour_now, allow_missing=1),
        rain_72h_mm=_sum(obs, hour_now - timedelta(hours=72), hour_now, allow_missing=3),
        forecast_24h_mm=_sum(fc, hour_now, hour_now + timedelta(hours=24), allow_missing=1),
        peak_mm_per_h=max(win) if win else None,
        threshold_crossing_utc=threshold_crossing(pairs, now, th.get("a", 14.82), th.get("b", -0.39),
                                                  th.get("min_duration_h", 1), th.get("max_duration_h", 72)) if th else None,
        threshold_name=th.get("name", "none") + (f" [{th['status']}]" if th.get("status") else ""),
        stale=stale,
        age_s=0.0,
        error=error,
        simulated=simulated,
    )


# ---------------------------------------------------------------- sources
def fetch_apify(lat: float, lon: float, policy: dict, token: str, http=_http_json) -> tuple:
    actor = policy.get("apify_actor_id", "")
    if not actor or actor.startswith("REPLACE"):
        raise WeatherError("no Apify Actor configured in config/rainfall-policy.json")
    body = json.loads(json.dumps(policy.get("apify_input", {})).replace("{lat}", str(lat)).replace("{lon}", str(lon)))
    url = (f"https://api.apify.com/v2/actors/{urllib.parse.quote(actor, safe='~')}/run-sync-get-dataset-items"
           f"?timeout={int(policy.get('timeout_s', 120))}")
    items = http(url, body, {"Authorization": f"Bearer {token}"}, policy.get("timeout_s", 120) + 15)
    pairs = normalize(items, int(policy.get("apify_utc_offset_s", 0)))
    if not pairs:
        raise WeatherError("Apify Actor returned no hourly precipitation rows (check the adapter in weather.normalize)")
    return pairs, f"apify:{actor}"


def fetch_open_meteo(lat: float, lon: float, http=_http_json) -> tuple:
    url = ("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
        {"latitude": lat, "longitude": lon, "hourly": "precipitation", "past_days": 3,
         "forecast_days": 2, "timezone": "UTC"}))
    pairs = normalize(http(url, None, None, 30))
    if not pairs:
        raise WeatherError("Open-Meteo returned no precipitation")
    return pairs, "open-meteo-direct (fallback)"


def load_offline(path: Path, now: datetime) -> tuple:
    """Saved series, shifted so its reference time lines up with now. Labelled as such."""
    data = json.loads(Path(path).read_text())
    pairs = normalize(data.get("response", data))
    ref = _parse_time(data.get("reference_now"))
    if ref is not None:
        shift = now.replace(minute=0, second=0, microsecond=0) - ref
        pairs = [(t + shift, mm) for t, mm in pairs]
    return pairs, f"offline-file {Path(path).name} (saved series re-timed to now: {data.get('provenance', 'see file')})"


class WeatherService:
    """Keeps the last good snapshot so an outage shows as stale data, not as dry weather."""

    def __init__(self, settings, http: Callable = _http_json):
        self.s = settings
        self.http = http
        self.last_good: Optional[WeatherSnapshot] = None

    def fetch(self, now: datetime) -> WeatherSnapshot:
        pol = self.s.rainfall
        lat, lon = self.s.site["center"]
        errors = []
        attempts = []
        if pol.get("source", "apify") == "apify":
            if self.s.apify_token:
                attempts.append(lambda: fetch_apify(lat, lon, pol, self.s.apify_token, self.http))
            else:
                errors.append("APIFY_TOKEN not set")
        if pol.get("fallback_source") == "open-meteo-direct" or pol.get("source") == "open-meteo-direct":
            attempts.append(lambda: fetch_open_meteo(lat, lon, self.http))
        for attempt in attempts:
            try:
                pairs, source = attempt()
                snap = build_snapshot(self.s.site_id, pairs, now, source, pol,
                                      error="; ".join(errors) or None)
                self.last_good = snap
                return snap
            except Exception as e:  # network, HTTP, parsing: record and try the next source
                errors.append(f"{type(e).__name__}: {e}"[:200])
        if self.last_good is not None:
            age = (now - self.last_good.fetched_at_utc).total_seconds()
            snap = build_snapshot(self.s.site_id, [(_parse_time(h["time_utc"]), h["mm"]) for h in self.last_good.hours],
                                  now, self.last_good.source + " (cached)", pol, error="; ".join(errors), stale=True)
            snap.age_s = age
            snap.fetched_at_utc = self.last_good.fetched_at_utc
            return snap
        off = pol.get("offline_file")
        if off and self.s.path(off).exists():
            pairs, source = load_offline(self.s.path(off), now)
            return build_snapshot(self.s.site_id, pairs, now, source, pol, error="; ".join(errors), stale=True)
        return build_snapshot(self.s.site_id, [], now, "none", pol, error="; ".join(errors) or "no source", stale=True)


def snapshot_from_scenario(site_id: str, hours_mm: list, start: datetime, now: datetime, policy: dict) -> WeatherSnapshot:
    """Replay helper: hours_mm[i] is the rainfall for the hour ending start + i hours."""
    pairs = [(start + timedelta(hours=i + 1), mm) for i, mm in enumerate(hours_mm)]
    return build_snapshot(site_id, pairs, now, "scenario file (simulated)", policy, simulated=True)

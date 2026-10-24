"""Section 04: hazard engine. Decides which pre-drawn landslide paths are likely to be hit.

Three layers:
  1. Code prepares the evidence: per-stake features, rainfall, an inverse-velocity timing
     fit when a stake is accelerating, and a rule-based view with a minimum risk level.
  2. Claude (Anthropic Messages API) weighs that evidence and answers through a forced
     tool call, choosing only from the zone list.
  3. Code checks the answer: unknown zones and nodes are dropped, timing without a valid
     fit is removed, coverage gaps are always included, the risk level never drops below
     the rule floor. If the call fails or the answer is unusable, the rule-based view is
     used and marked engine = "rules-fallback".

No messages are sent from here; pipeline.py decides what happens next.
"""
from __future__ import annotations

import hashlib
import json
import math
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from backend.schemas import HazardAssessment, ZoneImpact, to_dict

LEVELS = ["monitoring", "elevated", "urgent_review"]
LIKELIHOODS = ["low", "medium", "high"]
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

TOOL = {
    "name": "submit_assessment",
    "description": "Submit the hazard assessment for the site. Zones must come from the provided list.",
    "input_schema": {
        "type": "object",
        "properties": {
            "risk_level": {"type": "string", "enum": LEVELS},
            "zones": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "zone_id": {"type": "string"},
                        "likelihood": {"type": "string", "enum": LIKELIHOODS},
                        "evidence_nodes": {"type": "array", "items": {"type": "string"}},
                        "reasons": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["zone_id", "likelihood", "evidence_nodes", "reasons"],
                },
            },
            "coverage_gaps": {"type": "array", "items": {"type": "string"}},
            "timing": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["unavailable", "experimental"]},
                    "earliest_utc": {"type": ["string", "null"]},
                    "latest_utc": {"type": ["string", "null"]},
                    "basis": {"type": "string"},
                },
                "required": ["status"],
            },
            "summary": {"type": "string"},
        },
        "required": ["risk_level", "zones", "coverage_gaps", "timing", "summary"],
    },
}


# ================================================================ layer 1: evidence
def inverse_velocity_fit(series: list, now: datetime, rules: dict) -> Optional[dict]:
    """Fukuzono-style fit on tilt: 1/velocity falls linearly to zero at failure.

    series: [(utc datetime, tilt_deg)], oldest first. Returns a fit only when the stake is
    clearly accelerating and the line fits well; otherwise None (timing unavailable)."""
    window = rules.get("window_s", 900)
    pts = [(t, v) for t, v in series if (now - t).total_seconds() <= window]
    if len(pts) < rules.get("min_points", 8) + 4:
        return None
    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() / 3600 for t, _ in pts]
    ys = [v for _, v in pts]
    vel = []                                            # rolling 5-point slope, deg/h
    for i in range(2, len(xs) - 2):
        sx, sy = xs[i - 2:i + 3], ys[i - 2:i + 3]
        mx, my = sum(sx) / 5, sum(sy) / 5
        sxx = sum((x - mx) ** 2 for x in sx)
        if sxx > 0:
            vel.append((xs[i], sum((x - mx) * (y - my) for x, y in zip(sx, sy)) / sxx))
    vel = [(x, v) for x, v in vel if v >= rules.get("min_velocity_deg_per_h", 2.0)]
    if len(vel) < rules.get("min_points", 8):
        return None
    X = [x for x, _ in vel]
    Y = [1.0 / v for _, v in vel]
    mx, my = sum(X) / len(X), sum(Y) / len(Y)
    sxx = sum((x - mx) ** 2 for x in X)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(X, Y)) / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in Y)
    r2 = 1 - sum((y - (a + b * x)) ** 2 for x, y in zip(X, Y)) / ss_tot if ss_tot > 0 else 0
    if b >= 0 or r2 < rules.get("min_r2", 0.8):
        return None
    t_fail = t0 + timedelta(hours=-a / b)
    lead = (t_fail - now).total_seconds()
    if lead <= 0 or lead > 48 * 3600:
        return None
    return {"predicted_failure_utc": t_fail.isoformat(),
            "earliest_utc": (now + timedelta(seconds=0.7 * lead)).isoformat(),
            "latest_utc": (now + timedelta(seconds=1.3 * lead)).isoformat(),
            "r2": round(r2, 3), "points": len(vel),
            "latest_velocity_deg_per_h": round(vel[-1][1], 2),
            "method": "inverse velocity of tilt (Fukuzono 1985), experimental"}


def node_evidence(node_id: str, cfg, feat, fit: Optional[dict], rules: dict, stale_after_s: float) -> dict:
    gap = None
    if feat is None or feat.quality == "unavailable":
        gap = f"{node_id}: no usable data"
    elif feat.latest_reading_age_s is not None and feat.latest_reading_age_s > stale_after_s:
        gap = f"{node_id}: last reading {int(feat.latest_reading_age_s)} s ago"
    elif feat.tilt_deg is None and feat.wetness_index is None:
        gap = f"{node_id}: neither tilt nor wetness available ({', '.join(feat.quality_reasons[:3])})"
    wet = bool(feat and feat.wetness_index is not None and feat.wetness_index >= rules["wet_index"])
    tilt = feat.tilt_deg if feat else None
    rate = feat.tilt_trend_deg_per_h if feat else None
    watch = bool((tilt is not None and tilt >= rules["tilt_watch_deg"]) or (rate is not None and rate >= rules["tilt_rate_watch_deg_per_h"]))
    alarm = bool((tilt is not None and tilt >= rules["tilt_alarm_deg"]) or (rate is not None and rate >= rules["tilt_rate_alarm_deg_per_h"]) or fit)
    if gap and not (watch or alarm):
        wet = watch = alarm = False
    r = lambda v, n=3: None if v is None else round(v, n)
    return {
        "node_id": node_id, "lat": cfg.lat, "lon": cfg.lon, "sector": cfg.sector,
        "quality": feat.quality if feat else "unavailable",
        "quality_reasons": list(feat.quality_reasons) if feat else ["no readings"],
        "simulated": bool(feat.simulated) if feat else False,
        "wetness_index": r(feat.wetness_index if feat else None, 2),
        "wetness_trend_per_h": r(feat.wetness_trend_per_h if feat else None, 2),
        "tilt_deg": r(tilt, 2), "tilt_downslope_deg": r(feat.tilt_downslope_deg if feat else None, 2),
        "tilt_trend_deg_per_h": r(rate, 2),
        "timing_fit": fit,
        "_flags": {"gap": gap, "wet": wet, "watch": watch, "alarm": alarm},
    }


def rule_view(nodes: dict, weather, zones: dict, rules: dict) -> dict:
    """Plain rules: likelihood per zone, minimum risk level, coverage gaps, reasons."""
    heavy = False
    rain_txt = "no weather data"
    if weather is not None:
        r24, f24 = weather.rain_24h_mm, weather.forecast_24h_mm
        heavy = bool((r24 or 0) >= rules["rain_24h_watch_mm"] or (f24 or 0) >= rules["forecast_24h_watch_mm"])
        rain_txt = f"{r24 if r24 is not None else '?'} mm in the past 24 h, {f24 if f24 is not None else '?'} mm forecast"
    impacts = {}
    for z in zones.values():
        src = [nodes[n] for n in z.source_nodes if n in nodes]
        strong = [n for n in src if n["_flags"]["alarm"] or (n["_flags"]["watch"] and n["_flags"]["wet"])]
        moving = [n for n in src if n["_flags"]["watch"] or n["_flags"]["alarm"]]
        wet = [n for n in src if n["_flags"]["wet"]]
        gaps = [n for n in src if n["_flags"]["gap"]]
        multi = len(z.source_nodes) > 1
        if (not multi and strong) or (multi and len(strong) >= 2):
            lk = "high"
        elif strong or moving or (wet and heavy):
            lk = "medium"
        else:
            lk = "low"
        reasons = []
        for n in moving:
            trend = "" if n["tilt_trend_deg_per_h"] is None else f", trend {n['tilt_trend_deg_per_h']} deg/h"
            reasons.append(f"{n['node_id']} tilted {n['tilt_deg']} deg from its baseline{trend}")
        for n in wet:
            reasons.append(f"{n['node_id']} soil near its wet calibration (index {n['wetness_index']})")
        if heavy:
            reasons.append(f"heavy rain: {rain_txt}")
        if gaps and lk == "low":
            reasons.append("stake above this zone has no current data; low here means no evidence, not safety")
        evidence = sorted({n["node_id"] for n in moving + wet})
        impacts[z.zone_id] = ZoneImpact(z.zone_id, lk, evidence, reasons or ["no warning signs from the stakes above it"])
    lks = [i.likelihood for i in impacts.values()]
    any_alarm = any(n["_flags"]["alarm"] for n in nodes.values())
    if "high" in lks and any_alarm:
        floor = "urgent_review"
    elif "high" in lks or "medium" in lks or (heavy and any(n["_flags"]["wet"] for n in nodes.values())):
        floor = "elevated"
    else:
        floor = "monitoring"
    gaps = [n["_flags"]["gap"] for n in nodes.values() if n["_flags"]["gap"]]
    if weather is None:
        gaps.append("weather: no data")
    elif weather.stale:
        gaps.append(f"weather: stale ({weather.source.split(' (')[0]})")
    return {"impacts": impacts, "floor": floor, "gaps": gaps, "heavy_rain": heavy, "rain_text": rain_txt}


# ================================================================ layer 2: Claude
def call_claude_http(api_key: str, model: str, system: str, evidence: dict, max_tokens: int, timeout: float) -> dict:
    """POST to the Messages API with a forced tool call. Returns the tool input dict."""
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "tools": [TOOL],
        "tool_choice": {"type": "tool", "name": TOOL["name"]},
        "messages": [{"role": "user", "content": "Evidence for the current assessment:\n" + json.dumps(evidence, indent=1)}],
    }
    req = urllib.request.Request(ANTHROPIC_URL, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode())
    for block in resp.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == TOOL["name"]:
            return block["input"]
    raise ValueError(f"no tool call in Claude response (stop_reason={resp.get('stop_reason')})")


# ================================================================ layer 3: checks
def _parse_iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def validate(answer: dict, view: dict, nodes: dict, zones: dict, fits: dict) -> tuple:
    """Returns (risk_level, impacts, gaps, timing_status, timing_window, summary, notes)."""
    notes = []
    if not isinstance(answer, dict):
        raise ValueError("answer is not an object")
    level = answer.get("risk_level")
    if level not in LEVELS:
        raise ValueError(f"invalid risk_level {level!r}")
    if LEVELS.index(level) < LEVELS.index(view["floor"]):
        notes.append(f"risk level raised from {level} to the rule floor {view['floor']}")
        level = view["floor"]

    impacts = {}
    for z in answer.get("zones", []) or []:
        zid = z.get("zone_id")
        if zid not in zones:
            notes.append(f"dropped unknown zone {zid!r}")
            continue
        lk = z.get("likelihood")
        if lk not in LIKELIHOODS:
            notes.append(f"zone {zid}: invalid likelihood {lk!r}, using rule value")
            lk = view["impacts"][zid].likelihood
        ev = [n for n in z.get("evidence_nodes", []) if n in nodes]
        if len(ev) != len(z.get("evidence_nodes", [])):
            notes.append(f"zone {zid}: dropped unknown evidence nodes")
        reasons = [str(r)[:300] for r in (z.get("reasons") or [])][:5] or ["(no reason given)"]
        impacts[zid] = ZoneImpact(zid, lk, ev, reasons)
    # a zone the rules call high must not silently disappear
    for zid, imp in view["impacts"].items():
        if imp.likelihood == "high" and (zid not in impacts or impacts[zid].likelihood == "low"):
            notes.append(f"zone {zid}: kept at high from the rules")
            impacts[zid] = imp
        elif zid not in impacts:
            impacts[zid] = ZoneImpact(zid, "low", [], ["not named by the assessment"])

    gaps = [str(g)[:200] for g in (answer.get("coverage_gaps") or [])]
    for g in view["gaps"]:
        if not any(g.split(":")[0] in x for x in gaps):
            gaps.append(g)

    timing = answer.get("timing") or {}
    best_fit = min(fits.values(), key=lambda f: f["earliest_utc"]) if fits else None
    if timing.get("status") == "experimental" and not best_fit:
        notes.append("removed timing: no stake has a valid inverse-velocity fit")
    if best_fit:
        status, window = "experimental", {"earliest_utc": best_fit["earliest_utc"], "latest_utc": best_fit["latest_utc"],
                                          "basis": best_fit["method"] + f" (R2 {best_fit['r2']})"}
        if timing.get("status") != "experimental":
            notes.append("timing window added from the code fit")
    else:
        status, window = "unavailable", None
    summary = str(answer.get("summary") or "").strip()[:600] or "No summary returned."
    return level, list(impacts.values()), gaps, status, window, summary, notes


def rules_summary(view: dict, level: str) -> str:
    named = [i for i in view["impacts"].values() if i.likelihood != "low"]
    if not named:
        s = "No stake shows movement and soil is not near saturation."
    else:
        s = "Watch " + ", ".join(f"{i.zone_id} ({i.likelihood})" for i in sorted(named, key=lambda i: -LIKELIHOODS.index(i.likelihood))) + "."
    s += f" Rainfall: {view['rain_text']}."
    if view["gaps"]:
        s += f" Missing data: {len(view['gaps'])} source(s)."
    return s


# ================================================================ engine
class HazardEngine:
    def __init__(self, settings, call_claude: Optional[Callable] = None):
        self.s = settings
        self.policy = settings.hazard
        self.rules = self.policy["rules"]
        self.call_claude = call_claude
        prompt_path = settings.path(self.policy.get("prompt_file", "config/hazard-prompt.md"))
        self.prompt = prompt_path.read_text() if prompt_path.exists() else ""
        first = self.prompt.splitlines()[0] if self.prompt else ""
        self.prompt_version = first.split(":", 1)[1].strip() if first.startswith("prompt_version") else None
        self.last: Optional[HazardAssessment] = None
        self.last_time: Optional[datetime] = None
        self.last_fingerprint: Optional[str] = None
        self.counter = 0

    def _claude(self):
        if self.call_claude is not None:
            return self.call_claude
        if not (self.policy.get("use_claude", True) and self.s.anthropic_api_key):
            return None
        key = self.s.anthropic_api_key
        return lambda system, evidence: call_claude_http(key, self.policy["model"], system, evidence,
                                                         self.policy.get("max_tokens", 1500), self.policy.get("timeout_s", 30))

    def build_evidence(self, features: dict, series: dict, weather, zones: dict, now: datetime):
        fits = {}
        nodes = {}
        for nid, cfg in self.s.nodes.items():
            fit = inverse_velocity_fit(series.get(nid, []), now, self.policy.get("timing_fit", {}))
            if fit:
                fits[nid] = fit
            nodes[nid] = node_evidence(nid, cfg, features.get(nid), fit, self.rules,
                                       self.policy.get("node_stale_after_s", 15))
        view = rule_view(nodes, weather, zones, self.rules)
        w = None
        if weather is not None:
            w = {"source": weather.source, "stale": weather.stale, "rain_1h_mm": weather.rain_1h_mm,
                 "rain_6h_mm": weather.rain_6h_mm, "rain_24h_mm": weather.rain_24h_mm, "rain_72h_mm": weather.rain_72h_mm,
                 "forecast_24h_mm": weather.forecast_24h_mm, "peak_mm_per_h": weather.peak_mm_per_h,
                 "rain_threshold_crossing_utc": weather.threshold_crossing_utc.isoformat() if weather.threshold_crossing_utc else None,
                 "threshold_used": weather.threshold_name}
        evidence = {
            "now_utc": now.isoformat(),
            "site": {"name": self.s.site.get("name"), "timezone": self.s.site.get("timezone")},
            "nodes": [{k: v for k, v in n.items() if k != "_flags"} for n in nodes.values()],
            "rules": {"minimum_risk_level": view["floor"],
                      "zone_likelihoods": {z: i.likelihood for z, i in view["impacts"].items()},
                      "coverage_gaps": view["gaps"]},
            "weather": w,
            "zones": [{"zone_id": z.zone_id, "name": z.name, "source_nodes": z.source_nodes,
                       "severity_if_hit": z.severity_if_hit} for z in zones.values()],
        }
        return evidence, nodes, view, fits

    def fingerprint(self, evidence: dict) -> str:
        def rnd(n):
            return {"q": n["quality"], "w": None if n["wetness_index"] is None else round(n["wetness_index"], 1),
                    "t": None if n["tilt_deg"] is None else round(n["tilt_deg"] * 2) / 2,
                    "r": None if n["tilt_trend_deg_per_h"] is None else round(math.log10(max(abs(n["tilt_trend_deg_per_h"]), 0.1)), 1),
                    "f": bool(n["timing_fit"])}
        core = {"n": [rnd(n) for n in evidence["nodes"]], "rules": evidence["rules"],
                "w": None if not evidence["weather"] else [evidence["weather"]["stale"],
                     round((evidence["weather"]["rain_24h_mm"] or 0) / 10), round((evidence["weather"]["forecast_24h_mm"] or 0) / 10)]}
        return hashlib.sha1(json.dumps(core, sort_keys=True).encode()).hexdigest()

    def maybe_assess(self, features: dict, series: dict, weather, zones: dict, now: datetime,
                     force: bool = False) -> Optional[HazardAssessment]:
        """New assessment if the evidence changed (or force), rate-limited; else None."""
        evidence, nodes, view, fits = self.build_evidence(features, series, weather, zones, now)
        fp = self.fingerprint(evidence)
        if not force and self.last is not None:
            age = (now - self.last_time).total_seconds()
            if fp == self.last_fingerprint and age < 10 * self.policy.get("min_interval_s", 20):
                return None
            if age < self.policy.get("min_interval_s", 20) and LEVELS.index(view["floor"]) <= LEVELS.index(self.last.risk_level):
                return None
        a = self._assess(evidence, nodes, view, fits, weather, zones, now, any(n["simulated"] for n in nodes.values()))
        self.last, self.last_time, self.last_fingerprint = a, now, fp
        return a

    def assess(self, features: dict, series: dict, weather, zones: dict, now: datetime) -> HazardAssessment:
        evidence, nodes, view, fits = self.build_evidence(features, series, weather, zones, now)
        return self._assess(evidence, nodes, view, fits, weather, zones, now, any(n["simulated"] for n in nodes.values()))

    def _assess(self, evidence, nodes, view, fits, weather, zones, now, simulated) -> HazardAssessment:
        self.counter += 1
        aid = f"HA-{now.strftime('%Y%m%dT%H%M%S')}-{self.counter:04d}"
        notes, engine = [], "rules-only"
        claude = self._claude()
        result = None
        if claude is not None:
            try:
                answer = claude(self.prompt, evidence)
                result = validate(answer, view, nodes, zones, fits)
                engine = f"claude:{self.policy.get('model')}"
                notes = result[6]
            except Exception as e:
                engine = "rules-fallback"
                notes = [f"Claude unavailable or answer rejected: {type(e).__name__}: {str(e)[:160]}"]
        elif self.policy.get("use_claude", True):
            notes = ["ANTHROPIC_API_KEY not set: rule-based assessment only"]
        if result is None:
            level = view["floor"]
            best = min(fits.values(), key=lambda f: f["earliest_utc"]) if fits else None
            result = (level, list(view["impacts"].values()), view["gaps"],
                      "experimental" if best else "unavailable",
                      {"earliest_utc": best["earliest_utc"], "latest_utc": best["latest_utc"],
                       "basis": best["method"] + f" (R2 {best['r2']})"} if best else None,
                      rules_summary(view, level), notes)
        level, impacts, gaps, tstatus, twindow, summary, notes = result
        quality = "degraded" if gaps else "ok"
        return HazardAssessment(
            assessment_id=aid, site_id=self.s.site_id, created_at_utc=now, engine=engine,
            input_refs={"weather_snapshot": weather.snapshot_id if weather else None,
                        "nodes": {n: {"quality": v["quality"], "tilt_deg": v["tilt_deg"], "wetness_index": v["wetness_index"]}
                                  for n, v in nodes.items()}},
            risk_level=level, zone_impacts=impacts, coverage_gaps=gaps,
            timing_status=tstatus, timing_window=twindow,
            rain_threshold_crossing_utc=weather.threshold_crossing_utc if weather else None,
            summary=summary, quality=quality, notes=notes,
            policy_version=self.policy.get("version", ""), prompt_version=self.prompt_version,
            simulated=simulated or bool(weather and weather.simulated),
        )

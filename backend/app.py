"""Web server, API and background tasks (standard library only).

Starts: serial readers for every Arduino it finds (live mode), filler stakes, the weather
refresher, the scenario player (replay mode) and a 1-second tick that keeps the hazard
assessment and plan current. Serves backend/static/ and the JSON API in docs/api.md.
Route handlers only check permissions, call the pipeline or read stored results.
"""
from __future__ import annotations

import csv
import io
import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from backend import security
from backend.notifications import local_time
from backend.pipeline import Conflict, Pipeline
from backend.schemas import to_dict
from backend.sensors import SerialReader
from backend.weather import WeatherService

STATIC = Path(__file__).resolve().parent / "static"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".json": "application/json", ".png": "image/png", ".svg": "image/svg+xml"}


# ================================================================ views (read stored results)
def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def plan_rows(app, plan: Optional[dict], user) -> list:
    if not plan:
        return []
    p = app.pipeline
    needs = {n.household_id: n for n in p.needs}
    accepted = {a["household_id"]: a for a in p.db.assignments()}
    plan_asg = {a["household_id"]: a for a in plan.get("assignments", [])}
    progress = {}
    for e in p.db.progress():
        progress.setdefault(e["household_id"], set()).add(e["kind"])
    msgs = {m["household_id"]: m for m in p.db.notifications()}
    rows = []
    for a in plan["actions"]:
        hid = a["household_id"]
        hh = p.ds.households.get(hid)
        n = needs.get(hid)
        asg = accepted.get(hid) or plan_asg.get(hid)
        team = p.ds.teams.get(a.get("team_id")) if a.get("team_id") else None
        shelter = p.ds.shelters.get(a.get("destination_id")) if a.get("destination_id") else None
        done = progress.get(hid, set())
        status = ("evacuated" if "evacuated" in done else "team arrived" if "team_arrived" in done
                  else "acknowledged" if "acknowledged" in done else
                  ("message sent" if hid in msgs else "not yet notified"))
        row = {
            "household_id": hid, "name": hh.name if hh else hid, "people_count": n.people_count if n else None,
            "zone_id": a.get("zone_id"), "likelihood": a.get("likelihood"),
            "needs_help": bool(n and n.needs_team), "assistance": n.assistance if n else [],
            "checklist": n.checklist if n else [],
            "prep_min": n.prep_min if n else None, "travel_min": n.travel_min if n else None,
            "prepare_by_utc": a.get("prepare_by_utc"), "leave_by_utc": a.get("leave_by_utc"),
            "team_id": a.get("team_id"), "team_name": team.name if team else None,
            "arrival_by_utc": a.get("arrival_by_utc"),
            "assignment_id": asg["assignment_id"] if asg else None,
            "assignment_state": asg["state"] if asg else None,
            "destination_id": a.get("destination_id"), "destination_name": shelter.name if shelter else None,
            "feasible": a.get("feasible"), "problems": a.get("reasons", []), "status": status,
            "message": msgs[hid]["text"] if hid in msgs and security.can_see_details(user) else None,
        }
        rows.append(row)
    return rows if security.can_see_details(user) else security.strip_sensitive(rows)


def dashboard(app, user) -> dict:
    p = app.pipeline
    now = p.clock.now()
    a = p.assessment
    zl = {z.zone_id: z for z in a.zone_impacts} if a else {}
    nodes = []
    for nid, cfg in p.s.nodes.items():
        f = p.features.get(nid)
        src = p.sources.get(nid)
        age = (now - src["last_seen"]).total_seconds() if src else None
        status = "no-data" if f is None else f.quality
        if age is not None and age > max(3 * p.stale_after_s, 30):
            status = "no-data"
        nodes.append({"node_id": nid, "lat": cfg.lat, "lon": cfg.lon, "sector": cfg.sector, "status": status,
                      "source": src["source"] if src else None, "simulated": bool(src and src["simulated"]),
                      "last_seen_age_s": age,
                      "wetness_index": f.wetness_index if f else None, "moisture_raw": f.moisture_raw if f else None,
                      "tilt_deg": f.tilt_deg if f else None, "tilt_trend_deg_per_h": f.tilt_trend_deg_per_h if f else None,
                      "reasons": list(f.quality_reasons) if f else ["no readings yet"]})
    zones = []
    for z in p.ds.zones.values():
        imp = zl.get(z.zone_id)
        zones.append({"zone_id": z.zone_id, "name": z.name, "ring": z.ring, "severity_if_hit": z.severity_if_hit,
                      "origin": z.origin, "source_nodes": z.source_nodes,
                      "likelihood": imp.likelihood if imp else None,
                      "evidence_nodes": imp.evidence_nodes if imp else [], "reasons": imp.reasons if imp else []})
    plan = p.db.latest_plan()
    approved = p.db.latest_plan("approved")
    rows = plan_rows(app, plan, user)
    by_building = {}
    exposed = {r["household_id"]: r for r in rows}
    for h in p.ds.households.values():
        b = p.ds.buildings[h.building_id]
        e = by_building.setdefault(b.building_id, {"building_id": b.building_id, "lat": b.lat, "lon": b.lon,
                                                   "households": [], "exposed": False, "likelihood": None,
                                                   "needs_help": False, "status": None})
        e["households"].append(h.household_id)
        r = exposed.get(h.household_id)
        if r:
            e["exposed"] = True
            order = {"low": 0, "medium": 1, "high": 2, None: -1}
            if order[r["likelihood"]] > order[e["likelihood"]]:
                e["likelihood"] = r["likelihood"]
            e["needs_help"] = e["needs_help"] or r["needs_help"]
            e["status"] = r["status"]
    pending = [r["leave_by_utc"] for r in rows if r["leave_by_utc"] and r["status"] != "evacuated"]
    counts = {"exposed": len(rows), "need_help": sum(r["needs_help"] for r in rows),
              "team_assigned": sum(1 for r in rows if r["team_id"]),
              "accepted": sum(1 for r in rows if r["assignment_state"] in ("accepted", "arrived", "done")),
              "acknowledged": sum(1 for r in rows if r["status"] in ("acknowledged", "team arrived", "evacuated")),
              "evacuated": sum(1 for r in rows if r["status"] == "evacuated"),
              "infeasible": sum(1 for r in rows if r["feasible"] is False)}
    w = p.weather
    out = {
        "server_time_utc": now.isoformat(), "utc_offset_hours": p.s.site.get("utc_offset_hours", 0),
        "mode": app.mode, "replay": app.replay_status(), "role": user.role,
        "site": {"name": p.s.site.get("name"), "center": p.s.site["center"], "zoom": p.s.site.get("zoom", 16)},
        "nodes": nodes, "zones": zones, "buildings": list(by_building.values()),
        "shelters": [to_dict(s) for s in p.ds.shelters.values()],
        "assessment": None if a is None else {
            "assessment_id": a.assessment_id, "created_at_utc": _iso(a.created_at_utc), "engine": a.engine,
            "risk_level": a.risk_level, "summary": a.summary, "coverage_gaps": a.coverage_gaps,
            "timing_status": a.timing_status, "timing_window": a.timing_window,
            "rain_threshold_crossing_utc": _iso(a.rain_threshold_crossing_utc), "notes": a.notes,
            "quality": a.quality, "simulated": a.simulated},
        "weather": None if w is None else {
            "source": w.source, "stale": w.stale, "fetched_at_utc": _iso(w.fetched_at_utc), "error": w.error,
            "rain_1h_mm": w.rain_1h_mm, "rain_24h_mm": w.rain_24h_mm, "rain_72h_mm": w.rain_72h_mm,
            "forecast_24h_mm": w.forecast_24h_mm, "peak_mm_per_h": w.peak_mm_per_h,
            "threshold_crossing_utc": _iso(w.threshold_crossing_utc), "threshold_name": w.threshold_name,
            "hours": w.hours[-72:], "simulated": w.simulated},
        "plan": None if plan is None else {
            "plan_id": plan["plan_id"], "version": plan["version"], "state": plan["state"],
            "created_at_utc": plan["created_at_utc"], "horizon_utc": plan["horizon_utc"],
            "horizon_basis": plan["horizon_basis"], "unresolved": plan["unresolved"], "counts": counts},
        "approved_plan": None if approved is None else {
            "plan_id": approved["plan_id"], "version": approved["version"],
            "approved_by": approved.get("approved_by"), "approved_at_utc": approved.get("approved_at_utc")},
        "operator_horizon_utc": p.db.kv_get("operator_horizon_utc"),
        "rows": rows,
        "countdown": {"label": "first household must leave", "time_utc": min(pending)} if pending else None,
        "events": list(p.events)[:40],
        "outbox": p.db.notifications()[-12:] if user.role == "operator" else [],
    }
    return out          # rows were already filtered by plan_rows for this user


def committee_queue(app, user) -> dict:
    d = dashboard(app, user)
    rows = sorted([r for r in d["rows"] if r["needs_help"] or r["feasible"] is False],
                  key=lambda r: (r["arrival_by_utc"] or r["leave_by_utc"] or "9999"))
    unack = [r for r in d["rows"] if r["status"] == "message sent" and r["likelihood"] == "high"]
    teams = []
    accepted = app.pipeline.db.assignments()
    for t in app.pipeline.ds.teams.values():
        jobs = [a for a in accepted if a["team_id"] == t.team_id and a["state"] in ("accepted", "arrived")]
        teams.append({"team_id": t.team_id, "name": t.name, "vehicle": t.vehicle, "seats": t.seats,
                      "wheelchair_spaces": t.wheelchair_spaces, "active_jobs": [j["household_id"] for j in jobs]})
    return {"server_time_utc": d["server_time_utc"], "utc_offset_hours": d["utc_offset_hours"], "mode": d["mode"],
            "replay": d["replay"], "plan": d["plan"], "approved_plan": d["approved_plan"], "queue": rows,
            "unacknowledged_high": unack, "teams": teams, "assessment": d["assessment"], "events": d["events"][:15]}


def resident_plan(app, user) -> dict:
    p = app.pipeline
    hid = user.household_id
    hh = p.ds.households.get(hid)
    approved = p.db.latest_plan("approved")
    action = next((a for a in (approved or {}).get("actions", []) if a["household_id"] == hid), None)
    needs = next((n for n in p.needs if n.household_id == hid), None)
    msgs = p.db.notifications(hid)
    asg = next((a for a in p.db.assignments() if a["household_id"] == hid), None)
    if asg is None and action and approved:
        asg = next((a for a in approved["assignments"] if a["household_id"] == hid), None)
    shelter = p.ds.shelters.get(action["destination_id"]) if action and action.get("destination_id") else None
    team = p.ds.teams.get(asg["team_id"]) if asg else None
    prog = sorted({e["kind"] for e in p.db.progress(hid)})
    return {
        "server_time_utc": p.clock.now().isoformat(), "utc_offset_hours": p.s.site.get("utc_offset_hours", 0),
        "mode": app.mode, "replay": app.replay_status(), "household_id": hid, "name": hh.name if hh else hid, "language": hh.language if hh else "en",
        "plan_id": approved["plan_id"] if approved else None, "plan_version": approved["version"] if approved else None,
        "action_needed": action is not None,
        "leave_by_utc": action.get("leave_by_utc") if action else None,
        "prepare_by_utc": action.get("prepare_by_utc") if action else None,
        "zone": p.ds.zones[action["zone_id"]].name if action and action.get("zone_id") in p.ds.zones else None,
        "destination": None if shelter is None else {"name": shelter.name, "lat": shelter.lat, "lon": shelter.lon,
                                                     "wheelchair_accessible": shelter.wheelchair_accessible},
        "checklist": needs.checklist if needs else [],
        "help": None if not (needs and needs.needs_team) else {
            "state": asg["state"] if asg else "requested", "team": team.name if team else None,
            "arrival_by_utc": asg.get("arrival_by_utc") if asg else None},
        "message": msgs[-1]["text"] if msgs else None,
        "progress": prog,
    }


def plan_csv(app, plan: dict, user) -> str:
    off = app.pipeline.s.site.get("utc_offset_hours", 0)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["household", "name", "people", "zone", "likelihood", "needs help", "assistance", "prep min", "travel min",
                "start preparing by", "leave by", "team", "team arrival by", "destination", "feasible", "status", "problems"])
    for r in plan_rows(app, plan, user):
        w.writerow([r["household_id"], r.get("name", ""), r["people_count"], r["zone_id"], r["likelihood"],
                    "yes" if r["needs_help"] else "no", "; ".join(r.get("assistance", [])), r["prep_min"], r["travel_min"],
                    local_time(r["prepare_by_utc"], off), local_time(r["leave_by_utc"], off), r["team_name"] or "",
                    local_time(r["arrival_by_utc"], off), r["destination_name"] or "", r["feasible"], r["status"],
                    "; ".join(r["problems"])])
    return buf.getvalue()


# ================================================================ application and tasks
class App:
    def __init__(self, settings, db, clock, pipeline: Optional[Pipeline] = None, scenario: Optional[dict] = None,
                 weather_service: Optional[WeatherService] = None):
        self.s = settings
        self.db = db
        self.clock = clock
        self.pipeline = pipeline or Pipeline(settings, db, clock)
        self.auth = security.Auth(settings.data_paths["users"])
        self.mode = "replay" if scenario else "live"
        self.scenario = scenario
        self.weather = weather_service or WeatherService(settings)
        self.stop = threading.Event()
        self.threads = []
        self.player = None
        self.readers = {}

    def replay_status(self):
        if not self.player:
            return None
        return {"name": self.scenario.get("name"), "description": self.scenario.get("description"),
                "minute": round(self.player.minute(), 1), "duration_min": self.scenario.get("duration_min"),
                "done": self.player.done, "speed": self.clock.speed}

    def _spawn(self, target, name):
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self.threads.append(t)

    def start_tasks(self):
        from tools.replay import CalmNodes, ScenarioPlayer
        if self.mode == "replay":
            self.player = ScenarioPlayer(self.pipeline, self.scenario, self.s.rainfall)
            self._spawn(lambda: self.player.run(self.stop), "replay")
        else:
            self._spawn(self._serial_manager, "serial-manager")
            self._spawn(self._weather_loop, "weather")
            if self.s.extra_nodes == "calm":
                self._spawn(lambda: CalmNodes(self.pipeline).run(self.stop), "filler-stakes")
            self._spawn(self._tick_loop, "tick")

    def _tick_loop(self):
        while not self.stop.is_set():
            try:
                self.pipeline.tick()
            except Exception:
                traceback.print_exc()
            self.stop.wait(1.0)

    def _weather_loop(self):
        while not self.stop.is_set():
            try:
                self.pipeline.on_weather(self.weather.fetch(self.clock.now()))
            except Exception as e:
                self.pipeline.log(f"weather task error: {e}")
            self.stop.wait(self.s.rainfall.get("refresh_s", 600))

    def _find_ports(self) -> list:
        explicit = [p for p in self.s.serial_ports if p != "auto"]
        if explicit:
            return explicit
        try:
            from serial.tools import list_ports
        except ImportError:
            return []
        found = []
        for port in list_ports.comports():
            text = f"{port.device} {port.description} {port.manufacturer or ''}".lower()
            if any(k in text for k in ("arduino", "usbmodem", "ttyacm", "usbserial", "usb serial", "wchusbserial", "ch340", "cp210")):
                found.append(port.device)
        return found

    def _serial_manager(self):
        """Starts a reader for every Arduino port, now and whenever one is plugged in later."""
        try:
            import serial  # noqa: F401
        except ImportError:
            self.pipeline.log("pyserial is not installed: run `uv sync`. No live stakes will be read.")
            return
        announced = False
        while not self.stop.is_set():
            ports = self._find_ports()
            if not ports and not announced:
                self.pipeline.log("no Arduino found yet; plug it in (checking every 3 s)")
                announced = True
            for port in ports:
                if port not in self.readers:
                    reader = SerialReader(port, self.pipeline.gateway,
                                          lambda res, port=port: self.pipeline.on_sensor_result(res, f"USB {port}"),
                                          lambda msg: self.pipeline.log(f"[serial] {msg}"))
                    self.readers[port] = reader
                    self._spawn(lambda r=reader: r.run(self.stop), f"serial-{port}")
            self.stop.wait(3.0)

    def shutdown(self):
        self.stop.set()


# ================================================================ HTTP
def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SlopeCast/1.0"

        def log_message(self, fmt, *args):
            pass

        def _user(self, qs):
            token = None
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
            token = token or (qs.get("token") or [None])[0]
            return app.auth.identify(token)

        def _send(self, code: int, body, ctype="application/json"):
            data = body if isinstance(body, (bytes, bytearray)) else (
                body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode())
            except ValueError:
                raise Conflict("request body is not valid JSON")

        def _static(self, path: str):
            rel = "index.html" if path in ("/", "") else path.lstrip("/").removeprefix("static/")
            f = (STATIC / rel).resolve()
            if STATIC not in f.parents or not f.is_file():
                return self._send(404, {"error": "not found"})
            self._send(200, f.read_bytes(), CONTENT_TYPES.get(f.suffix, "application/octet-stream"))

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            parts = [x for x in u.path.split("/") if x]
            try:
                if not parts or parts[0] != "api":
                    return self._static(u.path)
                user = self._user(qs)
                p = app.pipeline
                if parts == ["api", "health"]:
                    return self._send(200, {"ok": True, "mode": app.mode, "readings": p.db.reading_count(),
                                            "nodes": {n: p.node_is_live(n) for n in p.s.nodes},
                                            "weather": p.weather.source if p.weather else None,
                                            "claude": bool(p.s.anthropic_api_key) and p.s.hazard.get("use_claude", True)})
                if parts == ["api", "whoami"]:
                    return self._send(200, {"role": user.role, "name": user.name, "household_id": user.household_id})
                if parts == ["api", "dashboard"]:
                    return self._send(200, dashboard(app, user))
                if parts == ["api", "committee", "queue"]:
                    security.require(user, "committee", "operator")
                    return self._send(200, committee_queue(app, user))
                if parts == ["api", "me", "plan"]:
                    security.require(user, "resident")
                    return self._send(200, resident_plan(app, user))
                if len(parts) >= 3 and parts[:2] == ["api", "plans"]:
                    security.require(user, "committee", "operator")
                    plan = p.db.get_plan(parts[2])
                    if plan is None:
                        return self._send(404, {"error": "no such plan"})
                    if len(parts) == 4 and parts[3] == "export.csv":
                        return self._send(200, plan_csv(app, plan, user), "text/csv; charset=utf-8")
                    return self._send(200, plan)
                return self._send(404, {"error": "unknown endpoint"})
            except security.Forbidden as e:
                return self._send(403, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            parts = [x for x in u.path.split("/") if x]
            try:
                user = self._user(qs)
                body = self._body()
                p = app.pipeline
                if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[3] == "approve":
                    security.require(user, "operator")
                    plan = p.approve_plan(parts[2], int(body.get("version", -1)), user.name)
                    return self._send(200, {"plan_id": plan["plan_id"], "version": plan["version"], "state": plan["state"]})
                if parts == ["api", "horizon"]:
                    security.require(user, "operator")
                    m = body.get("minutes")
                    value = p.set_operator_horizon(None if m in (None, "") else float(m), user.name)
                    return self._send(200, {"operator_horizon_utc": value})
                if parts == ["api", "reassess"]:
                    security.require(user, "operator")
                    a = p.tick(force=True)
                    return self._send(200, {"assessment_id": a.assessment_id if a else None})
                if len(parts) == 4 and parts[:2] == ["api", "assignments"] and parts[3] == "accept":
                    security.require(user, "committee", "operator")
                    return self._send(200, p.accept_assignment(parts[2], user.name))
                if parts == ["api", "progress"]:
                    kind = body.get("kind")
                    hid = body.get("household_id") or user.household_id
                    if kind == "team_arrived":
                        security.require(user, "committee", "operator")
                    security.household_scope(user, hid)
                    new = p.record_progress(kind, hid, body.get("team_id"), user.name,
                                            body.get("event_id") or str(uuid.uuid4()), body.get("note", ""))
                    return self._send(200, {"recorded": new})
                return self._send(404, {"error": "unknown endpoint"})
            except security.Forbidden as e:
                return self._send(403, {"error": str(e)})
            except Conflict as e:
                return self._send(409, {"error": str(e)})
            except (ValueError, TypeError) as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
    return Handler


def serve(app: App, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd

"""The conductor. Every event enters here and the section modules run in a fixed order:

    sensor line  -> Gateway -> SensorReading (stored) -> SensorFeatures per node
    weather      -> WeatherSnapshot (stored)
    tick()       -> hazard.maybe_assess -> people.evaluate -> planner.build_plan (all stored)
    operator     -> approve / horizon / accept job / progress -> replan -> notifications

Modules never call each other; only this file wires them. One lock serialises changes so
two clicks cannot corrupt a plan.
"""
from __future__ import annotations

import json
import math
import threading
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Optional

from backend import notifications, people, planner
from backend.hazard import HazardEngine
from backend.households import import_into_db, load_files, load_from_db
from backend.schemas import EvacuationPlan, ProgressEvent, to_dict
from backend.sensors import Gateway, IngestResult, NodeCalibration, compute_features, load_calibration, tilt_from_baseline


class Conflict(RuntimeError):
    """A request based on an out-of-date plan or an invalid state change."""


class Pipeline:
    def __init__(self, settings, db, clock, hazard: Optional[HazardEngine] = None):
        self.s = settings
        self.db = db
        self.clock = clock
        self.lock = threading.RLock()
        self.hazard = hazard or HazardEngine(settings)
        # demo data: the files are the source of truth; upsert them so edits apply on restart
        import_into_db(db, load_files(settings.data_paths, settings.site["center"]))
        self.ds = load_from_db(db)
        site_lookup = {n: settings.site_id for n in settings.nodes}
        self.gateway = Gateway(site_lookup, clock=clock.now, simulated=clock.simulated)
        self.sim_gateway = Gateway(site_lookup, clock=clock.now, simulated=True)
        self.history = {n: deque(maxlen=7200) for n in settings.nodes}
        self.features = {}
        self.sources = {}                     # node -> {"source": str, "last_seen": datetime, "simulated": bool}
        self.weather = None
        self.assessment = None
        self.needs = []
        self.events = deque(maxlen=200)
        self.calibration_overrides = {}       # replays supply calibration for simulated stakes
        self.session_baselines = {}           # node -> (session_id, g0) when auto baseline is used
        self.window_s, self.stale_after_s = 600.0, 10.0
        self._cal_file = {}
        self._cal_defaults = {}
        self.reload_calibration()

    # ---------------------------------------------------------------- helpers
    def log(self, msg: str):
        self.events.appendleft({"time_utc": self.clock.now().isoformat(), "msg": msg})

    def set_time_scale(self, step_s: float):
        """Replays send one reading per step_s simulated seconds; widen windows to match."""
        self.window_s = max(600.0, 15 * step_s)
        self.stale_after_s = max(10.0, 3 * step_s)
        self.hazard.policy = dict(self.hazard.policy, node_stale_after_s=max(15.0, 3.5 * step_s),
                                  min_interval_s=max(self.hazard.policy.get("min_interval_s", 20), 2 * step_s))

    def reload_calibration(self):
        raw = json.loads(self.s.calibration_path.read_text())
        self._cal_defaults = raw.get("defaults", {})
        self._cal_file = load_calibration(self.s.calibration_path)

    def calibration_for(self, node_id: str) -> tuple:
        """(NodeCalibration, notes). Fills gaps from labelled defaults; never silently."""
        if node_id in self.calibration_overrides:
            return self.calibration_overrides[node_id], ["info:scenario_calibration"]
        cal = self._cal_file.get(node_id) or NodeCalibration(node_id, "uncalibrated")
        notes = []
        dm = self._cal_defaults.get("moisture", {})
        if (cal.dry_raw is None or cal.wet_raw is None) and dm.get("dry_raw") is not None:
            cal = replace(cal, dry_raw=dm["dry_raw"], wet_raw=dm["wet_raw"])
            notes.append("info:default_moisture_calibration")
        if cal.baseline_g is None and self._cal_defaults.get("auto_tilt_baseline"):
            sb = self.session_baselines.get(node_id)
            hist = self.history.get(node_id)
            if hist and (sb is None or sb[0] != hist[-1].session_id):
                sb = self._try_session_baseline(node_id)
            if sb:
                cal = replace(cal, baseline_g=sb[1], accel_model=None)
                notes.append("info:session_tilt_baseline")
        return cal, notes

    def _try_session_baseline(self, node_id: str):
        hist = self.history[node_id]
        sess = hist[-1].session_id
        good = [r for r in hist if r.session_id == sess and r.accel_g is not None and not r.health_flags][:10]
        if len(good) < 10:
            return None
        g = tuple(sum(r.accel_g[i] for r in good) / 10 for i in range(3))
        sd = max(math.sqrt(sum((r.accel_g[i] - g[i]) ** 2 for r in good) / 10) for i in range(3))
        if sd > 0.02:
            return None
        self.session_baselines[node_id] = (sess, g)
        self.log(f"{node_id}: no saved tilt baseline; using the first 10 still seconds of this session")
        return self.session_baselines[node_id]

    # ---------------------------------------------------------------- inputs
    def on_line(self, line: str, source: str, simulated: bool = False) -> IngestResult:
        gw = self.sim_gateway if simulated else self.gateway
        res = gw.ingest_line(line, self.clock.now())
        self.on_sensor_result(res, source)
        return res

    def on_sensor_result(self, res: IngestResult, source: str = "serial"):
        with self.lock:
            if res.kind == "hello":
                self.log(f"{res.hello['node']} connected via {source}: firmware {res.hello['fw']}, accelerometer {res.hello['acc_model']}")
                return
            if res.kind != "reading":
                return
            r = res.reading
            if r.node_id not in self.history:
                self.log(f"reading from unknown node {r.node_id} ignored (add it to config/site.json)")
                return
            self.db.save_reading(r)
            self.history[r.node_id].append(r)
            self.sources[r.node_id] = {"source": source, "last_seen": r.receipt_time_utc, "simulated": r.simulated}
            self._update_features(r.node_id)

    def _update_features(self, node_id: str):
        cal, notes = self.calibration_for(node_id)
        f = compute_features(self.history[node_id], cal, node_id, self.clock.now(),
                             window_s=self.window_s, stale_after_s=self.stale_after_s)
        if notes:
            f = replace(f, quality_reasons=tuple(f.quality_reasons) + tuple(notes))
        self.features[node_id] = f

    def node_is_live(self, node_id: str, within_s: float = 10) -> bool:
        src = self.sources.get(node_id)
        return bool(src and not src["simulated"] and (self.clock.now() - src["last_seen"]).total_seconds() < within_s)

    def on_weather(self, snap):
        with self.lock:
            self.weather = snap
            self.db.put("weather", snap.snapshot_id, snap)
            self.log(f"weather: {snap.source.split(' (')[0]}" + (" (stale)" if snap.stale else "")
                     + f", {snap.rain_24h_mm} mm past 24 h, {snap.forecast_24h_mm} mm next 24 h")

    # ---------------------------------------------------------------- the main step
    def tick(self, force: bool = False):
        with self.lock:
            now = self.clock.now()
            for n in self.s.nodes:
                if self.history[n]:
                    self._update_features(n)
            fit_window = self.hazard.policy.get("timing_fit", {}).get("window_s", 900)
            series = {}
            for n in self.s.nodes:
                cal, _ = self.calibration_for(n)
                if cal.baseline_g is None:
                    series[n] = []
                    continue
                recent = [r for r in self.history[n] if r.accel_g is not None and not (set(r.health_flags) & {
                    "accel_missing", "accel_unsupported_model", "accel_read_failed"})
                    and (now - r.sample_time_utc).total_seconds() <= fit_window]
                series[n] = [(r.sample_time_utc, tilt_from_baseline(r.accel_g, cal.baseline_g)) for r in recent]
            a = self.hazard.maybe_assess(self.features, series, self.weather, self.ds.zones, now, force=force)
            if a is None:
                return None
            self.assessment = a
            self.db.put("assessment", a.assessment_id, a)
            sig = self._signature(a)
            changed = sig != getattr(self, "_last_sig", None)
            self.needs = people.evaluate(a, self.ds, self.s.evacuation)
            self.db.put("needs", a.assessment_id, {"assessment_id": a.assessment_id, "created_at_utc": now.isoformat(),
                                                   "needs": [to_dict(n) for n in self.needs]})
            if changed or force or self.db.latest_plan() is None:
                self._last_sig = sig
                self.log(f"assessment {a.assessment_id}: {a.risk_level} by {a.engine}"
                         + (f" ({'; '.join(a.notes)[:140]})" if a.notes else ""))
                self.replan("assessment changed" if changed else "reassessed")
            return a

    @staticmethod
    def _signature(a) -> tuple:
        """What matters for the plan: level, which paths at what likelihood, timing, gaps."""
        t = a.timing_window["earliest_utc"][:15] if a.timing_window else None   # to 10 minutes
        return (a.risk_level, tuple(sorted((z.zone_id, z.likelihood) for z in a.zone_impacts)),
                a.timing_status, t, len(a.coverage_gaps))

    def _anchored_default(self, now: datetime) -> Optional[datetime]:
        """Policy-default horizon fixed when a level is first reached; tightened on escalation."""
        minutes = (self.s.evacuation.get("default_horizon_minutes") or {}).get(self.assessment.risk_level)
        if not minutes:
            self.db.kv_set("policy_horizon_utc", None)
            return None
        cand = now + timedelta(minutes=minutes)
        old = self.db.kv_get("policy_horizon_utc")
        value = min(datetime.fromisoformat(old), cand) if old else cand
        self.db.kv_set("policy_horizon_utc", value.isoformat())
        return value

    def replan(self, reason: str) -> Optional[dict]:
        with self.lock:
            if self.assessment is None:
                return None
            now = self.clock.now()
            op = self.db.kv_get("operator_horizon_utc")
            op_dt = datetime.fromisoformat(op) if op else None
            plan = planner.build_plan(self.assessment, self.needs, self.ds, self.db.assignments(("accepted", "arrived", "done")),
                                      self.s.evacuation, now, self.db.next_plan_version(), op_dt,
                                      simulated=self.clock.simulated, anchored_default=self._anchored_default(now))
            done = {e["household_id"] for e in self.db.progress() if e["kind"] == "evacuated"}
            for act in plan.actions:
                if act.household_id in done:
                    act.reasons.append("evacuated")
            prev = self.db.latest_plan("draft")
            if prev:
                prev["state"] = "superseded"
                self.db.save_plan(prev)
            self.db.save_plan(plan)
            self.log(f"plan {plan.plan_id} (v{plan.version}) drafted: {reason}; "
                     f"{len(plan.actions)} exposed households, horizon {plan.horizon_basis}")
            return to_dict(plan)

    # ---------------------------------------------------------------- operator and field actions
    def approve_plan(self, plan_id: str, version: int, actor: str) -> dict:
        with self.lock:
            latest = self.db.latest_plan()
            if latest is None or latest["plan_id"] != plan_id or latest["version"] != version:
                raise Conflict(f"plan {plan_id} v{version} is out of date; latest is "
                               f"{latest['plan_id'] if latest else 'none'} v{latest['version'] if latest else '-'}")
            if latest["state"] == "approved":
                return latest
            old = self.db.latest_plan("approved")
            if old:
                old["state"] = "superseded"
                self.db.save_plan(old)
            latest["state"] = "approved"
            latest["approved_by"] = actor
            latest["approved_at_utc"] = self.clock.now().isoformat()
            self.db.save_plan(latest)
            needs = {n.household_id: to_dict(n) for n in self.needs}
            asg = {a["household_id"]: a for a in latest["assignments"]}
            for a in self.db.assignments():
                asg[a["household_id"]] = a
            sent = notifications.on_plan_approved(latest, self.ds, needs, asg, self.s.messages,
                                                  self.s.site.get("utc_offset_hours", 0), self.db, self.clock.now())
            self.log(f"plan {plan_id} approved by {actor}; {len(sent)} resident messages queued (simulated)")
            return latest

    def set_operator_horizon(self, minutes: Optional[float], actor: str) -> Optional[str]:
        with self.lock:
            if minutes is None:
                self.db.kv_set("operator_horizon_utc", None)
                self.log(f"operator horizon cleared by {actor}")
                value = None
            else:
                if not 5 <= minutes <= 72 * 60:
                    raise Conflict("horizon must be between 5 minutes and 72 hours")
                value = (self.clock.now() + timedelta(minutes=minutes)).isoformat()
                self.db.kv_set("operator_horizon_utc", value)
                self.log(f"operator horizon set by {actor}: {minutes:.0f} min from now")
            self.replan("operator horizon changed")
            return value

    def accept_assignment(self, assignment_id: str, actor: str) -> dict:
        with self.lock:
            existing = self.db.get_assignment(assignment_id)
            if existing and existing["state"] in ("accepted", "arrived", "done"):
                return existing
            plan = self.db.latest_plan()
            match = next((a for a in (plan or {}).get("assignments", []) if a["assignment_id"] == assignment_id), None)
            if match is None:
                raise Conflict(f"assignment {assignment_id} is not in the latest plan")
            match.update(state="accepted", accepted_by=actor, accepted_at_utc=self.clock.now().isoformat())
            self.db.save_assignment(match)
            self.log(f"{match['team_id']} accepted {match['household_id']} ({actor})")
            self.replan(f"{match['team_id']} accepted a job")
            return match

    def record_progress(self, kind: str, household_id: Optional[str], team_id: Optional[str],
                        actor: str, event_id: str, note: str = "") -> bool:
        if kind not in ("acknowledged", "team_arrived", "evacuated"):
            raise Conflict(f"unknown progress kind {kind}")
        with self.lock:
            if household_id and household_id not in self.ds.households:
                raise Conflict(f"unknown household {household_id}")
            plan = self.db.latest_plan("approved") or self.db.latest_plan()
            ev = ProgressEvent(event_id, plan["plan_id"] if plan else None, household_id, team_id, kind, actor,
                               self.clock.now(), note)
            new = self.db.add_progress(ev)
            if not new:
                return False
            if kind == "acknowledged" and household_id:
                notifications.acknowledge(self.db, household_id, self.clock.now())
            for a in self.db.assignments():
                if a["household_id"] == household_id:
                    if kind == "team_arrived" and a["state"] == "accepted":
                        a["state"] = "arrived"; self.db.save_assignment(a)
                    if kind == "evacuated":
                        a["state"] = "done"; self.db.save_assignment(a)
            self.log(f"{household_id or team_id}: {kind} ({actor})")
            if kind != "acknowledged":
                self.replan(f"progress: {kind}")
            return True

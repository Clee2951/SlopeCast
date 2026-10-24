"""Feeds simulated stakes and weather through the normal pipeline. Not a second risk engine.

Two uses:
  * Scenario replay:  uv run python -m backend --replay tests/fixtures/scenarios/escalating.json
    or headless:      uv run python tools/replay.py tests/fixtures/scenarios/escalating.json
  * Filler stakes in live mode: CalmNodes sends quiet readings for stakes marked
    "simulated-if-absent" in config/site.json, so the map shows several stakes while the
    real Arduino is the one that moves. Every simulated record is tagged simulated.

Readings are written in the same JSON line format as the firmware (docs/serial-protocol.md)
and go through the same Gateway, so replay exercises the real ingestion code.
"""
from __future__ import annotations

import json
import math
import random
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.sensors import NodeCalibration  # noqa: E402
from backend.weather import snapshot_from_scenario, build_snapshot  # noqa: E402

UPRIGHT_G0 = (0.0, -1.0, 0.0)      # simulated stakes hold the board upright
DOWNSLOPE = (0.0, 0.0, 1.0)


def interp(points: list, t: float) -> float:
    """Piecewise-linear value at t from [[t0, v0], [t1, v1], ...]."""
    if not points:
        return 0.0
    if t <= points[0][0]:
        return points[0][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t <= t1:
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0) if t1 > t0 else v1
    return points[-1][1]


def tilt_at(spec: dict, minute: float) -> float:
    tilt = interp(spec.get("tilt_deg", [[0, 0]]), minute)
    c = spec.get("tilt_creep")                 # accelerating creep: velocity A / (tf - t) deg per minute
    if c and minute > c["start_min"]:
        tf, t0 = c["fail_min"], c["start_min"]
        m = min(minute, tf - 0.5)
        tilt += c["A"] * math.log((tf - t0) / (tf - m))
    return tilt


def reading_line(node: str, boot: int, seq: int, up_ms: int, moist: float, tilt_deg: float,
                 noise_deg: float, rng: random.Random, flags=(), accel_ok: bool = True) -> str:
    a = math.radians(tilt_deg + rng.gauss(0, noise_deg))
    g = (rng.gauss(0, 0.001), -math.cos(a), math.sin(a))
    m = max(0, min(1023, int(round(moist + rng.gauss(0, 2)))))
    return json.dumps({"v": 1, "type": "reading", "node": node, "boot": boot, "seq": seq, "up_ms": up_ms,
                       "moist_raw": m, "moist_min": max(0, m - 3), "moist_max": min(1023, m + 3),
                       "ax": round(g[0], 4) if accel_ok else None, "ay": round(g[1], 4) if accel_ok else None,
                       "az": round(g[2], 4) if accel_ok else None, "acc_units": "g",
                       "acc_model": "ADXL345" if accel_ok else "none", "acc_n": 20 if accel_ok else 0,
                       "fw": "sim-1.0", "flags": list(flags)})


def sim_calibration(node: str) -> NodeCalibration:
    return NodeCalibration(node, "scenario", 300, 700, UPRIGHT_G0, DOWNSLOPE, "ADXL345")


class ScenarioPlayer:
    """Plays a scenario file on the pipeline's (simulated) clock."""

    def __init__(self, pipeline, scenario: dict, weather_policy: dict):
        self.p = pipeline
        self.sc = scenario
        self.pol = weather_policy
        self.step_s = float(scenario.get("step_s", 60))
        self.start = pipeline.clock.now()
        self.rng = random.Random(scenario.get("seed", 1))
        self.boots = {n: self.rng.randint(1, 2 ** 31 - 1) for n in scenario.get("nodes", {})}
        self.seq = {n: 0 for n in self.boots}
        self.done = False
        for n in self.boots:
            pipeline.calibration_overrides[n] = sim_calibration(n)
        for t in scenario.get("remove_teams", []):
            if pipeline.ds.teams.pop(t, None):
                pipeline.log(f"scenario: team {t} removed (out of service)")
        pipeline.set_time_scale(self.step_s)

    def minute(self) -> float:
        return (self.p.clock.now() - self.start).total_seconds() / 60

    def weather_now(self):
        w = self.sc.get("weather")
        if not w:
            return None
        now = self.p.clock.now()
        series_start = self.start - timedelta(hours=w.get("past_hours", 0))
        snap = snapshot_from_scenario(self.p.s.site_id, w["hourly_mm"], series_start, now, self.pol)
        out = w.get("outage_after_min")
        if out is not None and self.minute() >= out:
            snap.stale = True
            snap.source += " (outage simulated: last data shown)"
            snap.error = "weather source unreachable (simulated)"
        return snap

    def step(self):
        m = self.minute()
        for node, spec in self.sc.get("nodes", {}).items():
            if any(a <= m < b for a, b in spec.get("dropout_min", [])):
                continue
            self.seq[node] += 1
            accel_ok = not any(a <= m < b for a, b in spec.get("accel_fault_min", []))
            line = reading_line(node, self.boots[node], self.seq[node], int(m * 60000) + 1000,
                                interp(spec.get("moisture", [[0, 320]]), m), tilt_at(spec, m),
                                spec.get("noise_deg", 0.02), self.rng,
                                () if accel_ok else ("accel_read_failed",), accel_ok)
            self.p.on_line(line, "scenario", simulated=True)

    def run(self, stop: threading.Event, tick_every_steps: int = 1, weather_every_min: float = 30):
        duration = self.sc.get("duration_min", 240)
        last_w = -1e9
        self.p.log(f"replay started: {self.sc.get('name')} ({duration} simulated minutes)")
        while not stop.is_set() and self.minute() <= duration:
            if self.minute() - last_w >= weather_every_min:
                snap = self.weather_now()
                if snap is not None:
                    self.p.on_weather(snap)
                last_w = self.minute()
            self.step()
            self.p.tick()
            if self.p.clock.frozen:            # headless/tests: step the clock exactly
                self.p.clock.advance(self.step_s)
            else:
                self.p.clock.sleep(self.step_s, stop)
        self.done = True
        self.p.log("replay finished; holding the final state")


class CalmNodes:
    """Live-mode filler: quiet readings for absent 'simulated-if-absent' stakes."""

    def __init__(self, pipeline):
        self.p = pipeline
        self.rng = random.Random(42)
        self.nodes = [n for n, c in pipeline.s.nodes.items() if c.kind == "simulated-if-absent"]
        self.boot = {n: self.rng.randint(1, 2 ** 31 - 1) for n in self.nodes}
        self.seq = {n: 0 for n in self.nodes}
        self.t0 = pipeline.clock.now()
        for n in self.nodes:
            pipeline.calibration_overrides[n] = sim_calibration(n)

    def run(self, stop: threading.Event):
        while not stop.is_set():
            for n in self.nodes:
                if self.p.node_is_live(n):
                    self.p.calibration_overrides.pop(n, None)   # a real stake took over this ID
                    continue
                self.seq[n] += 1
                up = int((self.p.clock.now() - self.t0).total_seconds() * 1000) + 1000
                self.p.on_line(reading_line(n, self.boot[n], self.seq[n], up, 360 + 10 * math.sin(self.seq[n] / 300),
                                            0.0, 0.01, self.rng), "simulated filler", simulated=True)
            stop.wait(1.0)


def main(argv=None) -> int:
    """Headless replay: runs a scenario at high speed and prints what the pipeline concluded."""
    import argparse
    from backend.clock import Clock
    from backend.db import Database
    from backend.pipeline import Pipeline
    from backend.settings import load_settings

    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("scenario")
    ap.add_argument("--no-claude", action="store_true")
    args = ap.parse_args(argv)
    sc = json.loads(Path(args.scenario).read_text())
    s = load_settings()
    if args.no_claude:
        s.hazard = dict(s.hazard, use_claude=False)
    clock = Clock(simulated=True)
    clock.freeze(datetime.now(timezone.utc).replace(microsecond=0))
    db = Database(":memory:")
    p = Pipeline(s, db, clock)
    stop = threading.Event()
    player = ScenarioPlayer(p, sc, s.rainfall)
    player.run(stop)
    clock.advance(-player.step_s)              # back to the last reading time
    p.tick(force=True)
    a = p.assessment
    plan = db.latest_plan()
    print(f"\n{sc.get('name')}: {a.risk_level if a else 'no assessment'} ({a.engine if a else '-'})")
    if a:
        print("zones:", ", ".join(f"{z.zone_id}={z.likelihood}" for z in a.zone_impacts))
        print("gaps:", a.coverage_gaps or "none")
        print("timing:", a.timing_status, a.timing_window or "")
        print("summary:", a.summary)
    if plan:
        print(f"plan {plan['plan_id']}: horizon {plan['horizon_basis']}, {len(plan['actions'])} exposed households,"
              f" {sum(1 for x in plan['actions'] if x['team_id'])} with a team, {len(plan['unresolved'])} unresolved")
    return 0


if __name__ == "__main__":
    sys.exit(main())

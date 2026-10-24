"""Section 01: slope node ingestion and sensor features.

Turns newline-delimited JSON from the Uno (docs/serial-protocol.md) into SensorReading
records, then derives calibrated SensorFeatures for the hazard engine.

    serial line -> parse_line() -> Gateway.ingest_line() -> SensorReading
    recent SensorReadings + NodeCalibration -> compute_features() -> SensorFeatures

This module never sends alerts or calls other section modules; backend/pipeline.py does
the wiring. It also has a small command line for bench work:

    python -m backend.sensors ports
    python -m backend.sensors monitor --port /dev/cu.usbmodem1101
    python -m backend.sensors monitor --replay tests/fixtures/sensor_packets.jsonl
    python -m backend.sensors calibrate --port /dev/cu.usbmodem1101 --step dry
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from backend.schemas import SensorFeatures, SensorReading, to_dict

PROTOCOL_VERSION = 1
BAUD = 115200

# flags that make one reading's moisture or tilt unusable (see docs/serial-protocol.md)
MOISTURE_BAD_FLAGS = {"moisture_unstable", "moisture_rail_high"}
ACCEL_BAD_FLAGS = {"accel_missing", "accel_unsupported_model", "accel_read_failed"}
COARSE_ACCEL_MODELS = {"MMA7660"}    # about 2.7 degrees per step: shows a hand tilt, not slow creep


def still_limit_g(model) -> float:
    """Largest per-axis standard deviation (g) that still counts as 'not moving' for this chip."""
    return 0.05 if model in COARSE_ACCEL_MODELS else 0.02
KNOWN_FLAGS = MOISTURE_BAD_FLAGS | ACCEL_BAD_FLAGS | {"accel_partial"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ================================================================ parsing
class PacketError(ValueError):
    """A line that is not a valid protocol record. It is counted and dropped."""


_REQUIRED = {
    "hello": {"v": int, "node": str, "boot": int, "up_ms": int, "fw": str, "acc_model": str},
    "reading": {"v": int, "node": str, "boot": int, "seq": int, "up_ms": int,
                "moist_raw": int, "moist_min": int, "moist_max": int,
                "acc_units": str, "acc_model": str, "acc_n": int, "fw": str, "flags": list},
}


def parse_line(line: str) -> dict:
    """Parse and type-check one line. Raises PacketError; never guesses missing fields."""
    line = line.strip()
    if not line:
        raise PacketError("empty line")
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        raise PacketError(f"not JSON: {e.msg}") from None
    if not isinstance(obj, dict):
        raise PacketError("not a JSON object")
    kind = obj.get("type")
    if kind not in _REQUIRED:
        raise PacketError(f"unknown type {kind!r}")
    for key, typ in _REQUIRED[kind].items():
        if key not in obj:
            raise PacketError(f"{kind}: missing {key}")
        val = obj[key]
        if typ is int and (isinstance(val, bool) or not isinstance(val, int)):
            raise PacketError(f"{kind}: {key} must be an integer")
        if typ is not int and not isinstance(val, typ):
            raise PacketError(f"{kind}: {key} must be {typ.__name__}")
    if obj["v"] != PROTOCOL_VERSION:
        raise PacketError(f"unsupported protocol version {obj['v']}")
    if not obj["node"]:
        raise PacketError("empty node id")
    if kind == "reading":
        if obj["seq"] < 1:
            raise PacketError("seq must start at 1")
        for k in ("moist_raw", "moist_min", "moist_max"):
            if not 0 <= obj[k] <= 1023:
                raise PacketError(f"{k} out of ADC range")
        axes = [obj.get(k, "missing") for k in ("ax", "ay", "az")]
        if "missing" in axes:
            raise PacketError("reading: missing ax/ay/az (use null when unavailable)")
        nulls = [a is None for a in axes]
        if any(nulls) and not all(nulls):
            raise PacketError("reading: partial acceleration vector")
        if not all(nulls):
            if not all(isinstance(a, (int, float)) and not isinstance(a, bool) for a in axes):
                raise PacketError("reading: ax/ay/az must be numbers or null")
            if not all(abs(a) < 20 for a in axes):
                raise PacketError("reading: acceleration outside sensor range")
        if obj["acc_units"] != "g":
            raise PacketError("reading: acc_units must be 'g'")
        if not all(isinstance(f, str) for f in obj["flags"]):
            raise PacketError("reading: flags must be strings")
    return obj


# ================================================================ gateway
@dataclass
class IngestResult:
    kind: str                                   # reading | hello | duplicate | invalid
    reading: Optional[SensorReading] = None
    hello: Optional[dict] = None
    detail: str = ""


@dataclass
class NodeInfo:
    node_id: str
    session_id: str
    firmware_version: str
    accel_model: str
    hello_time_utc: datetime


class Gateway:
    """Stateful line handler: sessions, duplicates and UTC timestamps.

    site_lookup maps node_id -> site_id (from config/site.json). Unknown nodes get
    site_id None rather than a guess.
    """

    def __init__(self, site_lookup: Optional[Mapping[str, str]] = None,
                 clock: Callable[[], datetime] = utcnow, simulated: bool = False,
                 max_remembered: int = 200_000):
        self.site_lookup = dict(site_lookup or {})
        self.clock = clock
        self.simulated = simulated
        self._anchor: dict[str, datetime] = {}          # session -> min(receipt - uptime)
        self._seen: set[tuple[str, int]] = set()
        self._seen_order: deque[tuple[str, int]] = deque()
        self._max = max_remembered
        self.nodes: dict[str, NodeInfo] = {}
        self.stats = {"reading": 0, "hello": 0, "duplicate": 0, "invalid": 0, "new_session": 0}
        self.unknown_flags: set[str] = set()

    def _session(self, node: str, boot: int) -> str:
        return f"{node}:{boot}"

    def _remember(self, key: tuple[str, int]) -> None:
        self._seen.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > self._max:
            self._seen.discard(self._seen_order.popleft())

    def ingest_line(self, line: str, receipt_time: Optional[datetime] = None) -> IngestResult:
        receipt = receipt_time or self.clock()
        if receipt.tzinfo is None:
            raise ValueError("receipt_time must be timezone-aware UTC")
        try:
            obj = parse_line(line)
        except PacketError as e:
            self.stats["invalid"] += 1
            return IngestResult("invalid", detail=str(e))

        session = self._session(obj["node"], obj["boot"])
        if session not in self._anchor:
            self.stats["new_session"] += 1
        anchor = receipt - timedelta(milliseconds=obj["up_ms"])
        if session not in self._anchor or anchor < self._anchor[session]:
            self._anchor[session] = anchor

        if obj["type"] == "hello":
            self.stats["hello"] += 1
            self.nodes[obj["node"]] = NodeInfo(obj["node"], session, obj["fw"], obj["acc_model"], receipt)
            return IngestResult("hello", hello=obj)

        key = (session, obj["seq"])
        if key in self._seen:
            self.stats["duplicate"] += 1
            return IngestResult("duplicate", detail=f"{session} seq {obj['seq']} already stored")
        self._remember(key)

        flags = tuple(obj["flags"])
        self.unknown_flags.update(f for f in flags if f not in KNOWN_FLAGS)
        accel = None if obj["ax"] is None else (float(obj["ax"]), float(obj["ay"]), float(obj["az"]))
        reading = SensorReading(
            node_id=obj["node"],
            site_id=self.site_lookup.get(obj["node"]),
            session_id=session,
            seq=obj["seq"],
            uptime_ms=obj["up_ms"],
            sample_time_utc=self._anchor[session] + timedelta(milliseconds=obj["up_ms"]),
            receipt_time_utc=receipt,
            moisture_raw=obj["moist_raw"],
            moisture_min=obj["moist_min"],
            moisture_max=obj["moist_max"],
            accel_g=accel,
            accel_units=obj["acc_units"],
            accel_model=obj["acc_model"],
            accel_samples=obj["acc_n"],
            firmware_version=obj["fw"],
            protocol_version=obj["v"],
            health_flags=flags,
            simulated=self.simulated,
        )
        self.stats["reading"] += 1
        return IngestResult("reading", reading=reading)


# ================================================================ calibration
@dataclass
class NodeCalibration:
    node_id: str
    version: Optional[str]
    dry_raw: Optional[float] = None
    wet_raw: Optional[float] = None
    baseline_g: Optional[tuple[float, float, float]] = None
    downslope_axis: Optional[tuple[float, float, float]] = None
    accel_model: Optional[str] = None


def load_calibration(path: str | Path) -> dict[str, NodeCalibration]:
    """Read config/calibration.json. Missing values stay None (uncalibrated), never 0."""
    data = json.loads(Path(path).read_text())
    out = {}
    for node, c in data.get("nodes", {}).items():
        m, t = c.get("moisture", {}), c.get("tilt", {})
        out[node] = NodeCalibration(
            node_id=node,
            version=data.get("version"),
            dry_raw=m.get("dry_raw"),
            wet_raw=m.get("wet_raw"),
            baseline_g=tuple(t["baseline_g"]) if t.get("baseline_g") else None,
            downslope_axis=tuple(t["downslope_axis"]) if t.get("downslope_axis") else None,
            accel_model=c.get("accel_model"),
        )
    return out


# ================================================================ features
def _dot(a, b): return sum(x * y for x, y in zip(a, b))
def _norm(a): return math.sqrt(_dot(a, a))
def _unit(a):
    n = _norm(a)
    return tuple(x / n for x in a) if n > 0 else None


def tilt_from_baseline(g, g0) -> float:
    """Angle in degrees between gravity now and the installation baseline (any mounting)."""
    cx = g[1] * g0[2] - g[2] * g0[1]
    cy = g[2] * g0[0] - g[0] * g0[2]
    cz = g[0] * g0[1] - g[1] * g0[0]
    return math.degrees(math.atan2(math.sqrt(cx * cx + cy * cy + cz * cz), _dot(g, g0)))


def downslope_tilt(g, g0, axis) -> float:
    """Signed lean in degrees along the recorded downslope axis (+ = downslope)."""
    return math.degrees(math.atan2(_dot(g, axis), _dot(g, _unit(g0))))


def _slope_per_hour(points: list[tuple[datetime, float]]) -> Optional[float]:
    if len(points) < 3:
        return None
    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 3600 for t, _ in points]
    ys = [v for _, v in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def compute_features(readings: Iterable[SensorReading], calibration: Optional[NodeCalibration],
                     node_id: str, now: datetime, window_s: float = 600, stale_after_s: float = 10,
                     min_trend_span_s: float = 60, recent_n: int = 5) -> SensorFeatures:
    """Calibrated wetness and tilt for one node over a recent window.

    Quality is 'unavailable' with no fresh data, 'degraded' when anything needed is
    missing or flagged, and 'ok' otherwise. A fault never produces a calm-looking value.
    """
    win = sorted((r for r in readings if r.node_id == node_id
                  and now - timedelta(seconds=window_s) <= r.sample_time_utc <= now + timedelta(seconds=5)),
                 key=lambda r: r.sample_time_utc)
    reasons: list[str] = []
    info: list[str] = []
    cal_version = calibration.version if calibration else None

    if not win:
        return SensorFeatures(node_id, None, now, None, None, 0, (), None, None, cal_version, None,
                              None, None, None, None, None, None, "unavailable", ("no_readings_in_window",))

    latest = win[-1]
    age = (now - latest.receipt_time_utc).total_seconds()
    sessions = tuple(dict.fromkeys(r.session_id for r in win))
    if len(sessions) > 1:
        info.append("node_restarted_in_window")
    if age > stale_after_s:
        reasons.append("stale_data")
    if calibration is None:
        reasons.append("node_not_in_calibration")
    flagged = sum(1 for r in win if r.health_flags)
    if flagged:
        reasons.append(f"health_flags_in_window:{flagged}/{len(win)}")
    span_ok = (win[-1].sample_time_utc - win[0].sample_time_utc).total_seconds() >= min_trend_span_s

    # ---- wetness
    moist = [r for r in win if not (set(r.health_flags) & MOISTURE_BAD_FLAGS)]
    moisture_raw = wetness = wet_trend = None
    if not moist:
        reasons.append("no_usable_moisture")
    else:
        moisture_raw = sum(r.moisture_raw for r in moist[-recent_n:]) / len(moist[-recent_n:])
        dry = calibration.dry_raw if calibration else None
        wet = calibration.wet_raw if calibration else None
        if dry is None or wet is None:
            reasons.append("moisture_uncalibrated")
        elif wet == dry:
            reasons.append("moisture_calibration_invalid")
        else:
            idx = lambda raw: (raw - dry) / (wet - dry)
            wetness = idx(moisture_raw)
            if wetness < -0.05 or wetness > 1.05:
                info.append("wetness_outside_calibrated_range")
            if span_ok:
                wet_trend = _slope_per_hour([(r.sample_time_utc, idx(r.moisture_raw)) for r in moist])

    # ---- tilt
    acc = [r for r in win if r.accel_g is not None and not (set(r.health_flags) & ACCEL_BAD_FLAGS)]
    tilt = tilt_ds = tilt_trend = None
    if any("accel_partial" in r.health_flags for r in win):
        reasons.append("accel_partial_samples")
    if not acc:
        reasons.append("no_usable_acceleration")
    elif calibration is None or calibration.baseline_g is None:
        reasons.append("tilt_baseline_missing")
    elif calibration.accel_model and acc[-1].accel_model != calibration.accel_model:
        reasons.append("accel_model_differs_from_calibration")
    else:
        g0 = calibration.baseline_g
        tilts = [(r.sample_time_utc, tilt_from_baseline(r.accel_g, g0)) for r in acc]
        recent = tilts[-recent_n:]
        tilt = sum(v for _, v in recent) / len(recent)
        if span_ok:
            tilt_trend = _slope_per_hour(tilts)
        if acc[-1].accel_model in COARSE_ACCEL_MODELS:
            info.append("coarse_accelerometer_about_2.7_deg_steps")
        if calibration.downslope_axis:
            vals = [downslope_tilt(r.accel_g, g0, calibration.downslope_axis) for r in acc[-recent_n:]]
            tilt_ds = sum(vals) / len(vals)
        else:
            info.append("downslope_axis_missing")

    if not span_ok:
        info.append("window_too_short_for_trend")
    quality = "degraded" if reasons else "ok"
    if "stale_data" in reasons and age > 3 * stale_after_s:
        quality = "unavailable"
    return SensorFeatures(
        node_id=node_id, site_id=latest.site_id, computed_at_utc=now,
        window_start_utc=win[0].sample_time_utc, window_end_utc=latest.sample_time_utc,
        reading_count=len(win), session_ids=sessions, first_seq=win[0].seq, last_seq=latest.seq,
        calibration_version=cal_version, latest_reading_age_s=age,
        moisture_raw=moisture_raw, wetness_index=wetness, wetness_trend_per_h=wet_trend,
        tilt_deg=tilt, tilt_downslope_deg=tilt_ds, tilt_trend_deg_per_h=tilt_trend,
        quality=quality, quality_reasons=tuple(reasons + ["info:" + i for i in info]),
        simulated=any(r.simulated for r in win),
    )


# ================================================================ serial reader
class SerialReader:
    """Blocking reader for a background thread. Reconnects; never crashes the backend.

    on_result gets every IngestResult; on_status gets short strings such as
    'connected', 'disconnected: ...'. backend/app.py starts this as a managed task.
    """

    def __init__(self, port: str, gateway: Gateway, on_result: Callable[[IngestResult], None],
                 on_status: Callable[[str], None] = lambda s: None, baud: int = BAUD,
                 retry_s: float = 2.0):
        self.port, self.gateway, self.on_result, self.on_status = port, gateway, on_result, on_status
        self.baud, self.retry_s = baud, retry_s

    def run(self, stop: threading.Event) -> None:
        import serial  # pyserial; imported here so tests run without it
        while not stop.is_set():
            try:
                with serial.Serial(self.port, self.baud, timeout=1) as ser:
                    self.on_status(f"connected {self.port}")
                    time.sleep(0.2)
                    ser.reset_input_buffer()          # drop a partial line from before we connected
                    ser.write(b"?\n")                 # ask for a hello (R4 does not reset on open)
                    quiet = 0                         # seconds in a row with nothing received
                    while not stop.is_set():
                        raw = ser.readline()
                        if not raw:
                            quiet += 1
                            if quiet == 5:
                                self.on_status(f"{self.port} is open but has sent nothing for 5 s. Is the SlopeCast "
                                               "firmware uploaded, and is the Arduino IDE Serial Monitor closed?")
                                ser.write(b"?\n")
                            continue
                        quiet = 0
                        line = raw.decode("utf-8", errors="replace")
                        self.on_result(self.gateway.ingest_line(line, utcnow()))
            except (OSError, serial.SerialException) as e:
                self.on_status(f"disconnected: {e}")
                stop.wait(self.retry_s)


# ================================================================ command line
def _site_lookup(path: Path) -> dict[str, str]:
    """Best-effort node -> site map from config/site.json ({"nodes": {"N01": {"site_id": ...}}})."""
    try:
        data = json.loads(path.read_text())
        site = data.get("site_id")
        return {n: (v.get("site_id", site) if isinstance(v, dict) else site)
                for n, v in data.get("nodes", {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _collect(port: str, seconds: float, node: str) -> list[SensorReading]:
    gw = Gateway()
    out: list[SensorReading] = []
    stop = threading.Event()

    def on_result(res: IngestResult):
        if res.kind == "reading" and res.reading.node_id == node:
            out.append(res.reading)
            print(f"  seq {res.reading.seq}: moist {res.reading.moisture_raw} accel {res.reading.accel_g} "
                  f"flags {list(res.reading.health_flags)}")

    t = threading.Thread(target=SerialReader(port, gw, on_result, print).run, args=(stop,), daemon=True)
    t.start()
    time.sleep(seconds)
    stop.set()
    t.join(timeout=3)
    return out


def _calibrate(args) -> int:
    path = Path(args.config)
    data = json.loads(path.read_text())
    node = data.setdefault("nodes", {}).setdefault(args.node, {"moisture": {}, "tilt": {}})
    node.setdefault("moisture", {}); node.setdefault("tilt", {})
    hold = "Hold it leaning 3-10 degrees downslope" if args.step == "downslope" else "Keep the stake still"
    print(f"Collecting {args.seconds:.0f} s from {args.port} for step '{args.step}'. {hold}.")
    rs = _collect(args.port, args.seconds, args.node)
    if len(rs) < 0.7 * args.seconds:
        print(f"Only {len(rs)} readings; expected about {args.seconds:.0f}. Not saved.")
        return 1
    now = utcnow().isoformat()
    if args.step in ("dry", "wet"):
        good = [r.moisture_raw for r in rs if not (set(r.health_flags) & MOISTURE_BAD_FLAGS)]
        if len(good) < 0.7 * len(rs):
            print("Too many flagged moisture readings (sensor unplugged or unstable?). Not saved.")
            return 1
        mean = sum(good) / len(good)
        spread = max(good) - min(good)
        node["moisture"][f"{args.step}_raw"] = round(mean, 1)
        node["moisture"][f"{args.step}_captured_at"] = now
        print(f"{args.step}_raw = {mean:.1f} (spread {spread})")
        m = node["moisture"]
        if m.get("dry_raw") is not None and m.get("wet_raw") is not None and m["wet_raw"] - m["dry_raw"] < 50:
            print("Warning: wet and dry differ by under 50 counts; the wetness index will be noisy.")
    else:
        good = [r for r in rs if r.accel_g and not (set(r.health_flags) & ACCEL_BAD_FLAGS)]
        if len(good) < 0.7 * len(rs):
            print("Too many failed accelerometer readings. Not saved.")
            return 1
        g = tuple(sum(r.accel_g[i] for r in good) / len(good) for i in range(3))
        sd = max(math.sqrt(sum((r.accel_g[i] - g[i]) ** 2 for r in good) / len(good)) for i in range(3))
        if sd > still_limit_g(good[-1].accel_model):
            print(f"Stake was moving (axis std {sd:.3f} g). Hold it still and retry. Not saved.")
            return 1
        if not 0.8 < _norm(g) < 1.2:
            print(f"|g| = {_norm(g):.3f}; expected about 1. Check the accelerometer. Not saved.")
            return 1
        if args.step == "baseline":
            node["tilt"]["baseline_g"] = [round(v, 5) for v in g]
            node["tilt"]["baseline_captured_at"] = now
            node["tilt"]["downslope_axis"] = None           # old axis belongs to the old baseline
            node["accel_model"] = good[-1].accel_model
            print(f"baseline_g = {node['tilt']['baseline_g']} ({good[-1].accel_model}, std {sd:.4f} g)")
        else:
            g0 = node["tilt"].get("baseline_g")
            if not g0:
                print("Capture --step baseline first. Not saved.")
                return 1
            u0 = _unit(g0)
            proj = tuple(g[i] - _dot(g, u0) * u0[i] for i in range(3))
            lean = tilt_from_baseline(g, g0)
            if lean < 2:
                print(f"Lean was only {lean:.1f} degrees; lean the stake 3-10 degrees downslope. Not saved.")
                return 1
            node["tilt"]["downslope_axis"] = [round(v, 5) for v in _unit(proj)]
            node["tilt"]["downslope_captured_at"] = now
            print(f"downslope_axis = {node['tilt']['downslope_axis']} (lean {lean:.1f} degrees). Stand it back up.")
    data["version"] = "cal-" + utcnow().strftime("%Y%m%dT%H%M%SZ")
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Saved {path} as version {data['version']}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m backend.sensors", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports", help="list serial ports")
    m = sub.add_parser("monitor", help="print parsed records and live features")
    src = m.add_mutually_exclusive_group(required=True)
    src.add_argument("--port")
    src.add_argument("--replay", help="read a .jsonl file instead of a serial port")
    m.add_argument("--node", default="N01")
    c = sub.add_parser("calibrate", help="capture dry/wet moisture or the tilt baseline")
    c.add_argument("--port", required=True)
    c.add_argument("--step", required=True, choices=["dry", "wet", "baseline", "downslope"])
    c.add_argument("--node", default="N01")
    c.add_argument("--seconds", type=float, default=15)
    for sp in (m, c):
        sp.add_argument("--config", default="config/calibration.json")
        sp.add_argument("--site-config", default="config/site.json")
    args = p.parse_args(argv)

    if args.cmd == "ports":
        from serial.tools import list_ports
        for port in list_ports.comports():
            print(f"{port.device}\t{port.description}")
        return 0
    if args.cmd == "calibrate":
        return _calibrate(args)

    cal = load_calibration(args.config).get(args.node) if Path(args.config).exists() else None
    gw = Gateway(_site_lookup(Path(args.site_config)), simulated=bool(args.replay))
    history: deque[SensorReading] = deque(maxlen=3600)

    def show(res: IngestResult):
        if res.kind != "reading":
            print(f"[{res.kind}] {res.detail or res.hello or ''}")
            return
        history.append(res.reading)
        now = res.reading.receipt_time_utc
        f = compute_features(history, cal, args.node, now)
        fmt = lambda v, s="{:.3f}": "-" if v is None else s.format(v)
        print(f"seq {res.reading.seq:5d} moist {res.reading.moisture_raw:4d} wet {fmt(f.wetness_index, '{:.2f}')} "
              f"tilt {fmt(f.tilt_deg)} deg trend {fmt(f.tilt_trend_deg_per_h)} deg/h  {f.quality} "
              f"{' '.join(f.quality_reasons)}")

    if args.replay:
        t = utcnow()
        for i, line in enumerate(Path(args.replay).read_text().splitlines()):
            show(gw.ingest_line(line, t + timedelta(seconds=i)))
        print(gw.stats)
        return 0
    stop = threading.Event()
    try:
        SerialReader(args.port, gw, show, lambda s: print(f"[serial] {s}")).run(stop)
    except KeyboardInterrupt:
        print(gw.stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())

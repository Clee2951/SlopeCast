"""Start SlopeCast:  uv run python -m backend [options]

    uv run python -m backend                         live: Arduino over USB + Apify weather
    uv run python -m backend --replay tests/fixtures/scenarios/escalating.json
    uv run python -m backend --replay <file> --speed 120     faster replay

Then open http://localhost:8000 (links for each view are printed at startup).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import App, serve  # noqa: E402
from backend.clock import Clock  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.settings import SettingsError, load_settings  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backend", description="SlopeCast backend and dashboard")
    ap.add_argument("--replay", help="scenario JSON file to replay instead of live hardware")
    ap.add_argument("--speed", type=float, help="replay speed: simulated seconds per real second (default from the scenario, usually 60)")
    ap.add_argument("--serial", help="comma-separated serial ports (default: auto-detect Arduinos)")
    ap.add_argument("--http-port", type=int, help="web port (default 8000)")
    ap.add_argument("--no-claude", action="store_true", help="use the rule-based hazard engine only")
    ap.add_argument("--no-filler", action="store_true", help="live mode: don't simulate the absent stakes N02/N03")
    ap.add_argument("--keep", action="store_true", help="keep readings and plans from the last run (default: start clean)")
    args = ap.parse_args(argv)

    try:
        s = load_settings()
    except SettingsError as e:
        print(f"Configuration problem: {e}")
        return 2
    if args.serial:
        s.serial_ports = [p.strip() for p in args.serial.split(",") if p.strip()]
    if args.http_port:
        s.http_port = args.http_port
    if args.no_claude:
        s.hazard = dict(s.hazard, use_claude=False)
    if args.no_filler:
        s.extra_nodes = "off"

    scenario = None
    if args.replay:
        scenario = json.loads(Path(args.replay).read_text())
        speed = args.speed or scenario.get("speed", 60)
        clock = Clock(simulated=True, speed=speed)
        db = Database(s.root / "var" / "replay.db")
    else:
        clock = Clock()
        db = Database(s.db_path)
    if not args.keep:
        db.reset_runtime()

    app = App(s, db, clock, scenario=scenario)
    try:
        httpd = serve(app, s.http_host, s.http_port)
    except OSError as e:
        print(f"Could not open port {s.http_port}: {e}. Try --http-port 8001.")
        return 2
    app.start_tasks()
    base = f"http://localhost:{s.http_port}"
    print(f"\nSlopeCast running in {app.mode.upper()} mode"
          + (f": {scenario.get('name')} at {clock.speed:g}x" if scenario else ""))
    print(f"  Claude hazard engine: {'on (' + s.hazard['model'] + ')' if s.anthropic_api_key and s.hazard.get('use_claude', True) else 'off (rule-based)'}")
    if not scenario:
        print(f"  Weather: {s.rainfall.get('source')}" + ("" if s.apify_token else " (APIFY_TOKEN not set: using fallback)"))
    print(f"\n  Dashboard (operator):  {base}/?view=dashboard&token=operator-demo")
    print(f"  Rescue committee:      {base}/?view=committee&token=committee-demo")
    print(f"  Resident (household H02): {base}/?view=resident&token=resident-h02")
    print(f"  Public dashboard:      {base}/?view=dashboard\n  Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        app.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

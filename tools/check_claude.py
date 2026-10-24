"""Check that your Claude API key works with SlopeCast's hazard engine, without hardware.

    uv run python tools/check_claude.py

Sends one sample assessment (stake N01 tilting and wet, heavy rain) to the model named in
config/hazard-policy.json, using ANTHROPIC_API_KEY from .env, and prints what Claude
answered and what SlopeCast's checks did with it. Costs one small API call.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.hazard import HazardEngine, call_claude_http  # noqa: E402
from backend.households import load_files  # noqa: E402
from backend.schemas import SensorFeatures  # noqa: E402
from backend.settings import load_settings  # noqa: E402

HINTS = {
    "401": "The key was rejected. Copy it again from the Console (it starts with sk-ant-) into .env as ANTHROPIC_API_KEY=..., no quotes or spaces.",
    "403": "The key is valid but not allowed to do this. Check its workspace and your role in the Console.",
    "workspace": "Your key is not scoped to a workspace. Either create a key scoped to a workspace, or put the workspace ID in .env as ANTHROPIC_WORKSPACE_ID=wrkspc_...",
    "404": "Model not found. Put a model your account can use in config/hazard-policy.json -> \"model\" (see the Models overview in the docs).",
    "credit": "Your account has no credit. Add credits or a payment method in the Console's billing settings (or redeem hackathon credits).",
    "429": "Rate limited. Wait a minute, or raise min_interval_s in config/hazard-policy.json so SlopeCast calls less often.",
    "timed out": "No answer in time. Check your internet, or raise timeout_s in config/hazard-policy.json.",
}


def feature(node, wet, tilt, rate, now):
    return SensorFeatures(node, "demo", now, now, now, 60, (f"{node}:1",), 1, 60, "check", 1.0,
                          300 + 400 * wet, wet, 0.1, tilt, tilt, rate, "ok", (), False)


def main() -> int:
    s = load_settings()
    key = s.anthropic_api_key
    print(f"Model:      {s.hazard.get('model')}")
    print(f"API key:    {'set (' + key[:10] + '...' + key[-4:] + ')' if key else 'NOT SET'}")
    print(f"Workspace:  {s.anthropic_workspace_id or '(none; fine if your key is scoped to a workspace)'}")
    if not key:
        print("\nAdd ANTHROPIC_API_KEY=sk-ant-... to the .env file in the repo root, then run this again.")
        return 1
    now = datetime.now(timezone.utc)
    ds = load_files(s.data_paths, s.site["center"])
    feats = {"N01": feature("N01", 0.92, 5.0, 25.0, now), "N02": feature("N02", 0.6, 0.2, 0.1, now),
             "N03": feature("N03", 0.3, 0.0, 0.0, now)}
    eng = HazardEngine(s)
    evidence, nodes, view, fits = eng.build_evidence(feats, {}, None, ds.zones, now)
    print("\nCalling Claude...")
    t0 = time.time()
    try:
        answer = call_claude_http(key, s.hazard["model"], eng.prompt, evidence, s.hazard.get("max_tokens", 1500),
                                  s.hazard.get("timeout_s", 30), s.anthropic_workspace_id)
    except Exception as e:
        msg = str(e)
        print(f"FAILED: {type(e).__name__}: {msg}")
        for k, hint in HINTS.items():
            if k in msg.lower() or k in msg:
                print("Fix: " + hint)
                break
        print("SlopeCast still runs without Claude: it falls back to the rule-based engine and labels it on the dashboard.")
        return 1
    print(f"OK in {time.time() - t0:.1f} s\n")
    print("Claude's answer:")
    print(f"  risk level: {answer.get('risk_level')}")
    for z in answer.get("zones", []):
        print(f"  {z.get('zone_id')}: {z.get('likelihood')}  evidence {z.get('evidence_nodes')}  {'; '.join(z.get('reasons', []))[:140]}")
    print(f"  timing: {answer.get('timing')}")
    print(f"  summary: {answer.get('summary')}")
    from backend.hazard import validate
    level, impacts, gaps, tstatus, twin, summary, notes = validate(answer, view, nodes, ds.zones, fits)
    print("\nAfter SlopeCast's checks:")
    print(f"  risk level {level}; zones " + ", ".join(f"{i.zone_id}={i.likelihood}" for i in impacts if i.likelihood != "low"))
    print(f"  timing {tstatus}; gaps {gaps}")
    print(f"  notes: {notes or 'none (answer accepted as given)'}")
    print("\nThe hazard engine is ready. Start SlopeCast and the dashboard banner will say 'assessed by Claude'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

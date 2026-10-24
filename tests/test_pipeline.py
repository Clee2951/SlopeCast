"""End to end: a replayed scenario through ingestion, hazard (fake Claude), people, planner,
approval, simulated messages and acknowledgement. All views must reference one plan."""
import json
import threading
from pathlib import Path

import pytest

from backend.app import committee_queue, dashboard, resident_plan
from backend.pipeline import Conflict, Pipeline
from backend.security import Auth
from tools.replay import ScenarioPlayer

SCEN = Path(__file__).parent / "fixtures" / "scenarios"


class FakeApp:
    def __init__(self, p, settings):
        self.pipeline = p
        self.mode = "replay"
        self.auth = Auth(settings.data_paths["users"])

    def replay_status(self):
        return None


def fake_claude(system, evidence):
    """Echo the rule view, the way a well-behaved model would, so the test is deterministic."""
    zones = [{"zone_id": z, "likelihood": lk, "evidence_nodes": ["N01"] if lk != "low" else [], "reasons": ["test"]}
             for z, lk in evidence["rules"]["zone_likelihoods"].items()]
    return {"risk_level": evidence["rules"]["minimum_risk_level"], "zones": zones,
            "coverage_gaps": evidence["rules"]["coverage_gaps"], "timing": {"status": "unavailable"},
            "summary": "test summary"}


def run(settings, db, clock, name):
    from backend.hazard import HazardEngine
    p = Pipeline(settings, db, clock, hazard=HazardEngine(settings, call_claude=fake_claude))
    sc = json.loads((SCEN / f"{name}.json").read_text())
    ScenarioPlayer(p, sc, settings.rainfall).run(threading.Event())
    return p, sc


@pytest.mark.parametrize("name", ["normal", "escalating", "assistance-needed", "urgent-review", "data-outage"])
def test_every_scenario_runs(settings, db, clock, name):
    p, sc = run(settings, db, clock, name)
    assert p.assessment is not None
    assert db.latest_plan() is not None
    assert all(r.simulated for r in p.history["N01"])


def test_normal_is_calm(settings, db, clock):
    p, _ = run(settings, db, clock, "normal")
    assert p.assessment.risk_level == "monitoring"
    assert db.latest_plan()["actions"] == []


def test_escalating_full_loop(settings, db, clock):
    p, _ = run(settings, db, clock, "escalating")
    a = p.assessment
    assert a.engine.startswith("claude:")
    assert {z.zone_id: z.likelihood for z in a.zone_impacts}["Z1"] == "high"
    plan = db.latest_plan()
    ids = {x["household_id"] for x in plan["actions"]}
    assert "H02" in ids

    # stale version rejected, current approved, messages queued once
    with pytest.raises(Conflict):
        p.approve_plan(plan["plan_id"], plan["version"] - 1, "operator")
    p.approve_plan(plan["plan_id"], plan["version"], "operator")
    p.approve_plan(plan["plan_id"], plan["version"], "operator")
    assert len(db.notifications()) == len(plan["actions"])

    # committee accepts the van job, resident acknowledges, team arrives, evacuated
    h02 = next(x for x in db.latest_plan("approved")["assignments"] if x["household_id"] == "H02")
    p.accept_assignment(h02["assignment_id"], "committee")
    assert p.record_progress("acknowledged", "H02", None, "resident", "ev1")
    assert not p.record_progress("acknowledged", "H02", None, "resident", "ev1")    # repeat click
    p.record_progress("team_arrived", "H02", "T2", "committee", "ev2")
    p.record_progress("evacuated", "H02", "T2", "committee", "ev3")
    assert db.get_assignment(h02["assignment_id"])["state"] == "done"

    # every view points at the same approved plan
    app = FakeApp(p, settings)
    op = app.auth.identify("operator-demo")
    d = dashboard(app, op)
    q = committee_queue(app, app.auth.identify("committee-demo"))
    r = resident_plan(app, app.auth.identify("resident-h02"))
    assert d["approved_plan"]["plan_id"] == q["approved_plan"]["plan_id"] == r["plan_id"]
    assert "acknowledged" in r["progress"] and "evacuated" in r["progress"]
    assert db.notifications("H02")[0]["status"] == "acknowledged"


def test_assistance_needed_flags_infeasible(settings, db, clock):
    p, _ = run(settings, db, clock, "assistance-needed")
    plan = db.latest_plan()
    h02 = next(x for x in plan["actions"] if x["household_id"] == "H02")
    assert h02["feasible"] is False                    # the only wheelchair van is out of service


def test_urgent_review_has_experimental_timing(settings, db, clock):
    p, _ = run(settings, db, clock, "urgent-review")
    assert p.assessment.risk_level == "urgent_review"
    assert p.assessment.timing_status == "experimental"
    assert db.latest_plan()["horizon_basis"] == "hazard-timing"


def test_data_outage_is_degraded_not_safe(settings, db, clock):
    p, _ = run(settings, db, clock, "data-outage")
    gaps = p.assessment.coverage_gaps
    assert any(g.startswith("N03") for g in gaps)
    assert any(g.startswith("weather") for g in gaps)
    assert p.assessment.quality == "degraded"

"""Section 06 tests: leave-by arithmetic, horizon choice, team capacity, infeasible jobs,
and accepted jobs surviving a replan."""
from datetime import datetime, timedelta

from backend.people import evaluate
from backend.planner import build_plan, choose_horizon
from backend.schemas import HazardAssessment, HouseholdNeeds, ZoneImpact
from tests.conftest import T0


def assessment(level="urgent_review", timing=None, zones=None):
    return HazardAssessment("HA-1", "liugui-demo", T0, "rules-only", {}, level,
                            [ZoneImpact(z, lk, [], []) for z, lk in (zones or {"Z1": "high"}).items()], [],
                            "experimental" if timing else "unavailable", timing, None, "", "ok", [], "v", None)


def need(hid="H02", prep=30, travel=10, team=False, wc=0):
    return HouseholdNeeds(hid, "HA-1", "Z1", "high", True, 2, [], team, wc, [], prep, travel, "S1", "open", [])


def test_leave_by_arithmetic(settings, ds):
    pol = dict(settings.evacuation, safety_buffer_minutes=15)
    horizon = T0 + timedelta(minutes=120)
    plan = build_plan(assessment(), [need(prep=30, travel=10)], ds, [], pol, T0, 1, operator_horizon=horizon)
    act = plan.actions[0]
    assert plan.horizon_basis == "operator"
    assert act.leave_by_utc == horizon - timedelta(minutes=15 + 10)          # 13:35
    assert act.prepare_by_utc == act.leave_by_utc - timedelta(minutes=30)    # 13:05
    assert act.feasible


def test_horizon_priority(settings):
    t = {"earliest_utc": (T0 + timedelta(minutes=40)).isoformat(), "latest_utc": None, "basis": "fit"}
    assert choose_horizon(assessment(timing=t), T0 + timedelta(hours=2), settings.evacuation, T0)[1] == "hazard-timing"
    assert choose_horizon(assessment(), T0 + timedelta(hours=2), settings.evacuation, T0)[1] == "operator"
    assert choose_horizon(assessment(), None, settings.evacuation, T0)[1] == "policy-default"
    assert choose_horizon(assessment("monitoring"), None, settings.evacuation, T0) == (None, "none")


def test_no_horizon_means_no_invented_times(settings, ds):
    pol = dict(settings.evacuation, default_horizon_minutes={})
    plan = build_plan(assessment(), [need()], ds, [], pol, T0, 1)
    assert plan.horizon_basis == "none"
    assert plan.actions[0].leave_by_utc is None
    assert any("no deadline" in r for r in plan.actions[0].reasons)


def test_anchored_default_does_not_slide(settings, ds):
    anchor = T0 + timedelta(minutes=60)
    later = T0 + timedelta(minutes=20)
    plan = build_plan(assessment(), [need()], ds, [], settings.evacuation, later, 2, anchored_default=anchor)
    assert plan.horizon_utc == anchor


def test_wheelchair_household_gets_the_van(settings, ds):
    plan = build_plan(assessment(), [need(team=True, wc=1)], ds, [], settings.evacuation, T0, 1,
                      operator_horizon=T0 + timedelta(hours=3))
    assert plan.actions[0].team_id == "T2"
    assert plan.assignments[0].state == "proposed"


def test_no_van_means_infeasible(settings, ds):
    ds.teams.pop("T2")
    plan = build_plan(assessment(), [need(team=True, wc=1)], ds, [], settings.evacuation, T0, 1,
                      operator_horizon=T0 + timedelta(hours=3))
    assert plan.actions[0].feasible is False
    assert plan.unresolved and "vehicle" in plan.unresolved[0]


def test_deadline_too_close_is_infeasible(settings, ds):
    plan = build_plan(assessment(), [need(prep=30, travel=10)], ds, [], settings.evacuation, T0, 1,
                      operator_horizon=T0 + timedelta(minutes=20))
    assert plan.actions[0].feasible is False


def test_accepted_job_survives_replan(settings, ds):
    accepted = [{"assignment_id": "A-H02-T1", "team_id": "T1", "household_id": "H02", "state": "accepted",
                 "accepted_by": "committee"}]
    plan = build_plan(assessment(), [need(team=True, wc=0)], ds, accepted, settings.evacuation, T0, 2,
                      operator_horizon=T0 + timedelta(hours=3))
    assert plan.actions[0].team_id == "T1"
    assert plan.assignments[0].state == "accepted"


def test_teams_are_shared_by_deadline(settings, ds):
    ds.teams = {"T2": ds.teams["T2"]}                  # one van for two wheelchair households
    needs = [need("H02", team=True, wc=1), need("H21", team=True, wc=1)]
    plan = build_plan(assessment(), needs, ds, [], settings.evacuation, T0, 1, operator_horizon=T0 + timedelta(hours=4))
    assert {a.team_id for a in plan.actions} == {"T2"}
    arrivals = sorted(a.arrival_by_utc for a in plan.actions)
    assert arrivals[0] <= arrivals[1]


def test_demo_plan_end_to_end(settings, ds):
    a = assessment(zones={"Z1": "high", "Z4": "medium"})
    needs = evaluate(a, ds, settings.evacuation)
    plan = build_plan(a, needs, ds, [], settings.evacuation, T0, 1)
    ids = {x.household_id for x in plan.actions}
    assert "H02" in ids
    h02 = next(x for x in plan.actions if x.household_id == "H02")
    assert h02.team_id == "T2" and h02.leave_by_utc is not None

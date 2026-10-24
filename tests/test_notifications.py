"""Section 07 tests: approval gate, no duplicate messages, language fallback, honest
wording about help, and acknowledgements."""
import pytest

from backend.notifications import NotApproved, acknowledge, on_plan_approved, render
from backend.schemas import Household
from tests.conftest import T0


def plan(state="approved"):
    return {"plan_id": "PLAN-00001", "version": 1, "state": state, "actions": [
        {"household_id": "H02", "zone_id": "Z1", "destination_id": "S1", "leave_by_utc": "2026-10-24T13:40:00+00:00"},
        {"household_id": "H17", "zone_id": "Z1", "destination_id": "S2", "leave_by_utc": None}]}


NEEDS = {"H02": {"needs_team": True, "checklist": ["daily medication", "ID documents"]},
         "H17": {"needs_team": False, "checklist": ["ID documents"]}}


def test_unapproved_plan_cannot_send(settings, ds, db):
    with pytest.raises(NotApproved):
        on_plan_approved(plan("draft"), ds, NEEDS, {}, settings.messages, 8, db, T0)
    assert db.notifications() == []


def test_approving_twice_sends_once(settings, ds, db):
    first = on_plan_approved(plan(), ds, NEEDS, {}, settings.messages, 8, db, T0)
    second = on_plan_approved(plan(), ds, NEEDS, {}, settings.messages, 8, db, T0)
    assert len(first) == 2 and second == []
    assert len(db.notifications()) == 2


def test_leave_by_in_local_time_and_honest_help(settings, ds):
    hh = Household("H02", "B01", "Test", 2, "en")
    _, text = render(plan()["actions"][0], hh, NEEDS["H02"], "Liugui Elementary", "North gully", None, None, settings.messages, 8)
    assert "21:40" in text                             # 13:40 UTC is 21:40 in Taiwan
    assert "not yet confirmed" in text                 # help requested, not promised


def test_accepted_help_is_named(settings):
    hh = Household("H02", "B01", "Test", 2, "en")
    asg = {"team_id": "T2", "state": "accepted", "arrival_by_utc": "2026-10-24T13:20:00+00:00"}
    _, text = render(plan()["actions"][0], hh, NEEDS["H02"], "Hall", "Zone", asg, "Team B", settings.messages, 8)
    assert "Team B" in text and "21:20" in text


def test_no_deadline_wording(settings):
    hh = Household("H17", "B01", "Test", 1, "en")
    _, text = render(plan()["actions"][1], hh, NEEDS["H17"], "Hall", "Zone", None, None, settings.messages, 8)
    assert "Wait for further instructions" in text


def test_language_and_fallback(settings):
    zh = Household("H02", "B01", "Test", 2, "zh-Hant")
    lang, text = render(plan()["actions"][0], zh, NEEDS["H02"], "Hall", "Zone", None, None, settings.messages, 8)
    assert lang == "zh-Hant" and "前" in text
    other = Household("H02", "B01", "Test", 2, "fr")
    assert render(plan()["actions"][0], other, NEEDS["H02"], "Hall", "Zone", None, None, settings.messages, 8)[0] == "en"


def test_no_diagnosis_in_messages(settings, ds, db):
    sent = on_plan_approved(plan(), ds, NEEDS, {}, settings.messages, 8, db, T0)
    for m in sent:
        assert "wheelchair" not in m.text.lower()      # mobility details are not broadcast


def test_acknowledge(settings, ds, db):
    on_plan_approved(plan(), ds, NEEDS, {}, settings.messages, 8, db, T0)
    m = acknowledge(db, "H02", T0)
    assert m["status"] == "acknowledged"
    assert db.notifications("H17")[0]["status"] == "sent-simulated"

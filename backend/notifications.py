"""Section 07: resident messages, simulated delivery.

Only an approved plan produces messages. Each message has an idempotency key
(plan + household), so approving the same plan twice cannot message anyone twice.
Delivery is simulated: messages go to a local outbox shown on the dashboard. No message
contains medical details beyond the household's own packing checklist.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from backend.schemas import Notification


class NotApproved(RuntimeError):
    pass


def local_time(dt: Optional[datetime], utc_offset_hours: float) -> str:
    if dt is None:
        return "-"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.astimezone(timezone(timedelta(hours=utc_offset_hours))).strftime("%H:%M")


def render(action: dict, household, needs: Optional[dict], shelter_name: str, zone_name: str,
           assignment: Optional[dict], team_name: Optional[str], messages: dict, utc_offset: float) -> tuple:
    lang = household.language if household.language in messages["templates"] else messages.get("default_language", "en")
    t = messages["templates"][lang]
    if needs and needs.get("needs_team"):
        if assignment and assignment.get("state") in ("accepted", "arrived"):
            help_line = t["help_assigned"].format(team=team_name or assignment["team_id"],
                                                  arrival_by=local_time(assignment.get("arrival_by_utc"), utc_offset))
        else:
            help_line = t["help_requested"]
    else:
        help_line = t["no_help"]
    checklist = ", ".join((needs or {}).get("checklist", [])) or "-"
    fields = dict(issuer=messages.get("issuer", "SlopeCast"), zone=zone_name, destination=shelter_name,
                  checklist=checklist, help_line=help_line,
                  leave_by=local_time(action.get("leave_by_utc"), utc_offset))
    key = "leave_by" if action.get("leave_by_utc") else "no_deadline"
    return lang, t[key].format(**fields).strip()


def on_plan_approved(plan: dict, ds, needs_by_household: dict, assignments: dict, messages: dict,
                     utc_offset: float, db, now: datetime) -> list:
    if plan.get("state") != "approved":
        raise NotApproved(f"plan {plan.get('plan_id')} is {plan.get('state')}, not approved")
    created = []
    for a in plan["actions"]:
        hid = a["household_id"]
        hh = ds.households.get(hid)
        if hh is None:
            continue
        shelter = ds.shelters.get(a.get("destination_id"))
        zone = ds.zones.get(a.get("zone_id"))
        asg = assignments.get(hid)
        team = ds.teams.get(asg["team_id"]) if asg else None
        lang, text = render(a, hh, needs_by_household.get(hid), shelter.name if shelter else "the shelter the committee tells you",
                            zone.name if zone else (a.get("zone_id") or "your area"), asg, team.name if team else None,
                            messages, utc_offset)
        n = Notification(message_id=f"MSG-{plan['plan_id']}-{hid}", idempotency_key=f"{plan['plan_id']}:{hid}",
                         plan_id=plan["plan_id"], plan_version=plan["version"], household_id=hid, language=lang,
                         text=text, status="sent-simulated", created_at_utc=now, simulated=True)
        if db.add_notification(n):
            created.append(n)
    return created


def acknowledge(db, household_id: str, now: datetime) -> Optional[dict]:
    msgs = db.notifications(household_id)
    if not msgs:
        return None
    m = msgs[-1]
    if m["status"] != "acknowledged":
        m["status"] = "acknowledged"
        m["acknowledged_at_utc"] = now.isoformat()
        db.update_notification(m)
    return m


def unacknowledged(db, plan_id: str) -> list:
    return [m for m in db.notifications() if m["plan_id"] == plan_id and m["status"] != "acknowledged"]

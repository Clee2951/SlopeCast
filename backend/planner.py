"""Section 06: evacuation planner. Turns exposure and needs into deadlines and rescue jobs.

Planning horizon (when the slope is treated as unsafe), first match wins:
  hazard-timing   earliest edge of a validated experimental timing window
  operator        a time an operator entered on the dashboard
  policy-default  minutes for the current risk level in evacuation-policy.json (demo setting)
  none            no deadline: households are ranked by urgency, no times are invented

With a horizon:
  leave_by   = horizon - safety buffer - travel time
  prepare_by = leave_by - preparation time
  arrival_by (team) = leave_by - team service time
Teams are assigned earliest-deadline-first within seat and wheelchair limits. Jobs a team
has already accepted are kept. A job no team can reach in time is marked infeasible.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from backend.households import Dataset, distance_km
from backend.schemas import Assignment, EvacuationPlan, HouseholdAction

ORDER = {"low": 0, "medium": 1, "high": 2}


def choose_horizon(assessment, operator_horizon: Optional[datetime], policy: dict, now: datetime,
                   anchored_default: Optional[datetime] = None) -> tuple:
    if assessment.timing_status == "experimental" and assessment.timing_window:
        t = datetime.fromisoformat(assessment.timing_window["earliest_utc"])
        if t > now:
            return t, "hazard-timing"
    if operator_horizon is not None and operator_horizon > now:
        return operator_horizon, "operator"
    minutes = (policy.get("default_horizon_minutes") or {}).get(assessment.risk_level)
    if minutes:
        # the pipeline anchors this time when the level is first reached, so it doesn't slide on every replan
        return (anchored_default or now + timedelta(minutes=minutes)), "policy-default"
    return None, "none"


@dataclass
class _TeamState:
    team: object
    free_at: datetime
    lat: float
    lon: float


def _team_minutes(policy: dict, km: float, on_foot: bool) -> float:
    s = policy["speeds_km_per_h"]
    return km * policy.get("road_factor", 1.4) / (s["walk"] if on_foot else s["drive"]) * 60


def _can_serve(team, need) -> bool:
    if need.wheelchair_spaces and team.wheelchair_spaces < need.wheelchair_spaces:
        return False
    if team.seats == 0:                       # helpers on foot: only for walking assistance
        return need.wheelchair_spaces == 0
    return True


def build_plan(assessment, needs: list, ds: Dataset, commitments: list, policy: dict, now: datetime,
               version: int, operator_horizon: Optional[datetime] = None, simulated: bool = False,
               anchored_default: Optional[datetime] = None) -> EvacuationPlan:
    horizon, basis = choose_horizon(assessment, operator_horizon, policy, now, anchored_default)
    buffer = timedelta(minutes=policy.get("safety_buffer_minutes", 15))
    service = policy.get("team_service_minutes", 5)
    exposed = [n for n in needs if n.exposed]

    # teams start where they are; accepted jobs keep the team busy
    teams = {t.team_id: _TeamState(t, now, t.lat, t.lon) for t in ds.teams.values()}
    accepted = {c["household_id"]: c for c in commitments if c.get("state") in ("accepted", "arrived")}
    for c in accepted.values():
        ts = teams.get(c["team_id"])
        if ts:
            b = ds.building_of(c["household_id"])
            arrive = max(ts.free_at, now) + timedelta(minutes=_team_minutes(policy, distance_km(ts.lat, ts.lon, b.lat, b.lon), ts.team.seats == 0))
            ts.free_at, ts.lat, ts.lon = arrive + timedelta(minutes=service), b.lat, b.lon

    actions, assignments, unresolved = [], [], []
    rows = []
    for n in exposed:
        leave_by = prepare_by = arrival_by = None
        if horizon is not None:
            leave_by = horizon - buffer - timedelta(minutes=n.travel_min or 0)
            prepare_by = leave_by - timedelta(minutes=n.prep_min)
            arrival_by = leave_by - timedelta(minutes=service) if n.needs_team else None
        rows.append((n, leave_by, prepare_by, arrival_by))
    # earliest deadline first; without deadlines: likelihood, then need for help
    rows.sort(key=lambda r: (r[3] or r[1] or datetime.max.replace(tzinfo=now.tzinfo),
                             -ORDER.get(r[0].likelihood or "low", 0), not r[0].needs_team))

    for n, leave_by, prepare_by, arrival_by in rows:
        reasons = list(n.unresolved)
        feasible = True
        team_id = None
        if n.destination_id is None:
            feasible = False
        if prepare_by is not None and prepare_by < now:
            reasons.append("should already be preparing: leave as soon as possible")
            if leave_by < now:
                feasible = False
                reasons.append("deadline already passed with normal preparation time")
        if horizon is None:
            reasons.append("no deadline set: follow the committee's instructions")
        if n.needs_team:
            b = ds.building_of(n.household_id)
            if n.household_id in accepted:
                c = accepted[n.household_id]
                team_id = c["team_id"]
                reasons.append(f"{team_id} has accepted this job")
            else:
                best = None
                for ts in teams.values():
                    if not _can_serve(ts.team, n):
                        continue
                    km = distance_km(ts.lat, ts.lon, b.lat, b.lon)
                    arrive = max(ts.free_at, now) + timedelta(minutes=_team_minutes(policy, km, ts.team.seats == 0))
                    if best is None or arrive < best[0]:
                        best = (arrive, ts)
                if best is None:
                    feasible = False
                    reasons.append("no team has the vehicle this household needs")
                else:
                    arrive, ts = best
                    team_id = ts.team.team_id
                    if arrival_by is not None and arrive > arrival_by:
                        feasible = False
                        reasons.append(f"earliest team arrival {arrive.strftime('%H:%M')} UTC is after the deadline")
                    back = _team_minutes(policy, distance_km(b.lat, b.lon, *_dest(ds, n.destination_id, b)), ts.team.seats == 0)
                    ts.free_at = arrive + timedelta(minutes=service + (n.travel_min or 0) + back)
                    ts.lat, ts.lon = _dest(ds, n.destination_id, b)
            if team_id:
                assignments.append(Assignment(f"A-{n.household_id}-{team_id}", team_id, n.household_id, arrival_by,
                                              accepted[n.household_id]["state"] if n.household_id in accepted else "proposed",
                                              accepted.get(n.household_id, {}).get("accepted_by"),
                                              None))
        if not feasible:
            unresolved.append(f"{n.household_id}: " + (reasons[-1] if reasons else "infeasible"))
        actions.append(HouseholdAction(n.household_id, n.zone_id, n.likelihood, prepare_by, leave_by, team_id,
                                       arrival_by, n.destination_id, feasible, reasons))

    return EvacuationPlan(
        plan_id=f"PLAN-{version:05d}", version=version, site_id=assessment.site_id, created_at_utc=now,
        assessment_id=assessment.assessment_id, policy_version=policy.get("version", ""),
        horizon_utc=horizon, horizon_basis=basis, actions=actions, assignments=assignments,
        unresolved=unresolved, state="draft", simulated=simulated or assessment.simulated,
    )


def _dest(ds: Dataset, shelter_id: Optional[str], fallback_building) -> tuple:
    s = ds.shelters.get(shelter_id) if shelter_id else None
    return (s.lat, s.lon) if s else (fallback_building.lat, fallback_building.lon)

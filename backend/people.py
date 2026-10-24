"""Section 05: people engine. Who is exposed, and what each household needs to get out.

For every household: which assessed zones contain its building (exposure), what help
its members have said they need, how long preparing and travelling will take, and which
shelter suits them. Unknowns are reported in `unresolved`, never guessed. Medical needs
are never inferred from age.
"""
from __future__ import annotations

from typing import Optional

from backend.households import Dataset, distance_km, point_in_ring
from backend.schemas import HouseholdNeeds

ORDER = {"low": 0, "medium": 1, "high": 2}
DEFAULT_CHECKLIST = ["ID documents", "phone and charger", "water"]


def _choose_shelter(ds: Dataset, lat: float, lon: float, need_access: bool, need_power: bool,
                    unsafe_zone_rings: list) -> tuple:
    """Nearest suitable shelter. Returns (shelter or None, problems list)."""
    problems = []
    cands = []
    for s in ds.shelters.values():
        if s.route_status == "blocked":
            continue
        if need_access and not s.wheelchair_accessible:
            continue
        if need_power and not s.power_for_medical_devices:
            continue
        if any(point_in_ring(s.lat, s.lon, ring) for ring in unsafe_zone_rings):
            continue                                  # a shelter inside an at-risk zone is not a destination
        cands.append((s.route_status != "open", distance_km(lat, lon, s.lat, s.lon), s))
    if not cands:
        what = ", ".join(w for w, on in (("wheelchair access", need_access), ("power for medical devices", need_power)) if on)
        problems.append("no safe shelter available" + (f" with {what}" if what else ""))
        return None, problems
    cands.sort(key=lambda c: (c[0], c[1]))
    best = cands[0][2]
    if best.route_status != "open":
        problems.append(f"route to {best.name} not checked")
    return best, problems


def evaluate(assessment, ds: Dataset, policy: dict) -> list:
    """Returns one HouseholdNeeds per household (exposed or not)."""
    likelihood = {z.zone_id: z.likelihood for z in assessment.zone_impacts}
    exposed_levels = set(policy.get("exposed_likelihoods", ["medium", "high"]))
    unsafe_rings = [ds.zones[z].ring for z, lk in likelihood.items() if lk in exposed_levels and z in ds.zones]
    prep = policy["prep_minutes"]
    speed = policy["speeds_km_per_h"]
    road = policy.get("road_factor", 1.4)
    out = []
    for h in ds.households.values():
        b = ds.buildings[h.building_id]
        zones_here = [z for z in ds.zones.values() if point_in_ring(b.lat, b.lon, z.ring)]
        worst: Optional[str] = None
        worst_lk: Optional[str] = None
        for z in zones_here:
            lk = likelihood.get(z.zone_id, "low")
            if worst_lk is None or ORDER[lk] > ORDER[worst_lk]:
                worst, worst_lk = z.zone_id, lk
        exposed = worst_lk in exposed_levels
        members = ds.people_in(h.household_id)
        unresolved = []
        assistance = []
        wheelchairs = sum(1 for p in members if p.mobility == "wheelchair")
        helpers = sum(1 for p in members if p.mobility == "needs_helper")
        unknown = [p.person_id for p in members if p.mobility is None]
        if wheelchairs:
            assistance.append(f"wheelchair-accessible vehicle ({wheelchairs})")
        if helpers:
            assistance.append(f"helper to walk ({helpers})")
        for p in members:
            for n in p.stated_needs:
                if n not in assistance and "wheelchair" not in n and "helper" not in n:
                    assistance.append(n)
        if unknown:
            unresolved.append(f"mobility unknown for {', '.join(unknown)}")
        if not members:
            unresolved.append("no people recorded for this household")
        if b.access_notes:
            unresolved.append(f"access: {b.access_notes}")
        items = sorted({i for p in members for i in p.essentials})
        need_power = any("oxygen" in i.lower() for i in items)
        needs_team = bool(wheelchairs or helpers)
        checklist = items + [c for c in DEFAULT_CHECKLIST if c not in items]
        prep_min = (prep["base"] + prep["per_person"] * max(len(members), h.size)
                    + (prep["wheelchair"] if wheelchairs else 0) + (prep["needs_helper"] if helpers else 0)
                    + prep["per_essential_item"] * len(items))
        shelter, problems = _choose_shelter(ds, b.lat, b.lon, wheelchairs > 0, need_power, unsafe_rings)
        unresolved += problems
        travel = None
        if shelter is not None:
            km = distance_km(b.lat, b.lon, shelter.lat, shelter.lon) * road
            kmh = speed["drive"] if needs_team else speed["walk"]
            travel = round(km / kmh * 60, 1)
        out.append(HouseholdNeeds(
            household_id=h.household_id, assessment_id=assessment.assessment_id,
            zone_id=worst, likelihood=worst_lk, exposed=exposed, people_count=max(len(members), h.size),
            assistance=assistance, needs_team=needs_team, wheelchair_spaces=wheelchairs,
            checklist=checklist, prep_min=float(prep_min), travel_min=travel,
            destination_id=shelter.shelter_id if shelter else None,
            route_status=shelter.route_status if shelter else "none",
            unresolved=unresolved,
        ))
    return out

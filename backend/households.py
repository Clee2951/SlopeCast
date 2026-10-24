"""Section 03: households, buildings, people, shelters, hazard zones and rescue teams.

Loads the demo files, validates them, stores them in the database and answers location
questions. Unknown values stay None; nothing is filled in. Also holds the two geometry
helpers the people engine uses (point-in-polygon and distance).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from backend.schemas import Building, HazardZone, Household, Person, Shelter, Team, to_dict


class ValidationError(ValueError):
    def __init__(self, problems: list):
        super().__init__("; ".join(problems[:10]) + (f" (+{len(problems) - 10} more)" if len(problems) > 10 else ""))
        self.problems = problems


@dataclass
class Dataset:
    buildings: dict = field(default_factory=dict)    # id -> Building
    households: dict = field(default_factory=dict)   # id -> Household
    people: dict = field(default_factory=dict)       # id -> Person
    shelters: dict = field(default_factory=dict)
    zones: dict = field(default_factory=dict)
    teams: dict = field(default_factory=dict)

    def people_in(self, household_id: str) -> list:
        return [p for p in self.people.values() if p.household_id == household_id]

    def building_of(self, household_id: str) -> Building:
        return self.buildings[self.households[household_id].building_id]


# ---------------------------------------------------------------- geometry
def point_in_ring(lat: float, lon: float, ring: list) -> bool:
    """Ray casting. ring is [[lon, lat], ...]. Points exactly on an edge count as inside."""
    inside = False
    n = len(ring)
    for i in range(n - 1 if ring[0] == ring[-1] else n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        # on-edge check (boundary rule: on the line = inside, the cautious choice)
        cross = (x2 - x1) * (lat - y1) - (y2 - y1) * (lon - x1)
        if abs(cross) < 1e-12 and min(x1, x2) - 1e-12 <= lon <= max(x1, x2) + 1e-12 \
                and min(y1, y2) - 1e-12 <= lat <= max(y1, y2) + 1e-12:
            return True
        if (y1 > lat) != (y2 > lat):
            x_at = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < x_at:
                inside = not inside
    return inside


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def ring_centroid(ring: list) -> tuple:
    pts = ring[:-1] if ring[0] == ring[-1] else ring
    return (sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts))


# ---------------------------------------------------------------- loading and validation
def _need(d: dict, key: str, where: str, problems: list):
    if key not in d or d[key] in (None, ""):
        problems.append(f"{where}: missing {key}")
        return None
    return d[key]


def load_files(paths: dict, site_center: Optional[list] = None, max_km: float = 10.0) -> Dataset:
    """Read and validate the demo files named in config/site.json. Raises ValidationError."""
    problems: list = []
    ds = Dataset()
    hh = json.loads(Path(paths["households"]).read_text())

    for b in hh.get("buildings", []):
        bid = _need(b, "building_id", "building", problems)
        if bid is None:
            continue
        if bid in ds.buildings:
            problems.append(f"duplicate building_id {bid}")
        try:
            lat, lon = float(b["lat"]), float(b["lon"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"building {bid}: bad coordinates")
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            problems.append(f"building {bid}: coordinates out of range")
        if site_center and distance_km(lat, lon, *site_center) > max_km:
            problems.append(f"building {bid}: more than {max_km} km from the site")
        for fact in b.get("history", []):
            if not fact.get("source"):
                problems.append(f"building {bid}: history fact without a source")
        ds.buildings[bid] = Building(bid, lat, lon, b.get("construction"), b.get("built_year"),
                                     b.get("history", []), b.get("access_notes"))

    for h in hh.get("households", []):
        hid = _need(h, "household_id", "household", problems)
        bid = _need(h, "building_id", f"household {hid}", problems)
        if hid is None or bid is None:
            continue
        if hid in ds.households:
            problems.append(f"duplicate household_id {hid}")
        if bid not in ds.buildings:
            problems.append(f"household {hid}: unknown building {bid}")
        ds.households[hid] = Household(hid, bid, h.get("name", hid), int(h.get("size") or 0),
                                       h.get("language", "en"), h.get("contact", {}))

    for p in hh.get("people", []):
        pid = _need(p, "person_id", "person", problems)
        hid = _need(p, "household_id", f"person {pid}", problems)
        if pid is None or hid is None:
            continue
        if pid in ds.people:
            problems.append(f"duplicate person_id {pid}")
        if hid not in ds.households:
            problems.append(f"person {pid}: unknown household {hid}")
        mob = p.get("mobility")
        if mob not in (None, "walks", "needs_helper", "wheelchair"):
            problems.append(f"person {pid}: unknown mobility '{mob}'")
        ds.people[pid] = Person(pid, hid, p.get("age_band"), mob, p.get("stated_needs", []), p.get("essentials", []))

    for h in ds.households.values():
        n = len(ds.people_in(h.household_id))
        if h.size and n and n != h.size:
            problems.append(f"household {h.household_id}: size {h.size} but {n} people listed")

    for s in json.loads(Path(paths["shelters"]).read_text()).get("shelters", []):
        sid = _need(s, "shelter_id", "shelter", problems)
        if sid is None:
            continue
        ds.shelters[sid] = Shelter(sid, s.get("name", sid), float(s["lat"]), float(s["lon"]), s.get("capacity"),
                                   s.get("wheelchair_accessible"), s.get("power_for_medical_devices"),
                                   s.get("route_status", "unknown"), s.get("route_notes", ""))

    gj = json.loads(Path(paths["zones"]).read_text())
    for f in gj.get("features", []):
        pr = f.get("properties", {})
        zid = _need(pr, "zone_id", "zone", problems)
        if zid is None:
            continue
        if zid in ds.zones:
            problems.append(f"duplicate zone_id {zid}")
        geom = f.get("geometry") or {}
        if geom.get("type") != "Polygon" or not geom.get("coordinates"):
            problems.append(f"zone {zid}: geometry must be a Polygon")
            continue
        ring = geom["coordinates"][0]
        if len(ring) < 4:
            problems.append(f"zone {zid}: polygon needs at least 3 corners")
        ds.zones[zid] = HazardZone(zid, pr.get("name", zid), pr.get("source_nodes", []), pr.get("severity_if_hit", ""),
                                   pr.get("origin", "unknown origin"), pr.get("version", ""), ring)

    for t in json.loads(Path(paths["resources"]).read_text()).get("teams", []):
        tid = _need(t, "team_id", "team", problems)
        if tid is None:
            continue
        ds.teams[tid] = Team(tid, t.get("name", tid), t.get("vehicle", "unknown"), int(t.get("seats", 0)),
                             int(t.get("wheelchair_spaces", 0)), float(t["lat"]), float(t["lon"]))

    if problems:
        raise ValidationError(problems)
    return ds


# ---------------------------------------------------------------- database
KINDS = {"buildings": "building", "households": "household", "people": "person",
         "shelters": "shelter", "zones": "zone", "teams": "team"}
CLASSES = {"building": Building, "household": Household, "person": Person,
           "shelter": Shelter, "zone": HazardZone, "team": Team}


def import_into_db(db, ds: Dataset) -> dict:
    """Upsert every record by its ID. Re-running never duplicates; runtime progress is untouched."""
    counts = {}
    with db.transaction():
        for attr, kind in KINDS.items():
            items = getattr(ds, attr)
            for rec_id, rec in items.items():
                db.put(kind, rec_id, to_dict(rec), created_at="")
            counts[attr] = len(items)
    return counts


def load_from_db(db) -> Dataset:
    ds = Dataset()
    for attr, kind in KINDS.items():
        cls = CLASSES[kind]
        id_field = {"building": "building_id", "household": "household_id", "person": "person_id",
                    "shelter": "shelter_id", "zone": "zone_id", "team": "team_id"}[kind]
        setattr(ds, attr, {d[id_field]: cls(**d) for d in db.all(kind)})
    return ds


# ---------------------------------------------------------------- queries
def zones_containing(ds: Dataset, building: Building) -> list:
    return [z for z in ds.zones.values() if point_in_ring(building.lat, building.lon, z.ring)]


def households_in_zone(ds: Dataset, zone_id: str) -> list:
    z = ds.zones[zone_id]
    return [h for h in ds.households.values()
            if point_in_ring(ds.buildings[h.building_id].lat, ds.buildings[h.building_id].lon, z.ring)]


def accessible_shelters(ds: Dataset) -> list:
    return [s for s in ds.shelters.values() if s.wheelchair_accessible]

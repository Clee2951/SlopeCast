"""Shared record contracts for SlopeCast.

Every module exchanges these types instead of loose dicts. This file imports nothing
from the rest of the backend, so anything may import it.

Sections: 01 sensors, 02 weather, 03 site and people, 04 hazard, 05 people engine,
06 planner, 07/08 feedback and messages. Conventions for all of them:

- Times are timezone-aware UTC datetimes. Local time is for display only.
- A missing value is None plus a reason, never 0.
- Every derived record says what it was derived from and how trustworthy it is.
- Simulated or replayed records carry simulated=True so they never mix with live data.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Literal, Optional

Quality = Literal["ok", "degraded", "unavailable"]


def to_dict(record) -> dict:
    """JSON-friendly dict of any dataclass record (datetimes as ISO 8601)."""
    def conv(v):
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, (list, tuple)):
            return [conv(x) for x in v]
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        return v
    return {k: conv(v) for k, v in asdict(record).items()}


# ---------------------------------------------------------------- 01 field hardware
@dataclass(frozen=True)
class SensorReading:
    """One accepted `reading` line from a slope node, with gateway timestamps.

    Raw values are kept exactly as reported; nothing here is calibrated.
    """
    node_id: str
    site_id: Optional[str]            # from config/site.json, never guessed from the data
    session_id: str                   # "<node>:<boot>"; a new boot ID means the Uno restarted
    seq: int                          # unique within session
    uptime_ms: int
    sample_time_utc: datetime         # gateway estimate: session anchor + uptime
    receipt_time_utc: datetime        # when the laptop received the line
    moisture_raw: Optional[int]       # ADC counts 0-1023, None if the reading is unusable
    moisture_min: Optional[int]
    moisture_max: Optional[int]
    accel_g: Optional[tuple[float, float, float]]   # mean x, y, z in g; None if unavailable
    accel_units: str
    accel_model: str
    accel_samples: int
    firmware_version: str
    protocol_version: int
    health_flags: tuple[str, ...] = ()
    simulated: bool = False


@dataclass(frozen=True)
class SensorFeatures:
    """Calibrated, windowed view of one node's recent readings, for the hazard engine."""
    node_id: str
    site_id: Optional[str]
    computed_at_utc: datetime
    window_start_utc: Optional[datetime]
    window_end_utc: Optional[datetime]
    reading_count: int
    session_ids: tuple[str, ...]
    first_seq: Optional[int]
    last_seq: Optional[int]
    calibration_version: Optional[str]
    latest_reading_age_s: Optional[float]

    moisture_raw: Optional[float]               # latest usable raw value
    wetness_index: Optional[float]              # 0 = calibrated dry, 1 = calibrated wet; not % water
    wetness_trend_per_h: Optional[float]        # change in wetness index per hour over the window

    tilt_deg: Optional[float]                   # angle between gravity now and the installation baseline
    tilt_downslope_deg: Optional[float]         # signed: + means leaning downslope
    tilt_trend_deg_per_h: Optional[float]       # slope of tilt_deg over the window

    quality: Quality = "unavailable"
    quality_reasons: tuple[str, ...] = ()
    simulated: bool = False


# ---------------------------------------------------------------- site and people (03)
@dataclass
class NodeConfig:
    node_id: str
    lat: float
    lon: float
    sector: str
    kind: str = "live"                 # live | simulated-if-absent
    serial_port: str = "auto"


@dataclass
class Building:
    building_id: str
    lat: float
    lon: float
    construction: Optional[str] = None
    built_year: Optional[int] = None
    history: list = field(default_factory=list)       # each fact carries its own source
    access_notes: Optional[str] = None


@dataclass
class Household:
    household_id: str
    building_id: str
    name: str
    size: int
    language: str = "en"
    contact: dict = field(default_factory=dict)


@dataclass
class Person:
    person_id: str
    household_id: str
    age_band: Optional[str]
    mobility: Optional[str]            # walks | needs_helper | wheelchair | None (unknown)
    stated_needs: list = field(default_factory=list)
    essentials: list = field(default_factory=list)


@dataclass
class Shelter:
    shelter_id: str
    name: str
    lat: float
    lon: float
    capacity: Optional[int]
    wheelchair_accessible: Optional[bool]
    power_for_medical_devices: Optional[bool]
    route_status: str = "unknown"      # open | blocked | unknown
    route_notes: str = ""


@dataclass
class HazardZone:
    zone_id: str
    name: str
    source_nodes: list
    severity_if_hit: str
    origin: str
    version: str
    ring: list                         # [[lon, lat], ...] closed polygon ring


@dataclass
class Team:
    team_id: str
    name: str
    vehicle: str
    seats: int
    wheelchair_spaces: int
    lat: float
    lon: float


# ---------------------------------------------------------------- 02 weather
@dataclass
class WeatherSnapshot:
    snapshot_id: str
    site_id: str
    fetched_at_utc: datetime
    source: str                        # e.g. "apify:owner~actor run=abc", "open-meteo-direct", "offline-file"
    hours: list                        # [{"time_utc": iso, "mm": float|None, "kind": "observed"|"forecast"}]
    rain_1h_mm: Optional[float]
    rain_6h_mm: Optional[float]
    rain_24h_mm: Optional[float]
    rain_72h_mm: Optional[float]
    forecast_24h_mm: Optional[float]
    peak_mm_per_h: Optional[float]
    threshold_crossing_utc: Optional[datetime]
    threshold_name: str
    stale: bool = False
    age_s: float = 0.0
    error: Optional[str] = None
    simulated: bool = False


# ---------------------------------------------------------------- 04 hazard
@dataclass
class ZoneImpact:
    zone_id: str
    likelihood: str                    # low | medium | high
    evidence_nodes: list
    reasons: list


@dataclass
class HazardAssessment:
    assessment_id: str
    site_id: str
    created_at_utc: datetime
    engine: str                        # "claude:<model>" | "rules-fallback" | "rules-only"
    input_refs: dict
    risk_level: str                    # monitoring | elevated | urgent_review
    zone_impacts: list                 # list[ZoneImpact]
    coverage_gaps: list
    timing_status: str                 # unavailable | experimental
    timing_window: Optional[dict]      # {"earliest_utc", "latest_utc", "basis"}
    rain_threshold_crossing_utc: Optional[datetime]
    summary: str
    quality: str
    notes: list                        # validation notes (what code changed in Claude's answer, fallback reason)
    policy_version: str
    prompt_version: Optional[str]
    simulated: bool = False


# ---------------------------------------------------------------- 05 people
@dataclass
class HouseholdNeeds:
    household_id: str
    assessment_id: str
    zone_id: Optional[str]
    likelihood: Optional[str]
    exposed: bool
    people_count: int
    assistance: list                   # e.g. ["wheelchair vehicle", "helper"]
    needs_team: bool
    wheelchair_spaces: int
    checklist: list
    prep_min: float
    travel_min: Optional[float]
    destination_id: Optional[str]
    route_status: str
    unresolved: list


# ---------------------------------------------------------------- 06 planner
@dataclass
class HouseholdAction:
    household_id: str
    zone_id: Optional[str]
    likelihood: Optional[str]
    prepare_by_utc: Optional[datetime]       # start packing by (leave_by - preparation)
    leave_by_utc: Optional[datetime]         # depart by (horizon - safety buffer - travel)
    team_id: Optional[str]
    arrival_by_utc: Optional[datetime]
    destination_id: Optional[str]
    feasible: bool
    reasons: list


@dataclass
class Assignment:
    assignment_id: str
    team_id: str
    household_id: str
    arrival_by_utc: Optional[datetime]
    state: str = "proposed"            # proposed | accepted | arrived | done
    accepted_by: Optional[str] = None
    accepted_at_utc: Optional[datetime] = None


@dataclass
class EvacuationPlan:
    plan_id: str
    version: int
    site_id: str
    created_at_utc: datetime
    assessment_id: str
    policy_version: str
    horizon_utc: Optional[datetime]
    horizon_basis: str                 # hazard-timing | operator | policy-default | none
    actions: list                      # list[HouseholdAction]
    assignments: list                  # list[Assignment]
    unresolved: list
    state: str = "draft"               # draft | approved | superseded
    approved_by: Optional[str] = None
    approved_at_utc: Optional[datetime] = None
    simulated: bool = False


# ---------------------------------------------------------------- 07/08 feedback and messages
@dataclass
class ProgressEvent:
    event_id: str
    plan_id: Optional[str]
    household_id: Optional[str]
    team_id: Optional[str]
    kind: str                          # acknowledged | team_arrived | evacuated
    actor: str
    time_utc: datetime
    note: str = ""


@dataclass
class Notification:
    message_id: str
    idempotency_key: str
    plan_id: str
    plan_version: int
    household_id: str
    language: str
    text: str
    status: str                        # queued | sent-simulated | acknowledged
    created_at_utc: datetime
    acknowledged_at_utc: Optional[datetime] = None
    simulated: bool = True

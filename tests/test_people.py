"""Section 05 tests: exposure by polygon, assistance from stated needs only, durations,
shelter choice (accessible, not inside an at-risk zone) and unknowns left unresolved."""
from backend.households import Dataset, point_in_ring
from backend.people import evaluate
from backend.schemas import Building, HazardAssessment, HazardZone, Household, Person, Shelter, ZoneImpact
from tests.conftest import T0

SQ = [[120.0, 23.0], [120.01, 23.0], [120.01, 23.01], [120.0, 23.01], [120.0, 23.0]]   # [lon, lat]


def assessment(zones):
    return HazardAssessment("HA-test", "s", T0, "rules-only", {}, "elevated",
                            [ZoneImpact(z, lk, [], []) for z, lk in zones.items()], [], "unavailable", None,
                            None, "", "ok", [], "v", None)


def small_ds(mobility="walks", shelters=None, b_lat=23.005, b_lon=120.005, essentials=()):
    ds = Dataset()
    ds.zones["Z"] = HazardZone("Z", "test zone", ["N01"], "", "test", "1", SQ)
    ds.buildings["B1"] = Building("B1", b_lat, b_lon)
    ds.households["H1"] = Household("H1", "B1", "Test", 1)
    ds.people["P1"] = Person("P1", "H1", "80+", mobility, [], list(essentials))
    for s in shelters or [Shelter("S1", "Hall", 23.0, 120.03, 50, True, True, "open")]:
        ds.shelters[s.shelter_id] = s
    return ds


def test_inside_zone_is_exposed(settings):
    n = evaluate(assessment({"Z": "high"}), small_ds(), settings.evacuation)[0]
    assert n.exposed and n.zone_id == "Z" and n.likelihood == "high"


def test_outside_zone_is_not_exposed(settings):
    n = evaluate(assessment({"Z": "high"}), small_ds(b_lat=23.02), settings.evacuation)[0]
    assert not n.exposed and n.zone_id is None


def test_low_likelihood_is_not_exposed(settings):
    n = evaluate(assessment({"Z": "low"}), small_ds(), settings.evacuation)[0]
    assert not n.exposed


def test_wheelchair_needs_team_and_accessible_shelter(settings):
    shelters = [Shelter("S1", "Near, stairs", 23.0, 120.012, 50, False, True, "open"),
                Shelter("S2", "Far, accessible", 23.0, 120.05, 50, True, True, "open")]
    n = evaluate(assessment({"Z": "high"}), small_ds("wheelchair", shelters), settings.evacuation)[0]
    assert n.needs_team and n.wheelchair_spaces == 1
    assert n.destination_id == "S2"
    assert any("wheelchair" in a for a in n.assistance)


def test_walking_household_gets_no_team(settings):
    n = evaluate(assessment({"Z": "high"}), small_ds("walks"), settings.evacuation)[0]
    assert not n.needs_team and n.assistance == []


def test_age_alone_does_not_create_needs(settings):
    n = evaluate(assessment({"Z": "high"}), small_ds("walks"), settings.evacuation)[0]
    assert n.assistance == []                          # person is 80+, but stated no needs


def test_unknown_mobility_is_unresolved(settings):
    n = evaluate(assessment({"Z": "high"}), small_ds(None), settings.evacuation)[0]
    assert any("mobility unknown" in u for u in n.unresolved)


def test_no_accessible_shelter_flagged(settings):
    shelters = [Shelter("S1", "Stairs", 23.0, 120.03, 50, False, True, "open")]
    n = evaluate(assessment({"Z": "high"}), small_ds("wheelchair", shelters), settings.evacuation)[0]
    assert n.destination_id is None
    assert any("no safe shelter" in u for u in n.unresolved)


def test_shelter_inside_risk_zone_skipped(settings):
    shelters = [Shelter("S1", "Inside zone", 23.002, 120.002, 50, True, True, "open"),
                Shelter("S2", "Outside", 23.0, 120.05, 50, True, True, "open")]
    assert point_in_ring(23.002, 120.002, SQ)
    n = evaluate(assessment({"Z": "high"}), small_ds("walks", shelters), settings.evacuation)[0]
    assert n.destination_id == "S2"


def test_unknown_route_flagged(settings):
    shelters = [Shelter("S1", "Unchecked", 23.0, 120.03, 50, True, True, "unknown")]
    n = evaluate(assessment({"Z": "high"}), small_ds("walks", shelters), settings.evacuation)[0]
    assert any("not checked" in u for u in n.unresolved)


def test_preparation_minutes_follow_policy(settings):
    pm = settings.evacuation["prep_minutes"]
    n = evaluate(assessment({"Z": "high"}), small_ds("wheelchair", essentials=["daily medication"]), settings.evacuation)[0]
    assert n.prep_min == pm["base"] + pm["per_person"] + pm["wheelchair"] + pm["per_essential_item"]
    assert "daily medication" in n.checklist


def test_demo_wheelchair_user_in_z1(settings, ds):
    a = assessment({"Z1": "high"})
    needs = {n.household_id: n for n in evaluate(a, ds, settings.evacuation)}
    assert needs["H02"].exposed and needs["H02"].wheelchair_spaces == 1

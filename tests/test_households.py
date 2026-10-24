"""Section 03 tests: references between records, coordinates, provenance, boundary rule
and safe repeated imports."""
import json
import shutil
from pathlib import Path

import pytest

from backend.households import (ValidationError, households_in_zone, import_into_db, load_files,
                                load_from_db, point_in_ring)


def copy_data(settings, tmp_path):
    paths = {}
    for k, p in settings.data_paths.items():
        dst = tmp_path / Path(p).name
        shutil.copy(p, dst)
        paths[k] = dst
    return paths


def edit(path, fn):
    data = json.loads(Path(path).read_text())
    fn(data)
    Path(path).write_text(json.dumps(data))


def test_demo_data_is_valid(ds):
    assert len(ds.households) >= 15 and len(ds.zones) == 5 and len(ds.teams) == 3
    assert all(ds.households[p.household_id] for p in ds.people.values())


def test_every_zone_has_households(ds):
    for z in ds.zones:
        assert households_in_zone(ds, z), f"zone {z} has no households"


def test_duplicate_id_rejected(settings, tmp_path):
    paths = copy_data(settings, tmp_path)
    edit(paths["households"], lambda d: d["households"].append(dict(d["households"][0])))
    with pytest.raises(ValidationError):
        load_files(paths)


def test_unknown_building_rejected(settings, tmp_path):
    paths = copy_data(settings, tmp_path)
    edit(paths["households"], lambda d: d["households"][0].update(building_id="B999"))
    with pytest.raises(ValidationError):
        load_files(paths)


def test_bad_coordinates_rejected(settings, tmp_path):
    paths = copy_data(settings, tmp_path)
    edit(paths["households"], lambda d: d["buildings"][0].update(lat="north"))
    with pytest.raises(ValidationError):
        load_files(paths)


def test_far_away_building_rejected(settings, tmp_path):
    paths = copy_data(settings, tmp_path)
    edit(paths["households"], lambda d: d["buildings"][0].update(lat=25.03, lon=121.56))   # Taipei
    with pytest.raises(ValidationError):
        load_files(paths, settings.site["center"])


def test_history_needs_a_source(settings, tmp_path):
    paths = copy_data(settings, tmp_path)
    edit(paths["households"], lambda d: d["buildings"][0].update(history=[{"fact": "cracked wall"}]))
    with pytest.raises(ValidationError):
        load_files(paths)


def test_unknown_mobility_stays_unknown(settings, tmp_path):
    paths = copy_data(settings, tmp_path)
    edit(paths["households"], lambda d: d["people"][0].update(mobility=None))
    ds = load_files(paths)
    first = json.loads(Path(paths["households"]).read_text())["people"][0]["person_id"]
    assert ds.people[first].mobility is None


def test_point_in_ring_inside_outside_edge():
    sq = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]       # [lon, lat]
    assert point_in_ring(0.5, 0.5, sq)
    assert not point_in_ring(1.5, 0.5, sq)
    assert point_in_ring(0.0, 0.5, sq)                 # on the edge counts as inside (cautious)


def test_import_twice_does_not_duplicate(db, ds):
    import_into_db(db, ds)
    import_into_db(db, ds)
    again = load_from_db(db)
    assert len(again.households) == len(ds.households)
    assert len(again.people) == len(ds.people)

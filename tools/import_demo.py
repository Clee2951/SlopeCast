"""Load the demo households, buildings, people, shelters, zones and teams into the database.

    uv run python tools/import_demo.py            # import or update (safe to repeat)
    uv run python tools/import_demo.py --reset    # also clear readings, plans and progress

Records are upserted by ID, so running it twice never duplicates anything, and plans,
accepted jobs and progress are left alone unless --reset is given.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db import Database  # noqa: E402
from backend.households import ValidationError, import_into_db, load_files  # noqa: E402
from backend.settings import load_settings  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--reset", action="store_true", help="clear runtime data (readings, plans, progress) too")
    args = ap.parse_args(argv)
    s = load_settings()
    try:
        ds = load_files(s.data_paths, s.site["center"])
    except ValidationError as e:
        print("Demo data has problems:")
        for p in e.problems:
            print("  -", p)
        return 1
    db = Database(s.db_path)
    if args.reset:
        db.reset_runtime()
        print("Cleared readings, assessments, plans, assignments, progress and messages.")
    counts = import_into_db(db, ds)
    print(f"Imported into {s.db_path}: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Remove only records explicitly marked as demo data."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from scripts.seed_demo import clear_demo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="Confirm removal")
    args = parser.parse_args()
    if not args.yes:
        print("Refusing to delete demo records without --yes.")
        return 1
    with SessionLocal() as db:
        clear_demo(db)
        db.commit()
    print("Marked demo records removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

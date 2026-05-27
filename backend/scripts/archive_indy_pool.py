# Python Imports
import argparse
import sys
from pathlib import Path


# Ensure project root is importable when running this script directly
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


# Local Imports
from backend.services.race.archive import archive_pool


def parse_args():
    parser = argparse.ArgumentParser(
        description="Archive an Indy pool's final race results."
    )

    parser.add_argument(
        "--pool-id",
        type=int,
        required=True,
        help="The indy_pools.id value to archive.",
    )

    parser.add_argument(
        "--year",
        type=int,
        required=True,
        help="The race year to archive.",
    )

    parser.add_argument(
        "--race-name",
        type=str,
        default="Indianapolis 500",
        help="Race name to store with the archive.",
    )

    parser.add_argument(
        "--notes",
        type=str,
        default=None,
        help="Optional archive notes.",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    result = archive_pool(
        pool_id=args.pool_id,
        year=args.year,
        race_name=args.race_name,
        notes=args.notes,
    )

    if result.get("success"):
        print("Archive completed successfully.")
        print(f"Archive ID: {result.get('archive_id')}")
        print(f"Entries archived: {result.get('entries_archived')}")
        return

    print("Archive failed.")
    print(result.get("message", "Unknown error."))

    archive_id = result.get("archive_id")
    if archive_id is not None:
        print(f"Existing archive ID: {archive_id}")

    sys.exit(1)


if __name__ == "__main__":
    main()

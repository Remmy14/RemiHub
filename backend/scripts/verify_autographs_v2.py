"""Read-only operator audit for the 31-row Autographs V2 baseline.

Use an explicitly supplied QA or production config. This script is never run by
application startup or deployment automation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

import psycopg2

from backend.config import load_config

IMAGE = re.compile(r"/autographs/image/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(?:jpg|png|webp))\Z")


def audit(conn, root: Path, phase: str, expected_sha: str | None = None) -> dict:
    with conn.cursor() as cur:
        cur.execute("""SELECT id,driver_name,image_path,helmet_view,x_percent,y_percent,region,notes,created_at
            FROM public.autograph_entries ORDER BY id""")
        rows = cur.fetchall()
        stable = json.dumps(rows, default=str, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(stable.encode()).hexdigest()
        cur.execute("""SELECT count(DISTINCT lower(regexp_replace(btrim(driver_name), '[[:space:]]+', ' ', 'g')))
            FROM public.autograph_entries""")
        names = cur.fetchone()[0]
        missing = []
        paths = [row[2] for row in rows if row[2]]
        for path in paths:
            match = IMAGE.fullmatch(path)
            if not match or root.is_symlink() or (root / match.group(1)).is_symlink() or not (root / match.group(1)).is_file():
                missing.append(path)
        result = {"phase": phase, "autographs": len(rows), "normalized_names": names,
                  "nonblank_photo_references": len(paths), "missing_or_invalid_photo_references": missing,
                  "legacy_columns_sha256": digest,
                  "media_root_readable": os.access(root, os.R_OK | os.X_OK),
                  "media_root_writable": os.access(root, os.W_OK | os.X_OK)}
        if phase == "after":
            cur.execute("""SELECT count(*) FROM public.autograph_entries a JOIN public.autograph_helmets h
                ON h.id=a.helmet_id WHERE h.year=2026""")
            result["original_helmet_autographs"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM public.autograph_entries WHERE helmet_id IS NULL OR driver_id IS NULL OR date_signed IS NOT NULL")
            result["unassociated_or_fabricated_dates"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM public.autograph_drivers")
            result["drivers"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM public.autograph_helmets WHERE year=2026 AND uses_legacy_images")
            result["legacy_designated_2026_helmets"] = cur.fetchone()[0]
            cur.execute("""SELECT convalidated AND
                pg_get_constraintdef(oid) LIKE '%helmet_id IS NOT NULL%' AND
                pg_get_constraintdef(oid) LIKE '%driver_id IS NOT NULL%'
                FROM pg_constraint WHERE conrelid='public.autograph_entries'::regclass
                AND conname='autograph_entries_associations_required'""")
            constraint = cur.fetchone()
            result["association_constraint_validated"] = constraint is not None and constraint[0] is True
    failures = []
    for key, target in (("autographs", 31), ("normalized_names", 30), ("nonblank_photo_references", 29)):
        if result[key] != target:
            failures.append(f"{key}: expected {target}, found {result[key]}")
    if missing:
        failures.append("one or more photo references are missing or invalid")
    if expected_sha and digest != expected_sha:
        failures.append("legacy column digest changed")
    if phase == "after":
        for key, target in (("original_helmet_autographs", 31), ("unassociated_or_fabricated_dates", 0),
                            ("drivers", 30), ("legacy_designated_2026_helmets", 1)):
            if result[key] != target:
                failures.append(f"{key}: expected {target}, found {result[key]}")
        if not result["association_constraint_validated"]:
            failures.append("autograph association constraint is missing, invalid, or unvalidated")
    result["failures"] = failures
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Operator-supplied database config path")
    parser.add_argument("--media-root", type=Path, default=Path("/opt/remihub/data/autographs"))
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    parser.add_argument("--expected-sha", help="legacy_columns_sha256 from before phase")
    args = parser.parse_args()
    config = load_config(str(args.config))["Database"]
    conn = psycopg2.connect(user=config["user"], password=config["password"], host=config["host"],
                            port=config["port"], database=config["database"])
    try:
        conn.set_session(readonly=True)
        result = audit(conn, args.media_root, args.phase, args.expected_sha)
    finally:
        conn.close()
    print(json.dumps(result, indent=2))
    if result["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

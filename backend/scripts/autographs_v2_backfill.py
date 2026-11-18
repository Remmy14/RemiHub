"""Operator-run, transactionally guarded Autographs V2 legacy backfill.

Run only after migration 0024 using an explicitly selected QA or production
configuration. The agent must never invoke this against an actual database.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import psycopg2

from backend.config import load_config


BACKFILL_SQL = r"""
-- Operator-run only, after 0024 and before V2 application promotion. Run on
-- reviewed QA first. The owner confirmed that all 31 original rows are 2026.
-- One transaction makes a failed check roll back the entire backfill. Reruns
-- preserve already assigned IDs and do not duplicate helmets or drivers.
DO $backfill$
DECLARE
 legacy_helmet uuid;
 total integer;
 normalized_count integer;
BEGIN
 LOCK TABLE public.autograph_entries IN SHARE ROW EXCLUSIVE MODE;
 SELECT count(*) INTO total FROM public.autograph_entries;
 IF total <> 31 THEN RAISE EXCEPTION 'Expected 31 legacy autographs, found %', total; END IF;
 IF EXISTS (SELECT 1 FROM public.autograph_entries WHERE helmet_id IS NOT NULL AND helmet_id <>
   (SELECT id FROM public.autograph_helmets WHERE year=2026)) THEN
   RAISE EXCEPTION 'Existing helmet associations conflict with confirmed 2026 collection';
 END IF;
 IF EXISTS (SELECT 1 FROM public.autograph_entries WHERE driver_name IS NULL OR btrim(driver_name)=''
   OR helmet_view NOT IN ('front','right','back','left') OR x_percent NOT BETWEEN 0 AND 1
   OR y_percent NOT BETWEEN 0 AND 1) THEN
   RAISE EXCEPTION 'Unmappable legacy autograph rows';
 END IF;
 SELECT count(DISTINCT lower(regexp_replace(btrim(driver_name), '[[:space:]]+', ' ', 'g')))
 INTO normalized_count FROM public.autograph_entries;
 IF normalized_count <> 30 THEN RAISE EXCEPTION 'Expected 30 normalized names, found %', normalized_count; END IF;
 INSERT INTO public.autograph_helmets(year,display_name,uses_legacy_images)
 VALUES (2026,'2026 Indianapolis 500 Helmet',true) ON CONFLICT (year) DO NOTHING;
 SELECT id INTO legacy_helmet FROM public.autograph_helmets WHERE year=2026;
 IF NOT (SELECT uses_legacy_images FROM public.autograph_helmets WHERE id=legacy_helmet) THEN
   RAISE EXCEPTION 'Existing 2026 helmet lacks legacy-image designation';
 END IF;
 -- A completed backfill must be a no-op, including after later driver edits.
 -- A partial preexisting association is unexpected because this script commits
 -- atomically; stop rather than overwrite a human correction.
 IF NOT EXISTS (SELECT 1 FROM public.autograph_entries WHERE helmet_id IS DISTINCT FROM legacy_helmet OR driver_id IS NULL) THEN
   RETURN;
 END IF;
 IF EXISTS (SELECT 1 FROM public.autograph_entries WHERE helmet_id IS NOT NULL OR driver_id IS NOT NULL) THEN
   RAISE EXCEPTION 'Partial autograph associations require operator review';
 END IF;
 INSERT INTO public.autograph_drivers(display_name,normalized_name)
 SELECT DISTINCT ON (lower(regexp_replace(btrim(driver_name), '[[:space:]]+', ' ', 'g')))
   regexp_replace(btrim(driver_name), '[[:space:]]+', ' ', 'g'),
   lower(regexp_replace(btrim(driver_name), '[[:space:]]+', ' ', 'g'))
 FROM public.autograph_entries
 ORDER BY lower(regexp_replace(btrim(driver_name), '[[:space:]]+', ' ', 'g')), id
 ON CONFLICT (normalized_name) DO NOTHING;
 UPDATE public.autograph_entries AS a SET helmet_id=legacy_helmet, driver_id=d.id
 FROM public.autograph_drivers AS d
 WHERE d.normalized_name=lower(regexp_replace(btrim(a.driver_name), '[[:space:]]+', ' ', 'g'))
   AND (a.helmet_id IS DISTINCT FROM legacy_helmet OR a.driver_id IS DISTINCT FROM d.id);
 IF (SELECT count(*) FROM public.autograph_entries WHERE helmet_id=legacy_helmet AND driver_id IS NOT NULL) <> 31
    OR (SELECT count(DISTINCT driver_id) FROM public.autograph_entries) <> 30
    OR EXISTS (SELECT 1 FROM public.autograph_entries WHERE date_signed IS NOT NULL) THEN
   RAISE EXCEPTION 'Backfill relationship/date verification failed';
 END IF;
END;
$backfill$;
-- The old backend may accept Android writes between this backfill and V2 code
-- promotion. Link those inserts synchronously; V2 inserts already supply both
-- identifiers and pass through unchanged. This trigger stays compatible with
-- legacy Android creation after promotion as well.
CREATE OR REPLACE FUNCTION public.autograph_link_legacy_insert()
RETURNS trigger LANGUAGE plpgsql AS $legacy$
DECLARE
 legacy_helmet uuid;
 matched_driver uuid;
 canonical text;
 name_key text;
BEGIN
 IF NEW.helmet_id IS NOT NULL AND NEW.driver_id IS NOT NULL THEN RETURN NEW; END IF;
 IF NEW.helmet_id IS NOT NULL OR NEW.driver_id IS NOT NULL THEN
   RAISE EXCEPTION 'Autograph requires both helmet and driver associations';
 END IF;
 canonical := regexp_replace(btrim(NEW.driver_name), '[[:space:]]+', ' ', 'g');
 IF canonical='' THEN RAISE EXCEPTION 'Legacy driver name cannot be blank'; END IF;
 name_key := lower(canonical);
 SELECT id INTO legacy_helmet FROM public.autograph_helmets WHERE year=2026;
 IF legacy_helmet IS NULL THEN RAISE EXCEPTION 'Original 2026 helmet is missing'; END IF;
 INSERT INTO public.autograph_drivers(display_name,normalized_name)
 VALUES (canonical,name_key) ON CONFLICT (normalized_name) DO NOTHING RETURNING id INTO matched_driver;
 IF matched_driver IS NULL THEN
   SELECT id INTO matched_driver FROM public.autograph_drivers WHERE normalized_name=name_key;
 END IF;
 NEW.helmet_id := legacy_helmet;
 NEW.driver_id := matched_driver;
 RETURN NEW;
END;
$legacy$;
DROP TRIGGER IF EXISTS autograph_link_legacy_insert ON public.autograph_entries;
CREATE TRIGGER autograph_link_legacy_insert
BEFORE INSERT ON public.autograph_entries
FOR EACH ROW EXECUTE FUNCTION public.autograph_link_legacy_insert();
-- The trigger preserves old-backend INSERTs. This validated constraint also
-- rejects NULL associations from UPDATEs and every other database writer.
DO $association_constraint$
BEGIN
 IF NOT EXISTS (
   SELECT 1 FROM pg_constraint
   WHERE conrelid='public.autograph_entries'::regclass
     AND conname='autograph_entries_associations_required'
 ) THEN
   ALTER TABLE public.autograph_entries
     ADD CONSTRAINT autograph_entries_associations_required
     CHECK (helmet_id IS NOT NULL AND driver_id IS NOT NULL) NOT VALID;
 END IF;
END;
$association_constraint$;
ALTER TABLE public.autograph_entries
 VALIDATE CONSTRAINT autograph_entries_associations_required;
"""


def run(conn) -> None:
    """Apply the guarded backfill and compatibility trigger in one transaction."""
    try:
        with conn.cursor() as cur:
            cur.execute(BACKFILL_SQL)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True,
                        help="Explicit operator-supplied QA or production database configuration")
    args = parser.parse_args()
    config = load_config(str(args.config))["Database"]
    conn = psycopg2.connect(user=config["user"], password=config["password"],
                            host=config["host"], port=config["port"], database=config["database"])
    try:
        run(conn)
    finally:
        conn.close()
    print("Autographs V2 backfill and legacy insert trigger committed")


if __name__ == "__main__":
    main()

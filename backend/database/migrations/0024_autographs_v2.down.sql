-- Operator recovery only, before V2 writes. Dropping these columns after V2
-- activity would discard relationships and signing dates; retain a backup.
DROP INDEX autograph_entries_driver_idx;
DROP INDEX autograph_entries_helmet_idx;
ALTER TABLE public.autograph_entries DROP COLUMN date_signed, DROP COLUMN driver_id, DROP COLUMN helmet_id;
DROP TABLE public.autograph_helmet_images;
DROP INDEX autograph_drivers_display_idx;
DROP TABLE public.autograph_drivers;
DROP TABLE public.autograph_helmets;

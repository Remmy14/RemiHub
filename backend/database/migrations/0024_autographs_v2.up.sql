-- Additive schema only. Apply the separately reviewed operator backfill before
-- deploying the V2 application; the old application tolerates nullable links.
CREATE TABLE public.autograph_helmets (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 year integer NOT NULL UNIQUE CHECK (year BETWEEN 1911 AND 9999),
 display_name text NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 200),
 description text,
 uses_legacy_images boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE public.autograph_drivers (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 display_name text NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 200),
 normalized_name text NOT NULL UNIQUE CHECK (length(normalized_name) BETWEEN 1 AND 200),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX autograph_drivers_display_idx ON public.autograph_drivers(display_name, id);
CREATE TABLE public.autograph_helmet_images (
 helmet_id uuid NOT NULL REFERENCES public.autograph_helmets(id) ON DELETE RESTRICT,
 helmet_view text NOT NULL CHECK (helmet_view IN ('front','right','back','left')),
 image_path text NOT NULL,
 content_type text NOT NULL CHECK (content_type IN ('image/jpeg','image/png','image/webp')),
 byte_size integer NOT NULL CHECK (byte_size > 0 AND byte_size <= 5242880),
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (helmet_id, helmet_view)
);
ALTER TABLE public.autograph_entries
 ADD COLUMN helmet_id uuid REFERENCES public.autograph_helmets(id) ON DELETE RESTRICT,
 ADD COLUMN driver_id uuid REFERENCES public.autograph_drivers(id) ON DELETE RESTRICT,
 ADD COLUMN date_signed date;
CREATE INDEX autograph_entries_helmet_idx ON public.autograph_entries(helmet_id, created_at DESC, id DESC);
CREATE INDEX autograph_entries_driver_idx ON public.autograph_entries(driver_id, created_at DESC, id DESC);

-- All Adventures records are private to a RemiHub user. Dates are local calendar dates.
CREATE TABLE public.adventures_campgrounds (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES public.remihub_users(id) ON DELETE CASCADE,
 name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200), address text, city text,
 state_province text, postal_code text, country text, website text,
 latitude numeric(9,6) CHECK (latitude BETWEEN -90 AND 90),
 longitude numeric(9,6) CHECK (longitude BETWEEN -180 AND 180), notes text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE (user_id,id)
);
CREATE INDEX adventures_campgrounds_user_name_idx ON public.adventures_campgrounds(user_id,name,id);

CREATE TABLE public.adventures_trips (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES public.remihub_users(id) ON DELETE CASCADE,
 title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200), description text, destination text,
 campground_id uuid, arrival_date date, departure_date date,
 intent text NOT NULL DEFAULT 'Considering' CHECK (intent IN ('Considering','Planned','Booked','Cancelled')),
 notes text,
 campsite_number text, confirmation_number text, reservation_status text,
 check_in_time time, check_out_time time,
 electricity text NOT NULL DEFAULT 'unknown' CHECK (electricity IN ('50A','30A','20A','none','unknown')),
 water_hookup boolean, sewer_hookup boolean, site_length_feet numeric(7,2) CHECK (site_length_feet > 0),
 access_notes text, booking_website text, reservation_notes text,
 cover_photo_id uuid,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE (user_id,id), UNIQUE (id,campground_id),
 FOREIGN KEY (user_id,campground_id) REFERENCES public.adventures_campgrounds(user_id,id) ON DELETE RESTRICT,
 CONSTRAINT adventures_trip_dates CHECK ((arrival_date IS NULL AND departure_date IS NULL) OR
 (arrival_date IS NOT NULL AND departure_date IS NOT NULL AND departure_date >= arrival_date))
);
CREATE INDEX adventures_trips_calendar_idx ON public.adventures_trips(user_id,arrival_date,departure_date) WHERE arrival_date IS NOT NULL;
CREATE INDEX adventures_trips_history_idx ON public.adventures_trips(user_id,departure_date DESC,id) WHERE departure_date IS NOT NULL;
CREATE INDEX adventures_trips_campground_idx ON public.adventures_trips(user_id,campground_id,arrival_date DESC);

CREATE TABLE public.adventures_activities (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), trip_id uuid NOT NULL REFERENCES public.adventures_trips(id) ON DELETE CASCADE,
 title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200), description text,
 activity_date date, activity_time time, location text, notes text,
 display_order integer NOT NULL DEFAULT 0 CHECK (display_order >= 0), completed boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX adventures_activities_order_idx ON public.adventures_activities(trip_id,activity_date,display_order,id);

-- One implicit reusable master checklist per user; categories are text, not an enum.
CREATE TABLE public.adventures_template_items (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES public.remihub_users(id) ON DELETE CASCADE,
 text text NOT NULL CHECK (length(btrim(text)) BETWEEN 1 AND 500), category text NOT NULL CHECK (length(btrim(category)) BETWEEN 1 AND 100),
 display_order integer NOT NULL DEFAULT 0 CHECK (display_order >= 0), notes text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX adventures_template_items_order_idx ON public.adventures_template_items(user_id,category,display_order,id);
CREATE TABLE public.adventures_trip_checklists (
 trip_id uuid PRIMARY KEY REFERENCES public.adventures_trips(id) ON DELETE CASCADE,
 instantiated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE public.adventures_trip_items (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), trip_id uuid NOT NULL REFERENCES public.adventures_trip_checklists(trip_id) ON DELETE CASCADE,
 text text NOT NULL CHECK (length(btrim(text)) BETWEEN 1 AND 500), category text NOT NULL CHECK (length(btrim(category)) BETWEEN 1 AND 100),
 display_order integer NOT NULL DEFAULT 0 CHECK (display_order >= 0), notes text, completed boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX adventures_trip_items_order_idx ON public.adventures_trip_items(trip_id,category,display_order,id);

CREATE TABLE public.adventures_photos (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), trip_id uuid NOT NULL REFERENCES public.adventures_trips(id) ON DELETE CASCADE,
 original_filename text NOT NULL, storage_key uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
 content_type text NOT NULL CHECK (content_type IN ('image/jpeg','image/png','image/webp')),
 byte_size integer NOT NULL CHECK (byte_size > 0), caption text, favorite boolean NOT NULL DEFAULT false,
 display_order integer NOT NULL DEFAULT 0 CHECK (display_order >= 0), uploaded_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE (trip_id,id)
);
CREATE INDEX adventures_photos_order_idx ON public.adventures_photos(trip_id,display_order,uploaded_at,id);
ALTER TABLE public.adventures_trips ADD CONSTRAINT adventures_cover_photo_fkey
 FOREIGN KEY (id,cover_photo_id) REFERENCES public.adventures_photos(trip_id,id) DEFERRABLE INITIALLY IMMEDIATE;
CREATE TABLE public.adventures_photo_cleanup (
 storage_key uuid PRIMARY KEY, queued_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.adventures_journals (
 trip_id uuid PRIMARY KEY REFERENCES public.adventures_trips(id) ON DELETE CASCADE,
 overall_summary text, favorite_part text, family_enjoyed text, went_well text,
 could_improve text, do_differently text, problems text, notes text, would_visit_again boolean,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE public.adventures_memories (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), trip_id uuid NOT NULL REFERENCES public.adventures_trips(id) ON DELETE CASCADE,
 title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200), description text, long_text text,
 memory_date date, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(trip_id,id)
);
CREATE INDEX adventures_memories_trip_idx ON public.adventures_memories(trip_id,memory_date,id);
CREATE TABLE public.adventures_memory_photos (
 trip_id uuid NOT NULL, memory_id uuid NOT NULL, photo_id uuid NOT NULL,
 PRIMARY KEY(memory_id,photo_id),
 FOREIGN KEY (trip_id,memory_id) REFERENCES public.adventures_memories(trip_id,id) ON DELETE CASCADE,
 FOREIGN KEY (trip_id,photo_id) REFERENCES public.adventures_photos(trip_id,id) ON DELETE CASCADE
);
CREATE TABLE public.adventures_ratings (
 trip_id uuid PRIMARY KEY REFERENCES public.adventures_trips(id) ON DELETE CASCADE,
 overall smallint CHECK (overall BETWEEN 1 AND 5), campsite_quality smallint CHECK (campsite_quality BETWEEN 1 AND 5),
 facilities smallint CHECK (facilities BETWEEN 1 AND 5), activities smallint CHECK (activities BETWEEN 1 AND 5),
 family_friendliness smallint CHECK (family_friendliness BETWEEN 1 AND 5),
 would_return text CHECK (would_return IN ('yes','no','undecided')), review text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);

-- Destructive: operator must back up database and private photo storage first.
DROP TABLE public.adventures_ratings;
DROP TABLE public.adventures_memory_photos;
DROP TABLE public.adventures_memories;
DROP TABLE public.adventures_journals;
DROP TABLE public.adventures_photo_cleanup;
ALTER TABLE public.adventures_trips DROP CONSTRAINT adventures_cover_photo_fkey;
DROP TABLE public.adventures_photos;
DROP TABLE public.adventures_trip_items;
DROP TABLE public.adventures_trip_checklists;
DROP TABLE public.adventures_template_items;
DROP TABLE public.adventures_activities;
DROP TABLE public.adventures_trips;
DROP TABLE public.adventures_campgrounds;

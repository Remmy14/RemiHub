-- The UV alert proof of concept was abandoned. Its data is intentionally
-- discarded; the down migration can restore only the empty legacy schema.
DROP TABLE IF EXISTS public.uv_alert_state;
DROP TABLE IF EXISTS public.uv_alert_settings;

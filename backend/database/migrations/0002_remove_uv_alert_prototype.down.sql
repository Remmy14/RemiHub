-- This restores only the legacy table structures. Data removed by the up
-- migration cannot be recovered without restoring a database backup.
CREATE TABLE public.uv_alert_settings (
    id integer DEFAULT 1 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    profile_name text DEFAULT 'default'::text NOT NULL,
    alert_start_hour integer DEFAULT 8 NOT NULL,
    alert_end_hour integer DEFAULT 20 NOT NULL,
    last_alert_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT uv_alert_settings_singleton CHECK (id = 1),
    CONSTRAINT uv_alert_settings_pkey PRIMARY KEY (id)
);

CREATE TABLE public.uv_alert_state (
    id integer DEFAULT 1 NOT NULL,
    suppress_until timestamp with time zone,
    suppress_reason text,
    sunscreen_applied_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT uv_alert_state_singleton CHECK (id = 1),
    CONSTRAINT uv_alert_state_pkey PRIMARY KEY (id)
);

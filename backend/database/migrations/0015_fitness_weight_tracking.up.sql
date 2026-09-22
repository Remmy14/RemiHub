CREATE TABLE public.fitness_weight_measurements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    measurement_date date NOT NULL,
    weight_lb numeric(8, 2) NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_weight_measurements_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_weight_measurements_weight_check
        CHECK (weight_lb > 0 AND weight_lb <= 1000)
);

CREATE UNIQUE INDEX fitness_weight_measurements_user_date_uidx
    ON public.fitness_weight_measurements (user_id, measurement_date);

CREATE INDEX fitness_weight_measurements_user_latest_idx
    ON public.fitness_weight_measurements (user_id, measurement_date DESC, updated_at DESC);

CREATE TABLE public.fitness_weight_reminder_settings (
    user_id uuid PRIMARY KEY,
    enabled boolean NOT NULL DEFAULT true,
    reminder_time time without time zone NOT NULL DEFAULT TIME '09:00',
    timezone text NOT NULL DEFAULT 'America/New_York',
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_weight_reminder_settings_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_weight_reminder_settings_timezone_check
        CHECK (char_length(btrim(timezone)) BETWEEN 1 AND 120)
);

CREATE TABLE public.fitness_weight_reminder_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    reminder_date date NOT NULL,
    notification_id bigint,
    timezone text NOT NULL,
    reminder_time time without time zone NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_weight_reminder_runs_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_weight_reminder_runs_metadata_check
        CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT fitness_weight_reminder_runs_timezone_check
        CHECK (char_length(btrim(timezone)) BETWEEN 1 AND 120)
);

CREATE UNIQUE INDEX fitness_weight_reminder_runs_user_date_uidx
    ON public.fitness_weight_reminder_runs (user_id, reminder_date);

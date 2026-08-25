CREATE TABLE public.fitness_recurring_schedule_series (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    workout_template_id uuid NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    duration_weeks integer,
    weekdays integer[] NOT NULL,
    status text NOT NULL DEFAULT 'ACTIVE',
    idempotency_key text,
    request_fingerprint text,
    stopped_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_recurring_series_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_recurring_series_template_id_fkey
        FOREIGN KEY (workout_template_id)
        REFERENCES public.fitness_workout_templates(id)
        ON DELETE RESTRICT,
    CONSTRAINT fitness_recurring_series_date_check
        CHECK (
            end_date >= start_date
            AND end_date <= start_date + 1819
        ),
    CONSTRAINT fitness_recurring_series_duration_check
        CHECK (duration_weeks IS NULL OR duration_weeks BETWEEN 1 AND 260),
    CONSTRAINT fitness_recurring_series_weekdays_check
        CHECK (
            cardinality(weekdays) BETWEEN 1 AND 7
            AND weekdays <@ ARRAY[1,2,3,4,5,6,7]
        ),
    CONSTRAINT fitness_recurring_series_status_check
        CHECK (status IN ('ACTIVE', 'STOPPED')),
    CONSTRAINT fitness_recurring_series_idempotency_key_check
        CHECK (idempotency_key IS NULL OR char_length(btrim(idempotency_key)) BETWEEN 1 AND 160),
    CONSTRAINT fitness_recurring_series_fingerprint_check
        CHECK (request_fingerprint IS NULL OR char_length(request_fingerprint) BETWEEN 1 AND 4000)
);

CREATE INDEX fitness_recurring_series_user_status_idx
    ON public.fitness_recurring_schedule_series (user_id, status, start_date DESC);

CREATE UNIQUE INDEX fitness_recurring_series_user_idempotency_uidx
    ON public.fitness_recurring_schedule_series (user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

ALTER TABLE public.fitness_scheduled_workouts
    ADD COLUMN recurring_series_id uuid;

ALTER TABLE public.fitness_scheduled_workouts
    ADD CONSTRAINT fitness_scheduled_workouts_recurring_series_id_fkey
    FOREIGN KEY (recurring_series_id)
    REFERENCES public.fitness_recurring_schedule_series(id)
    ON DELETE SET NULL;

CREATE INDEX fitness_scheduled_workouts_series_date_idx
    ON public.fitness_scheduled_workouts (recurring_series_id, scheduled_date)
    WHERE recurring_series_id IS NOT NULL;

ALTER TABLE public.fitness_training_plan_instances
    ADD COLUMN stopped_at timestamp with time zone;

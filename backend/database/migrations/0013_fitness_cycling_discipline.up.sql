ALTER TABLE public.fitness_workout_templates
    DROP CONSTRAINT fitness_workout_templates_type_check;

ALTER TABLE public.fitness_workout_templates
    ADD CONSTRAINT fitness_workout_templates_type_check
        CHECK (workout_type IN ('RUNNING', 'LIFTING', 'CYCLING'));

CREATE TABLE public.fitness_cycling_workout_templates (
    template_id uuid PRIMARY KEY,
    planned_duration_seconds integer NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_cycling_templates_template_id_fkey
        FOREIGN KEY (template_id)
        REFERENCES public.fitness_workout_templates(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_cycling_templates_duration_check
        CHECK (planned_duration_seconds > 0 AND planned_duration_seconds <= 864000)
);

ALTER TABLE public.fitness_scheduled_workouts
    ADD COLUMN planned_duration_seconds integer;

ALTER TABLE public.fitness_scheduled_workouts
    ADD CONSTRAINT fitness_scheduled_workouts_duration_check
        CHECK (
            planned_duration_seconds IS NULL
            OR (planned_duration_seconds > 0 AND planned_duration_seconds <= 864000)
        );

CREATE TABLE public.fitness_cycling_workout_results (
    scheduled_workout_id uuid PRIMARY KEY,
    planned_duration_seconds integer NOT NULL,
    external_provider text,
    external_activity_id text,
    external_activity_uuid text,
    external_activity_name text,
    external_activity_type_key text,
    external_manufacturer text,
    start_time_local text,
    duration_seconds integer NOT NULL,
    moving_duration_seconds integer,
    completed_distance_miles numeric(8, 2) NOT NULL,
    calories numeric(10, 2),
    average_power_watts numeric(8, 2),
    max_power_watts numeric(8, 2),
    normalized_power_watts numeric(8, 2),
    average_cadence_rpm numeric(8, 2),
    max_cadence_rpm numeric(8, 2),
    average_hr numeric(8, 2),
    max_hr numeric(8, 2),
    hr_zone_1_seconds integer,
    hr_zone_2_seconds integer,
    hr_zone_3_seconds integer,
    hr_zone_4_seconds integer,
    hr_zone_5_seconds integer,
    aerobic_training_effect numeric(5, 2),
    anaerobic_training_effect numeric(5, 2),
    training_load numeric(10, 2),
    training_effect_label text,
    resistance_min numeric(8, 2),
    resistance_avg numeric(8, 2),
    resistance_max numeric(8, 2),
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_cycling_results_scheduled_id_fkey
        FOREIGN KEY (scheduled_workout_id)
        REFERENCES public.fitness_scheduled_workouts(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_cycling_results_planned_duration_check
        CHECK (planned_duration_seconds > 0 AND planned_duration_seconds <= 864000),
    CONSTRAINT fitness_cycling_results_external_provider_check
        CHECK (external_provider IS NULL OR external_provider IN ('GARMIN')),
    CONSTRAINT fitness_cycling_results_external_activity_check
        CHECK (
            (external_provider IS NULL AND external_activity_id IS NULL)
            OR (external_provider IS NOT NULL AND external_activity_id IS NOT NULL)
        ),
    CONSTRAINT fitness_cycling_results_duration_check
        CHECK (duration_seconds >= 0),
    CONSTRAINT fitness_cycling_results_moving_duration_check
        CHECK (moving_duration_seconds IS NULL OR moving_duration_seconds >= 0),
    CONSTRAINT fitness_cycling_results_distance_check
        CHECK (completed_distance_miles >= 0),
    CONSTRAINT fitness_cycling_results_power_check
        CHECK (
            (average_power_watts IS NULL OR average_power_watts >= 0)
            AND (max_power_watts IS NULL OR max_power_watts >= 0)
            AND (normalized_power_watts IS NULL OR normalized_power_watts >= 0)
        ),
    CONSTRAINT fitness_cycling_results_cadence_check
        CHECK (
            (average_cadence_rpm IS NULL OR average_cadence_rpm >= 0)
            AND (max_cadence_rpm IS NULL OR max_cadence_rpm >= 0)
        ),
    CONSTRAINT fitness_cycling_results_hr_check
        CHECK (
            (average_hr IS NULL OR average_hr >= 0)
            AND (max_hr IS NULL OR max_hr >= 0)
        ),
    CONSTRAINT fitness_cycling_results_hr_zones_check
        CHECK (
            (hr_zone_1_seconds IS NULL OR hr_zone_1_seconds >= 0)
            AND (hr_zone_2_seconds IS NULL OR hr_zone_2_seconds >= 0)
            AND (hr_zone_3_seconds IS NULL OR hr_zone_3_seconds >= 0)
            AND (hr_zone_4_seconds IS NULL OR hr_zone_4_seconds >= 0)
            AND (hr_zone_5_seconds IS NULL OR hr_zone_5_seconds >= 0)
        ),
    CONSTRAINT fitness_cycling_results_training_metrics_check
        CHECK (
            (aerobic_training_effect IS NULL OR aerobic_training_effect >= 0)
            AND (anaerobic_training_effect IS NULL OR anaerobic_training_effect >= 0)
            AND (training_load IS NULL OR training_load >= 0)
        ),
    CONSTRAINT fitness_cycling_results_resistance_check
        CHECK (
            (resistance_min IS NULL OR resistance_min >= 0)
            AND (resistance_avg IS NULL OR resistance_avg >= 0)
            AND (resistance_max IS NULL OR resistance_max >= 0)
        ),
    CONSTRAINT fitness_cycling_results_secondary_check
        CHECK (calories IS NULL OR calories >= 0)
);

CREATE UNIQUE INDEX fitness_cycling_results_external_activity_uidx
    ON public.fitness_cycling_workout_results (external_provider, external_activity_id)
    WHERE external_provider IS NOT NULL
      AND external_activity_id IS NOT NULL;

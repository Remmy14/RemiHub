ALTER TABLE public.fitness_running_workout_results
    ADD COLUMN external_provider text,
    ADD COLUMN external_activity_id text,
    ADD COLUMN external_activity_uuid text,
    ADD COLUMN external_activity_name text,
    ADD COLUMN moving_duration_seconds integer,
    ADD COLUMN average_speed_meters_per_second numeric(12, 6),
    ADD COLUMN average_hr numeric(8, 2),
    ADD COLUMN max_hr numeric(8, 2),
    ADD COLUMN training_load numeric(10, 2),
    ADD COLUMN aerobic_training_effect numeric(5, 2),
    ADD COLUMN anaerobic_training_effect numeric(5, 2),
    ADD COLUMN training_effect_label text,
    ADD COLUMN vo2_max numeric(8, 2),
    ADD COLUMN hr_zone_1_seconds integer,
    ADD COLUMN hr_zone_2_seconds integer,
    ADD COLUMN hr_zone_3_seconds integer,
    ADD COLUMN hr_zone_4_seconds integer,
    ADD COLUMN hr_zone_5_seconds integer,
    ADD COLUMN average_cadence_spm numeric(8, 2),
    ADD COLUMN average_power_watts numeric(8, 2),
    ADD COLUMN average_stride_length_meters numeric(8, 4),
    ADD COLUMN elevation_gain_meters numeric(10, 2),
    ADD COLUMN elevation_loss_meters numeric(10, 2),
    ADD COLUMN calories numeric(10, 2),
    ADD COLUMN steps integer;

ALTER TABLE public.fitness_running_workout_results
    ADD CONSTRAINT fitness_running_results_external_provider_check
        CHECK (external_provider IS NULL OR external_provider IN ('GARMIN')),
    ADD CONSTRAINT fitness_running_results_external_activity_check
        CHECK (
            (external_provider IS NULL AND external_activity_id IS NULL)
            OR (external_provider IS NOT NULL AND external_activity_id IS NOT NULL)
        ),
    ADD CONSTRAINT fitness_running_results_moving_duration_check
        CHECK (moving_duration_seconds IS NULL OR moving_duration_seconds >= 0),
    ADD CONSTRAINT fitness_running_results_average_speed_check
        CHECK (average_speed_meters_per_second IS NULL OR average_speed_meters_per_second >= 0),
    ADD CONSTRAINT fitness_running_results_hr_check
        CHECK (
            (average_hr IS NULL OR average_hr >= 0)
            AND (max_hr IS NULL OR max_hr >= 0)
        ),
    ADD CONSTRAINT fitness_running_results_training_metrics_check
        CHECK (
            (training_load IS NULL OR training_load >= 0)
            AND (aerobic_training_effect IS NULL OR aerobic_training_effect >= 0)
            AND (anaerobic_training_effect IS NULL OR anaerobic_training_effect >= 0)
            AND (vo2_max IS NULL OR vo2_max >= 0)
        ),
    ADD CONSTRAINT fitness_running_results_hr_zones_check
        CHECK (
            (hr_zone_1_seconds IS NULL OR hr_zone_1_seconds >= 0)
            AND (hr_zone_2_seconds IS NULL OR hr_zone_2_seconds >= 0)
            AND (hr_zone_3_seconds IS NULL OR hr_zone_3_seconds >= 0)
            AND (hr_zone_4_seconds IS NULL OR hr_zone_4_seconds >= 0)
            AND (hr_zone_5_seconds IS NULL OR hr_zone_5_seconds >= 0)
        ),
    ADD CONSTRAINT fitness_running_results_efficiency_check
        CHECK (
            (average_cadence_spm IS NULL OR average_cadence_spm >= 0)
            AND (average_power_watts IS NULL OR average_power_watts >= 0)
            AND (average_stride_length_meters IS NULL OR average_stride_length_meters >= 0)
            AND (elevation_gain_meters IS NULL OR elevation_gain_meters >= 0)
            AND (elevation_loss_meters IS NULL OR elevation_loss_meters >= 0)
        ),
    ADD CONSTRAINT fitness_running_results_secondary_check
        CHECK (
            (calories IS NULL OR calories >= 0)
            AND (steps IS NULL OR steps >= 0)
        );

CREATE UNIQUE INDEX fitness_running_results_external_activity_uidx
    ON public.fitness_running_workout_results (external_provider, external_activity_id)
    WHERE external_provider IS NOT NULL
      AND external_activity_id IS NOT NULL;

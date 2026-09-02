DROP INDEX fitness_cycling_results_external_activity_uidx;

DROP TABLE public.fitness_cycling_workout_results;

ALTER TABLE public.fitness_scheduled_workouts
    DROP CONSTRAINT fitness_scheduled_workouts_duration_check;

ALTER TABLE public.fitness_scheduled_workouts
    DROP COLUMN planned_duration_seconds;

DROP TABLE public.fitness_cycling_workout_templates;

ALTER TABLE public.fitness_workout_templates
    DROP CONSTRAINT fitness_workout_templates_type_check;

ALTER TABLE public.fitness_workout_templates
    ADD CONSTRAINT fitness_workout_templates_type_check
        CHECK (workout_type IN ('RUNNING', 'LIFTING'));

-- Rollback semantic: the pre-0010 schema cannot represent planning-stopped
-- training-plan instances. Dropping stopped_at preserves scheduled/completed
-- workout history and leaves ACTIVE/COMPLETED lifecycle status unchanged.
ALTER TABLE public.fitness_training_plan_instances
    DROP COLUMN stopped_at;

DROP INDEX fitness_scheduled_workouts_series_date_idx;

ALTER TABLE public.fitness_scheduled_workouts
    DROP CONSTRAINT fitness_scheduled_workouts_recurring_series_id_fkey;

ALTER TABLE public.fitness_scheduled_workouts
    DROP COLUMN recurring_series_id;

DROP INDEX fitness_recurring_series_user_idempotency_uidx;

DROP INDEX fitness_recurring_series_user_status_idx;

DROP TABLE public.fitness_recurring_schedule_series;

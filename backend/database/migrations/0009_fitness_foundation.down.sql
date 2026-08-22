DROP INDEX weightlifting_entries_fitness_scheduled_workout_idx;

ALTER TABLE public.weightlifting_entries
    DROP CONSTRAINT weightlifting_entries_fitness_scheduled_workout_id_fkey;

ALTER TABLE public.weightlifting_entries
    DROP COLUMN fitness_scheduled_workout_id;

DROP INDEX fitness_notification_runs_user_date_phase_uidx;

DROP TABLE public.fitness_notification_runs;

DROP TABLE public.fitness_running_workout_results;

DROP INDEX fitness_scheduled_workouts_plan_instance_idx;

DROP INDEX fitness_scheduled_workouts_user_date_idx;

DROP TABLE public.fitness_scheduled_workouts;

DROP INDEX fitness_plan_instances_user_status_idx;

DROP TABLE public.fitness_training_plan_instances;

DROP INDEX fitness_plan_template_items_order_idx;

DROP TABLE public.fitness_training_plan_template_items;

DROP INDEX fitness_training_plan_templates_user_active_idx;

DROP TABLE public.fitness_training_plan_templates;

DROP INDEX fitness_lifting_template_exercises_order_idx;

DROP TABLE public.fitness_lifting_template_exercises;

DROP TABLE public.fitness_running_workout_templates;

DROP INDEX fitness_workout_templates_user_active_idx;

DROP TABLE public.fitness_workout_templates;

-- Rollback to the legacy 1-3 Weightlifting slot constraint is intentionally
-- impossible once slot-4+ settings or entries exist unless that data is
-- handled before this down migration runs.
ALTER TABLE public.weightlifting_entries
    DROP CONSTRAINT weightlifting_entries_slot_check;

ALTER TABLE public.weightlifting_entries
    ADD CONSTRAINT weightlifting_entries_slot_check
    CHECK (workout_day_slot BETWEEN 1 AND 3);

ALTER TABLE public.weightlifting_day_slots
    DROP CONSTRAINT weightlifting_day_slots_slot_check;

ALTER TABLE public.weightlifting_day_slots
    ADD CONSTRAINT weightlifting_day_slots_slot_check
    CHECK (slot BETWEEN 1 AND 3);

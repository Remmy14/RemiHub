ALTER TABLE public.weightlifting_day_slots
    DROP CONSTRAINT weightlifting_day_slots_slot_check;

ALTER TABLE public.weightlifting_day_slots
    ADD CONSTRAINT weightlifting_day_slots_slot_check
    CHECK (slot >= 1);

ALTER TABLE public.weightlifting_entries
    DROP CONSTRAINT weightlifting_entries_slot_check;

ALTER TABLE public.weightlifting_entries
    ADD CONSTRAINT weightlifting_entries_slot_check
    CHECK (workout_day_slot >= 1);

CREATE TABLE public.fitness_workout_templates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    name text NOT NULL,
    workout_type text NOT NULL,
    notes text,
    active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_workout_templates_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_workout_templates_name_check
        CHECK (char_length(btrim(name)) BETWEEN 1 AND 160),
    CONSTRAINT fitness_workout_templates_type_check
        CHECK (workout_type IN ('RUNNING', 'LIFTING'))
);

CREATE INDEX fitness_workout_templates_user_active_idx
    ON public.fitness_workout_templates (user_id, active, name);

CREATE TABLE public.fitness_running_workout_templates (
    template_id uuid PRIMARY KEY,
    planned_distance_miles numeric(8, 2) NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_running_templates_template_id_fkey
        FOREIGN KEY (template_id)
        REFERENCES public.fitness_workout_templates(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_running_templates_distance_check
        CHECK (planned_distance_miles >= 0)
);

CREATE TABLE public.fitness_lifting_template_exercises (
    template_id uuid NOT NULL,
    exercise_id uuid NOT NULL,
    display_order integer NOT NULL DEFAULT 0,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_lifting_template_exercises_pkey
        PRIMARY KEY (template_id, exercise_id),
    CONSTRAINT fitness_lifting_template_exercises_template_id_fkey
        FOREIGN KEY (template_id)
        REFERENCES public.fitness_workout_templates(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_lifting_template_exercises_exercise_id_fkey
        FOREIGN KEY (exercise_id)
        REFERENCES public.weightlifting_exercises(id)
        ON DELETE RESTRICT
);

CREATE INDEX fitness_lifting_template_exercises_order_idx
    ON public.fitness_lifting_template_exercises (template_id, display_order);

CREATE TABLE public.fitness_training_plan_templates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    name text NOT NULL,
    notes text,
    active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_training_plan_templates_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_training_plan_templates_name_check
        CHECK (char_length(btrim(name)) BETWEEN 1 AND 160)
);

CREATE INDEX fitness_training_plan_templates_user_active_idx
    ON public.fitness_training_plan_templates (user_id, active, name);

CREATE TABLE public.fitness_training_plan_template_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_template_id uuid NOT NULL,
    workout_template_id uuid NOT NULL,
    day_offset integer NOT NULL,
    display_order integer NOT NULL DEFAULT 0,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_plan_template_items_plan_template_id_fkey
        FOREIGN KEY (plan_template_id)
        REFERENCES public.fitness_training_plan_templates(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_plan_template_items_workout_template_id_fkey
        FOREIGN KEY (workout_template_id)
        REFERENCES public.fitness_workout_templates(id)
        ON DELETE RESTRICT,
    CONSTRAINT fitness_plan_template_items_day_offset_check
        CHECK (day_offset >= 0)
);

CREATE INDEX fitness_plan_template_items_order_idx
    ON public.fitness_training_plan_template_items (
        plan_template_id,
        day_offset,
        display_order
    );

CREATE TABLE public.fitness_training_plan_instances (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    plan_template_id uuid NOT NULL,
    start_date date NOT NULL,
    status text NOT NULL DEFAULT 'ACTIVE',
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_plan_instances_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_plan_instances_plan_template_id_fkey
        FOREIGN KEY (plan_template_id)
        REFERENCES public.fitness_training_plan_templates(id)
        ON DELETE RESTRICT,
    CONSTRAINT fitness_plan_instances_status_check
        CHECK (status IN ('ACTIVE', 'COMPLETED'))
);

CREATE INDEX fitness_plan_instances_user_status_idx
    ON public.fitness_training_plan_instances (user_id, status, start_date DESC);

CREATE TABLE public.fitness_scheduled_workouts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    workout_template_id uuid NOT NULL,
    plan_instance_id uuid,
    scheduled_date date NOT NULL,
    original_scheduled_date date NOT NULL,
    status text NOT NULL DEFAULT 'PLANNED',
    replacement_scheduled_workout_id uuid,
    planned_distance_miles numeric(8, 2),
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_scheduled_workouts_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_scheduled_workouts_template_id_fkey
        FOREIGN KEY (workout_template_id)
        REFERENCES public.fitness_workout_templates(id)
        ON DELETE RESTRICT,
    CONSTRAINT fitness_scheduled_workouts_plan_instance_id_fkey
        FOREIGN KEY (plan_instance_id)
        REFERENCES public.fitness_training_plan_instances(id)
        ON DELETE SET NULL,
    CONSTRAINT fitness_scheduled_workouts_replacement_id_fkey
        FOREIGN KEY (replacement_scheduled_workout_id)
        REFERENCES public.fitness_scheduled_workouts(id)
        ON DELETE SET NULL,
    CONSTRAINT fitness_scheduled_workouts_status_check
        CHECK (status IN ('PLANNED', 'COMPLETED', 'SKIPPED', 'RESCHEDULED')),
    CONSTRAINT fitness_scheduled_workouts_distance_check
        CHECK (planned_distance_miles IS NULL OR planned_distance_miles >= 0)
);

CREATE INDEX fitness_scheduled_workouts_user_date_idx
    ON public.fitness_scheduled_workouts (user_id, scheduled_date, status);

CREATE INDEX fitness_scheduled_workouts_plan_instance_idx
    ON public.fitness_scheduled_workouts (plan_instance_id, scheduled_date);

CREATE TABLE public.fitness_running_workout_results (
    scheduled_workout_id uuid PRIMARY KEY,
    planned_distance_miles numeric(8, 2) NOT NULL,
    completed_distance_miles numeric(8, 2) NOT NULL,
    duration_seconds integer NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_running_results_scheduled_id_fkey
        FOREIGN KEY (scheduled_workout_id)
        REFERENCES public.fitness_scheduled_workouts(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_running_results_planned_distance_check
        CHECK (planned_distance_miles >= 0),
    CONSTRAINT fitness_running_results_completed_distance_check
        CHECK (completed_distance_miles >= 0),
    CONSTRAINT fitness_running_results_duration_check
        CHECK (duration_seconds >= 0)
);

CREATE TABLE public.fitness_notification_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    fitness_date date NOT NULL,
    phase text NOT NULL,
    status text NOT NULL,
    notification_id bigint,
    scheduled_workout_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
    timezone text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_notification_runs_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_notification_runs_phase_check
        CHECK (phase IN ('morning', 'evening')),
    CONSTRAINT fitness_notification_runs_status_check
        CHECK (status IN ('inserted', 'no_workouts')),
    CONSTRAINT fitness_notification_runs_metadata_check
        CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE UNIQUE INDEX fitness_notification_runs_user_date_phase_uidx
    ON public.fitness_notification_runs (user_id, fitness_date, phase);

ALTER TABLE public.weightlifting_entries
    ADD COLUMN fitness_scheduled_workout_id uuid;

ALTER TABLE public.weightlifting_entries
    ADD CONSTRAINT weightlifting_entries_fitness_scheduled_workout_id_fkey
    FOREIGN KEY (fitness_scheduled_workout_id)
    REFERENCES public.fitness_scheduled_workouts(id)
    ON DELETE SET NULL;

CREATE INDEX weightlifting_entries_fitness_scheduled_workout_idx
    ON public.weightlifting_entries (fitness_scheduled_workout_id)
    WHERE fitness_scheduled_workout_id IS NOT NULL;

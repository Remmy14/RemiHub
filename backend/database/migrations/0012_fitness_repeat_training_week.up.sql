ALTER TABLE public.fitness_scheduled_workouts
    ADD COLUMN plan_template_item_id uuid;

ALTER TABLE public.fitness_scheduled_workouts
    ADD CONSTRAINT fitness_scheduled_workouts_plan_template_item_id_fkey
    FOREIGN KEY (plan_template_item_id)
    REFERENCES public.fitness_training_plan_template_items(id)
    ON DELETE SET NULL;

CREATE INDEX fitness_scheduled_workouts_plan_template_item_idx
    ON public.fitness_scheduled_workouts (plan_template_item_id)
    WHERE plan_template_item_id IS NOT NULL;

CREATE INDEX fitness_scheduled_workouts_plan_prescription_date_idx
    ON public.fitness_scheduled_workouts (
        plan_instance_id,
        plan_template_item_id,
        scheduled_date
    )
    WHERE plan_instance_id IS NOT NULL;

CREATE TABLE public.fitness_training_plan_week_repeats (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    plan_instance_id uuid NOT NULL,
    selected_week_start date NOT NULL,
    selected_week_end date NOT NULL,
    repeated_week_start date NOT NULL,
    repeated_week_end date NOT NULL,
    idempotency_key text,
    request_fingerprint text,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_plan_week_repeats_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_plan_week_repeats_plan_instance_id_fkey
        FOREIGN KEY (plan_instance_id)
        REFERENCES public.fitness_training_plan_instances(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_plan_week_repeats_week_check
        CHECK (
            selected_week_end = selected_week_start + 6
            AND repeated_week_start = selected_week_start + 7
            AND repeated_week_end = repeated_week_start + 6
        ),
    CONSTRAINT fitness_plan_week_repeats_idempotency_key_check
        CHECK (idempotency_key IS NULL OR char_length(btrim(idempotency_key)) BETWEEN 1 AND 160),
    CONSTRAINT fitness_plan_week_repeats_fingerprint_check
        CHECK (request_fingerprint IS NULL OR char_length(request_fingerprint) BETWEEN 1 AND 4000)
);

CREATE UNIQUE INDEX fitness_plan_week_repeats_user_idempotency_uidx
    ON public.fitness_training_plan_week_repeats (user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX fitness_plan_week_repeats_instance_created_idx
    ON public.fitness_training_plan_week_repeats (plan_instance_id, created_at DESC);

CREATE TABLE public.fitness_training_plan_week_repeat_workouts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    repeat_id uuid NOT NULL,
    role text NOT NULL,
    mutation_order integer NOT NULL DEFAULT 0,
    scheduled_workout_id uuid NOT NULL,
    scheduled_date_before date,
    scheduled_date_after date NOT NULL,
    plan_template_item_id uuid,
    workout_template_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fitness_plan_week_repeat_workouts_repeat_id_fkey
        FOREIGN KEY (repeat_id)
        REFERENCES public.fitness_training_plan_week_repeats(id)
        ON DELETE CASCADE,
    CONSTRAINT fitness_plan_week_repeat_workouts_template_id_fkey
        FOREIGN KEY (workout_template_id)
        REFERENCES public.fitness_workout_templates(id)
        ON DELETE RESTRICT,
    CONSTRAINT fitness_plan_week_repeat_workouts_role_check
        CHECK (role IN ('REPEATED', 'SHIFTED'))
);

CREATE INDEX fitness_plan_week_repeat_workouts_repeat_role_idx
    ON public.fitness_training_plan_week_repeat_workouts (repeat_id, role, mutation_order, id);

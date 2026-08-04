CREATE TABLE public.weightlifting_settings (
    user_id uuid PRIMARY KEY,
    weight_unit text NOT NULL DEFAULT 'lb',
    default_weight_increment numeric(8, 2) NOT NULL DEFAULT 5,
    default_target_reps integer NOT NULL DEFAULT 12,
    default_sets integer DEFAULT 3,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT weightlifting_settings_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT weightlifting_settings_unit_check
        CHECK (weight_unit IN ('lb', 'kg')),
    CONSTRAINT weightlifting_settings_increment_check
        CHECK (default_weight_increment >= 0 AND default_weight_increment <= 200),
    CONSTRAINT weightlifting_settings_reps_check
        CHECK (default_target_reps > 0 AND default_target_reps <= 500),
    CONSTRAINT weightlifting_settings_sets_check
        CHECK (default_sets IS NULL OR (default_sets > 0 AND default_sets <= 100))
);

CREATE TABLE public.weightlifting_day_slots (
    user_id uuid NOT NULL,
    slot integer NOT NULL,
    label text NOT NULL,
    weekday text,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT weightlifting_day_slots_pkey
        PRIMARY KEY (user_id, slot),
    CONSTRAINT weightlifting_day_slots_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT weightlifting_day_slots_slot_check
        CHECK (slot BETWEEN 1 AND 3),
    CONSTRAINT weightlifting_day_slots_label_check
        CHECK (char_length(btrim(label)) BETWEEN 1 AND 80),
    CONSTRAINT weightlifting_day_slots_weekday_check
        CHECK (
            weekday IS NULL
            OR weekday IN (
                'monday',
                'tuesday',
                'wednesday',
                'thursday',
                'friday',
                'saturday',
                'sunday'
            )
        )
);

CREATE TABLE public.weightlifting_exercises (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    name text NOT NULL,
    display_order integer NOT NULL DEFAULT 0,
    active boolean NOT NULL DEFAULT true,
    notes text,
    target_reps integer NOT NULL,
    target_sets integer,
    weight_increment numeric(8, 2) NOT NULL,
    weight_unit text NOT NULL DEFAULT 'lb',
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT weightlifting_exercises_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT weightlifting_exercises_name_check
        CHECK (char_length(btrim(name)) BETWEEN 1 AND 160),
    CONSTRAINT weightlifting_exercises_reps_check
        CHECK (target_reps > 0 AND target_reps <= 500),
    CONSTRAINT weightlifting_exercises_sets_check
        CHECK (target_sets IS NULL OR (target_sets > 0 AND target_sets <= 100)),
    CONSTRAINT weightlifting_exercises_increment_check
        CHECK (weight_increment >= 0 AND weight_increment <= 200),
    CONSTRAINT weightlifting_exercises_unit_check
        CHECK (weight_unit IN ('lb', 'kg'))
);

CREATE INDEX weightlifting_exercises_user_active_order_idx
    ON public.weightlifting_exercises (user_id, active, display_order, name);

CREATE TABLE public.weightlifting_entries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    exercise_id uuid NOT NULL,
    week_start date NOT NULL,
    workout_day_slot integer NOT NULL,
    workout_date date,
    weight numeric(8, 2) NOT NULL,
    reps integer NOT NULL,
    sets integer,
    notes text,
    completed boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT weightlifting_entries_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT weightlifting_entries_exercise_id_fkey
        FOREIGN KEY (exercise_id)
        REFERENCES public.weightlifting_exercises(id)
        ON DELETE RESTRICT,
    CONSTRAINT weightlifting_entries_slot_check
        CHECK (workout_day_slot BETWEEN 1 AND 3),
    CONSTRAINT weightlifting_entries_weight_check
        CHECK (weight >= 0 AND weight <= 2000),
    CONSTRAINT weightlifting_entries_reps_check
        CHECK (reps > 0 AND reps <= 500),
    CONSTRAINT weightlifting_entries_sets_check
        CHECK (sets IS NULL OR (sets > 0 AND sets <= 100)),
    CONSTRAINT weightlifting_entries_week_start_check
        CHECK (EXTRACT(ISODOW FROM week_start) = 1)
);

CREATE UNIQUE INDEX weightlifting_entries_exercise_week_slot_uidx
    ON public.weightlifting_entries (exercise_id, week_start, workout_day_slot);

CREATE INDEX weightlifting_entries_user_week_idx
    ON public.weightlifting_entries (user_id, week_start);

CREATE INDEX weightlifting_entries_exercise_history_idx
    ON public.weightlifting_entries (
        exercise_id,
        week_start DESC,
        workout_day_slot DESC,
        updated_at DESC
    );

CREATE TABLE public.mead_batches (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    name text NOT NULL,
    start_at timestamp with time zone NOT NULL,
    stage text NOT NULL DEFAULT 'primary',
    volume numeric(12, 4) NOT NULL,
    volume_unit text NOT NULL,
    original_gravity numeric(5, 4) NOT NULL,
    target_final_gravity numeric(5, 4),
    notes text,
    recipe_notes text,
    tosna_enabled boolean NOT NULL DEFAULT false,
    tosna_nutrient_name text,
    tosna_total_amount numeric(12, 4),
    tosna_unit text,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT mead_batches_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT mead_batches_name_check
        CHECK (char_length(btrim(name)) BETWEEN 1 AND 160),
    CONSTRAINT mead_batches_stage_check
        CHECK (stage IN ('primary', 'secondary', 'aging', 'bottled', 'archived')),
    CONSTRAINT mead_batches_volume_check
        CHECK (volume > 0),
    CONSTRAINT mead_batches_volume_unit_check
        CHECK (char_length(btrim(volume_unit)) BETWEEN 1 AND 32),
    CONSTRAINT mead_batches_original_gravity_check
        CHECK (original_gravity BETWEEN 0.9000 AND 1.3000),
    CONSTRAINT mead_batches_target_final_gravity_check
        CHECK (target_final_gravity IS NULL OR target_final_gravity BETWEEN 0.9000 AND 1.3000),
    CONSTRAINT mead_batches_tosna_check
        CHECK (
            tosna_enabled = false
            OR (
                tosna_total_amount IS NOT NULL
                AND tosna_total_amount > 0
                AND tosna_unit IS NOT NULL
                AND char_length(btrim(tosna_unit)) BETWEEN 1 AND 32
            )
        )
);

CREATE INDEX mead_batches_user_stage_start_idx
    ON public.mead_batches (user_id, stage, start_at DESC);

CREATE TABLE public.mead_recipe_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id uuid NOT NULL,
    name text NOT NULL,
    amount numeric(12, 4),
    unit text,
    notes text,
    display_order integer NOT NULL DEFAULT 0,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT mead_recipe_items_batch_id_fkey
        FOREIGN KEY (batch_id)
        REFERENCES public.mead_batches(id)
        ON DELETE CASCADE,
    CONSTRAINT mead_recipe_items_name_check
        CHECK (char_length(btrim(name)) BETWEEN 1 AND 160),
    CONSTRAINT mead_recipe_items_amount_check
        CHECK (amount IS NULL OR amount > 0),
    CONSTRAINT mead_recipe_items_unit_check
        CHECK (unit IS NULL OR char_length(btrim(unit)) <= 32)
);

CREATE INDEX mead_recipe_items_batch_order_idx
    ON public.mead_recipe_items (batch_id, display_order, created_at);

CREATE TABLE public.mead_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id uuid NOT NULL,
    event_at timestamp with time zone NOT NULL,
    event_type text NOT NULL,
    gravity numeric(5, 4),
    notes text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT mead_events_batch_id_fkey
        FOREIGN KEY (batch_id)
        REFERENCES public.mead_batches(id)
        ON DELETE CASCADE,
    CONSTRAINT mead_events_type_check
        CHECK (event_type IN ('gravity_reading', 'note', 'racking', 'nutrient_addition', 'stage_change', 'other')),
    CONSTRAINT mead_events_gravity_check
        CHECK (gravity IS NULL OR gravity BETWEEN 0.9000 AND 1.3000),
    CONSTRAINT mead_events_gravity_required_check
        CHECK (event_type <> 'gravity_reading' OR gravity IS NOT NULL),
    CONSTRAINT mead_events_metadata_check
        CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE INDEX mead_events_batch_timeline_idx
    ON public.mead_events (batch_id, event_at, created_at, id);

CREATE INDEX mead_events_batch_gravity_latest_idx
    ON public.mead_events (batch_id, event_at DESC, created_at DESC, id DESC)
    WHERE event_type = 'gravity_reading';

CREATE TABLE public.mead_tasks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id uuid NOT NULL,
    task_type text NOT NULL,
    title text NOT NULL,
    description text,
    due_at timestamp with time zone NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    completed_at timestamp with time zone,
    notified_at timestamp with time zone,
    notified_due_at timestamp with time zone,
    source text NOT NULL DEFAULT 'manual',
    source_key text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT mead_tasks_batch_id_fkey
        FOREIGN KEY (batch_id)
        REFERENCES public.mead_batches(id)
        ON DELETE CASCADE,
    CONSTRAINT mead_tasks_type_check
        CHECK (task_type IN ('check_gravity', 'add_nutrients', 'consider_racking', 'check_clarity_taste', 'consider_bottling', 'custom')),
    CONSTRAINT mead_tasks_status_check
        CHECK (status IN ('pending', 'completed', 'cancelled')),
    CONSTRAINT mead_tasks_title_check
        CHECK (char_length(btrim(title)) BETWEEN 1 AND 200),
    CONSTRAINT mead_tasks_source_check
        CHECK (source IN ('manual', 'tosna')),
    CONSTRAINT mead_tasks_source_key_check
        CHECK (source <> 'tosna' OR source_key IS NOT NULL),
    CONSTRAINT mead_tasks_metadata_check
        CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE UNIQUE INDEX mead_tasks_generated_source_uidx
    ON public.mead_tasks (batch_id, source, source_key)
    WHERE source_key IS NOT NULL;

CREATE INDEX mead_tasks_due_pending_idx
    ON public.mead_tasks (due_at, id)
    WHERE status = 'pending';

CREATE INDEX mead_tasks_batch_status_due_idx
    ON public.mead_tasks (batch_id, status, due_at);

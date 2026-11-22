ALTER TABLE public.finance_plaid_verification_lease
    ADD COLUMN run_trigger text CHECK (run_trigger IN ('manual', 'scheduled')),
    ADD COLUMN run_id uuid,
    ADD COLUMN scheduled_pending boolean NOT NULL DEFAULT false,
    ADD COLUMN monitoring_pending boolean NOT NULL DEFAULT false,
    ADD COLUMN monitoring_generation bigint NOT NULL DEFAULT 0,
    ADD COLUMN monitoring_retry_at timestamptz;

CREATE TABLE public.finance_verification_runs (
    id uuid PRIMARY KEY,
    run_trigger text NOT NULL CHECK (run_trigger = 'manual'),
    status text NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'partially_completed', 'failed')),
    requested_at timestamptz NOT NULL,
    started_at timestamptz,
    completed_at timestamptz,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    results jsonb NOT NULL DEFAULT '{"connections":[],"accounts":[]}'::jsonb,
    error_code text,
    monitoring_reconciled boolean NOT NULL DEFAULT false
);

CREATE INDEX finance_verification_runs_requested_at_idx
    ON public.finance_verification_runs (requested_at DESC);

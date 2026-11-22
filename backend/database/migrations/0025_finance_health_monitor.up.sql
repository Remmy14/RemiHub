CREATE TABLE public.finance_health_monitor_state (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    assessed_on date,
    assessed_at timestamptz,
    evidence_hash text,
    readiness text,
    previous_readiness text,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE public.finance_health_monitor_issues (
    issue_key text PRIMARY KEY,
    source_type text NOT NULL,
    source_id text NOT NULL,
    code text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('critical', 'warning')),
    active boolean NOT NULL,
    occurrence integer NOT NULL DEFAULT 1 CHECK (occurrence > 0),
    opened_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    resolved_at timestamptz,
    notified boolean NOT NULL DEFAULT false
);

CREATE INDEX finance_health_monitor_active_issues_idx
    ON public.finance_health_monitor_issues (active, source_type, source_id);

CREATE TABLE public.finance_health_monitor_events (
    event_key text PRIMARY KEY,
    event_type text NOT NULL CHECK (event_type IN ('opened', 'resolved', 'reminder', 'suppressed')),
    issue_key text,
    target_month date,
    days_before integer,
    user_id uuid REFERENCES public.remihub_users(id) ON DELETE SET NULL,
    notification_id bigint,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX finance_health_monitor_events_recent_idx
    ON public.finance_health_monitor_events (created_at DESC);

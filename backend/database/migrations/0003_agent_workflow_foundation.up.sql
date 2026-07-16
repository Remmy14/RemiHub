CREATE SCHEMA agent;

COMMENT ON SCHEMA agent IS
    'Durable RemiHub agent cards, messages, approvals, runs, and audit events';

CREATE TABLE agent.cards (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title text NOT NULL,
    description text NOT NULL,
    status text NOT NULL DEFAULT 'planning_queued',
    revision integer NOT NULL DEFAULT 1,
    base_branch text NOT NULL DEFAULT 'main',
    feature_branch text,
    worktree_path text,
    codex_thread_id text,
    created_by uuid NOT NULL,
    closed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT agent_cards_created_by_fkey
        FOREIGN KEY (created_by)
        REFERENCES public.remihub_users(id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_cards_title_check
        CHECK (char_length(btrim(title)) BETWEEN 1 AND 160),
    CONSTRAINT agent_cards_description_check
        CHECK (char_length(btrim(description)) BETWEEN 1 AND 20000),
    CONSTRAINT agent_cards_revision_check
        CHECK (revision > 0),
    CONSTRAINT agent_cards_status_check
        CHECK (
            status IN (
                'planning_queued',
                'planning',
                'awaiting_feedback',
                'awaiting_implementation_approval',
                'implementation_queued',
                'implementing',
                'review_ready',
                'deployment_queued',
                'deploying',
                'completed',
                'failed',
                'cancelled',
                'closed'
            )
        ),
    CONSTRAINT agent_cards_closed_at_check
        CHECK (
            (status = 'closed' AND closed_at IS NOT NULL)
            OR (status <> 'closed' AND closed_at IS NULL)
        )
);

CREATE UNIQUE INDEX agent_one_open_card_uidx
    ON agent.cards ((1))
    WHERE status NOT IN ('completed', 'cancelled', 'closed');

CREATE INDEX agent_cards_created_at_idx
    ON agent.cards (created_at DESC);

CREATE TRIGGER trg_agent_cards_updated_at
    BEFORE UPDATE ON agent.cards
    FOR EACH ROW
    EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE agent.messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    card_id uuid NOT NULL,
    author_type text NOT NULL,
    content text NOT NULL,
    created_by uuid,
    client_message_id uuid,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT agent_messages_card_id_fkey
        FOREIGN KEY (card_id)
        REFERENCES agent.cards(id)
        ON DELETE CASCADE,
    CONSTRAINT agent_messages_created_by_fkey
        FOREIGN KEY (created_by)
        REFERENCES public.remihub_users(id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_messages_author_type_check
        CHECK (author_type IN ('user', 'agent', 'worker', 'system')),
    CONSTRAINT agent_messages_content_check
        CHECK (char_length(btrim(content)) BETWEEN 1 AND 20000),
    CONSTRAINT agent_messages_user_author_check
        CHECK (author_type <> 'user' OR created_by IS NOT NULL)
);

CREATE UNIQUE INDEX agent_messages_client_message_uidx
    ON agent.messages (card_id, client_message_id)
    WHERE client_message_id IS NOT NULL;

CREATE INDEX agent_messages_card_created_idx
    ON agent.messages (card_id, created_at, id);

CREATE TABLE agent.approvals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    card_id uuid NOT NULL,
    approval_type text NOT NULL,
    decision text NOT NULL,
    card_revision integer NOT NULL,
    decided_by uuid NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT agent_approvals_card_id_fkey
        FOREIGN KEY (card_id)
        REFERENCES agent.cards(id)
        ON DELETE CASCADE,
    CONSTRAINT agent_approvals_decided_by_fkey
        FOREIGN KEY (decided_by)
        REFERENCES public.remihub_users(id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_approvals_type_check
        CHECK (approval_type IN ('implementation', 'deployment')),
    CONSTRAINT agent_approvals_decision_check
        CHECK (decision IN ('approved', 'rejected', 'revoked')),
    CONSTRAINT agent_approvals_revision_check
        CHECK (card_revision > 0),
    CONSTRAINT agent_approvals_notes_check
        CHECK (notes IS NULL OR char_length(notes) <= 2000)
);

CREATE UNIQUE INDEX agent_approvals_approved_revision_uidx
    ON agent.approvals (card_id, approval_type, card_revision)
    WHERE decision = 'approved';

CREATE INDEX agent_approvals_card_created_idx
    ON agent.approvals (card_id, created_at, id);

CREATE TABLE agent.runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    card_id uuid NOT NULL,
    phase text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    card_revision integer NOT NULL,
    input_message_id uuid,
    requested_by uuid NOT NULL,
    worker_id text,
    lease_expires_at timestamp with time zone,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    error_message text,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT agent_runs_card_id_fkey
        FOREIGN KEY (card_id)
        REFERENCES agent.cards(id)
        ON DELETE CASCADE,
    CONSTRAINT agent_runs_input_message_id_fkey
        FOREIGN KEY (input_message_id)
        REFERENCES agent.messages(id)
        ON DELETE SET NULL,
    CONSTRAINT agent_runs_requested_by_fkey
        FOREIGN KEY (requested_by)
        REFERENCES public.remihub_users(id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_runs_phase_check
        CHECK (phase IN ('planning', 'implementation', 'deployment')),
    CONSTRAINT agent_runs_status_check
        CHECK (
            status IN (
                'queued',
                'claimed',
                'running',
                'succeeded',
                'failed',
                'blocked',
                'cancelled'
            )
        ),
    CONSTRAINT agent_runs_revision_check
        CHECK (card_revision > 0),
    CONSTRAINT agent_runs_error_message_check
        CHECK (error_message IS NULL OR char_length(error_message) <= 10000)
);

CREATE UNIQUE INDEX agent_one_active_run_uidx
    ON agent.runs ((1))
    WHERE status IN ('queued', 'claimed', 'running');

CREATE INDEX agent_runs_card_created_idx
    ON agent.runs (card_id, created_at, id);

CREATE INDEX agent_runs_queue_idx
    ON agent.runs (created_at, id)
    WHERE status = 'queued';

CREATE TRIGGER trg_agent_runs_updated_at
    BEFORE UPDATE ON agent.runs
    FOR EACH ROW
    EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE agent.events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    card_id uuid NOT NULL,
    event_type text NOT NULL,
    actor_type text NOT NULL,
    actor_user_id uuid,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT agent_events_card_id_fkey
        FOREIGN KEY (card_id)
        REFERENCES agent.cards(id)
        ON DELETE CASCADE,
    CONSTRAINT agent_events_actor_user_id_fkey
        FOREIGN KEY (actor_user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_events_event_type_check
        CHECK (char_length(btrim(event_type)) BETWEEN 1 AND 100),
    CONSTRAINT agent_events_actor_type_check
        CHECK (actor_type IN ('user', 'agent', 'worker', 'system')),
    CONSTRAINT agent_events_user_actor_check
        CHECK (actor_type <> 'user' OR actor_user_id IS NOT NULL)
);

CREATE INDEX agent_events_card_created_idx
    ON agent.events (card_id, created_at, id);

DO $grant_agent_access$
DECLARE
    app_role_name text;
BEGIN
    IF session_user ~ '_migrator$' THEN
        app_role_name := regexp_replace(session_user, '_migrator$', '_app');
    ELSIF current_database() = 'remihub' THEN
        app_role_name := 'remihub_app';
    ELSIF current_database() = 'remihub_qa' THEN
        app_role_name := 'remihub_qa_app';
    END IF;

    IF app_role_name IS NOT NULL
       AND EXISTS (
            SELECT 1
            FROM pg_roles
            WHERE rolname = app_role_name
       ) THEN
        EXECUTE format(
            'GRANT USAGE ON SCHEMA agent TO %I',
            app_role_name
        );
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE ON agent.cards, agent.runs TO %I',
            app_role_name
        );
        EXECUTE format(
            'GRANT SELECT, INSERT ON agent.messages, agent.approvals, agent.events TO %I',
            app_role_name
        );
    ELSE
        RAISE NOTICE
            'Agent schema created without application grants; no matching app role for session user %',
            session_user;
    END IF;
END;
$grant_agent_access$;

ALTER TABLE agent.cards
    ADD COLUMN resume_status text,
    ADD COLUMN blocked_reason text,
    ADD COLUMN blocked_until timestamp with time zone;

ALTER TABLE agent.cards
    DROP CONSTRAINT agent_cards_status_check;

ALTER TABLE agent.cards
    ADD CONSTRAINT agent_cards_status_check
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
                'blocked',
                'completed',
                'failed',
                'cancelled',
                'closed'
            )
        ),
    ADD CONSTRAINT agent_cards_blocked_state_check
        CHECK (
            (
                status = 'blocked'
                AND resume_status IN (
                    'planning_queued',
                    'implementation_queued',
                    'deployment_queued'
                )
                AND blocked_reason IS NOT NULL
                AND char_length(btrim(blocked_reason)) BETWEEN 1 AND 2000
                AND blocked_until IS NOT NULL
            )
            OR
            (
                status <> 'blocked'
                AND resume_status IS NULL
                AND blocked_reason IS NULL
                AND blocked_until IS NULL
            )
        );

ALTER TABLE agent.runs
    ADD COLUMN lease_token uuid,
    ADD COLUMN attempt_count integer NOT NULL DEFAULT 0,
    ADD COLUMN last_heartbeat_at timestamp with time zone,
    ADD COLUMN available_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ADD COLUMN blocked_reason text,
    ADD COLUMN result_message_id uuid,
    ADD COLUMN result_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE agent.runs
    ADD CONSTRAINT agent_runs_result_message_id_fkey
        FOREIGN KEY (result_message_id)
        REFERENCES agent.messages(id)
        ON DELETE SET NULL,
    ADD CONSTRAINT agent_runs_attempt_count_check
        CHECK (attempt_count >= 0),
    ADD CONSTRAINT agent_runs_lease_state_check
        CHECK (
            (
                status IN ('claimed', 'running')
                AND worker_id IS NOT NULL
                AND lease_token IS NOT NULL
                AND lease_expires_at IS NOT NULL
                AND last_heartbeat_at IS NOT NULL
            )
            OR
            (
                status NOT IN ('claimed', 'running')
                AND lease_token IS NULL
                AND lease_expires_at IS NULL
            )
        ),
    ADD CONSTRAINT agent_runs_blocked_state_check
        CHECK (
            (
                status = 'blocked'
                AND blocked_reason IS NOT NULL
                AND char_length(btrim(blocked_reason)) BETWEEN 1 AND 2000
            )
            OR
            (
                status <> 'blocked'
                AND blocked_reason IS NULL
            )
        ),
    ADD CONSTRAINT agent_runs_result_state_check
        CHECK (status <> 'succeeded' OR result_message_id IS NOT NULL);

DROP INDEX agent.agent_runs_queue_idx;

CREATE INDEX agent_runs_queue_idx
    ON agent.runs (available_at, created_at, id)
    WHERE status IN ('queued', 'blocked');

DO $grant_agent_worker_access$
DECLARE
    worker_role_name text;
BEGIN
    IF session_user ~ '_migrator$' THEN
        worker_role_name := regexp_replace(
            session_user,
            '_migrator$',
            '_agent_worker'
        );
    ELSIF current_database() = 'remihub' THEN
        worker_role_name := 'remihub_agent_worker';
    ELSIF current_database() = 'remihub_qa' THEN
        worker_role_name := 'remihub_qa_agent_worker';
    END IF;

    IF worker_role_name IS NOT NULL
       AND EXISTS (
            SELECT 1
            FROM pg_roles
            WHERE rolname = worker_role_name
       ) THEN
        EXECUTE format(
            'GRANT USAGE ON SCHEMA agent TO %I',
            worker_role_name
        );
        EXECUTE format(
            'GRANT SELECT ON agent.cards, agent.messages, agent.approvals, agent.runs TO %I',
            worker_role_name
        );
        EXECUTE format(
            'GRANT UPDATE (status, resume_status, blocked_reason, blocked_until, feature_branch, worktree_path, codex_thread_id) ON agent.cards TO %I',
            worker_role_name
        );
        EXECUTE format(
            'GRANT UPDATE (status, worker_id, lease_token, lease_expires_at, attempt_count, last_heartbeat_at, available_at, blocked_reason, started_at, finished_at, error_message, result_message_id, result_metadata) ON agent.runs TO %I',
            worker_role_name
        );
        EXECUTE format(
            'GRANT INSERT ON agent.messages, agent.events TO %I',
            worker_role_name
        );
    ELSE
        RAISE NOTICE
            'Agent worker lease schema created without worker grants; role % is not present',
            worker_role_name;
    END IF;
END;
$grant_agent_worker_access$;

UPDATE agent.runs
SET status = 'failed',
    error_message = COALESCE(
        error_message,
        'Run interrupted by rollback of agent worker leases'
    ),
    finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP),
    lease_token = NULL,
    lease_expires_at = NULL,
    blocked_reason = NULL
WHERE status IN ('claimed', 'running', 'blocked');

DO $revoke_agent_worker_access$
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
            'REVOKE UPDATE (status, resume_status, blocked_reason, blocked_until, feature_branch, worktree_path, codex_thread_id) ON agent.cards FROM %I',
            worker_role_name
        );
        EXECUTE format(
            'REVOKE UPDATE (status, worker_id, lease_token, lease_expires_at, attempt_count, last_heartbeat_at, available_at, blocked_reason, started_at, finished_at, error_message, result_message_id, result_metadata) ON agent.runs FROM %I',
            worker_role_name
        );
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA agent FROM %I',
            worker_role_name
        );
        EXECUTE format(
            'REVOKE USAGE ON SCHEMA agent FROM %I',
            worker_role_name
        );
    END IF;
END;
$revoke_agent_worker_access$;

DROP INDEX agent.agent_runs_queue_idx;

ALTER TABLE agent.runs
    DROP CONSTRAINT agent_runs_result_state_check,
    DROP CONSTRAINT agent_runs_blocked_state_check,
    DROP CONSTRAINT agent_runs_lease_state_check,
    DROP CONSTRAINT agent_runs_attempt_count_check,
    DROP CONSTRAINT agent_runs_result_message_id_fkey,
    DROP COLUMN result_metadata,
    DROP COLUMN result_message_id,
    DROP COLUMN blocked_reason,
    DROP COLUMN available_at,
    DROP COLUMN last_heartbeat_at,
    DROP COLUMN attempt_count,
    DROP COLUMN lease_token;

CREATE INDEX agent_runs_queue_idx
    ON agent.runs (created_at, id)
    WHERE status = 'queued';

ALTER TABLE agent.cards
    DROP CONSTRAINT agent_cards_blocked_state_check;

UPDATE agent.cards
SET status = 'failed',
    resume_status = NULL,
    blocked_reason = NULL,
    blocked_until = NULL
WHERE status = 'blocked';

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
                'completed',
                'failed',
                'cancelled',
                'closed'
            )
        );

ALTER TABLE agent.cards
    DROP COLUMN blocked_until,
    DROP COLUMN blocked_reason,
    DROP COLUMN resume_status;

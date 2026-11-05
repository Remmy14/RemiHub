-- Stop workers first. Historical review records must remain available for audit.
-- Refuse destructive schema rollback once review data exists; roll back scheduling
-- and readers instead, keeping this additive schema and the deployment gate.
DO $guard$ BEGIN
 IF EXISTS (SELECT 1 FROM agent.runs WHERE review_cycle IS NOT NULL OR phase = 'review')
 OR EXISTS (SELECT 1 FROM agent.cards WHERE status IN ('review_queued','reviewing','review_ready','deployment_queued','deploying') OR resume_status = 'review_queued') THEN
 RAISE EXCEPTION 'Independent review history/in-flight cards require audit-preserving application rollback';
 END IF;
END; $guard$;
-- An unused-schema rollback leaves deployments disabled, including old workers.
ALTER TABLE agent.cards ADD CONSTRAINT agent_review_rollback_deployment_guard
 CHECK (status NOT IN ('review_ready','deployment_queued','deploying'));
DROP TRIGGER agent_independent_review_deployment_gate ON agent.cards;
DROP FUNCTION agent.require_independent_review_for_deployment();
DO $grants$
DECLARE worker_role text;
BEGIN
 IF session_user ~ '_migrator$' THEN worker_role := regexp_replace(session_user, '_migrator$', '_agent_worker');
 ELSIF current_database() = 'remihub' THEN worker_role := 'remihub_agent_worker';
 ELSIF current_database() = 'remihub_qa' THEN worker_role := 'remihub_qa_agent_worker'; END IF;
 IF worker_role IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = worker_role) THEN
 EXECUTE format('REVOKE INSERT ON agent.runs FROM %I', worker_role);
 END IF;
END;
$grants$;
DROP INDEX agent.agent_review_attempt_uidx;
ALTER TABLE agent.runs DROP CONSTRAINT agent_review_identity_check, DROP COLUMN review_cycle, DROP COLUMN review_attempt;
ALTER TABLE agent.runs DROP CONSTRAINT agent_runs_phase_check;
ALTER TABLE agent.runs ADD CONSTRAINT agent_runs_phase_check CHECK (phase IN ('planning','implementation','deployment'));
ALTER TABLE agent.cards DROP CONSTRAINT agent_cards_status_check;
ALTER TABLE agent.cards ADD CONSTRAINT agent_cards_status_check CHECK (status IN ('planning_queued', 'planning', 'awaiting_feedback', 'awaiting_implementation_approval', 'implementation_queued', 'implementing', 'review_ready', 'deployment_queued', 'deploying', 'blocked', 'completed', 'failed', 'cancelled', 'closed'));
ALTER TABLE agent.cards DROP CONSTRAINT agent_cards_blocked_state_check;
ALTER TABLE agent.cards ADD CONSTRAINT agent_cards_blocked_state_check CHECK (
 (status = 'blocked' AND resume_status IN ('planning_queued','implementation_queued','deployment_queued')
 AND blocked_reason IS NOT NULL AND char_length(btrim(blocked_reason)) BETWEEN 1 AND 2000 AND blocked_until IS NOT NULL)
 OR (status <> 'blocked' AND resume_status IS NULL AND blocked_reason IS NULL AND blocked_until IS NULL));

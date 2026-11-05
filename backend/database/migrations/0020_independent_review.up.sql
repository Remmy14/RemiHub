-- No rows are grandfathered into passing independent review.
ALTER TABLE agent.cards DROP CONSTRAINT IF EXISTS agent_review_rollback_deployment_guard;
ALTER TABLE agent.cards DROP CONSTRAINT agent_cards_status_check;
ALTER TABLE agent.cards ADD CONSTRAINT agent_cards_status_check CHECK (status IN ('planning_queued', 'planning', 'awaiting_feedback', 'awaiting_implementation_approval', 'implementation_queued', 'implementing', 'review_queued', 'reviewing', 'review_ready', 'deployment_queued', 'deploying', 'blocked', 'completed', 'failed', 'cancelled', 'closed'));
ALTER TABLE agent.cards DROP CONSTRAINT agent_cards_blocked_state_check;
ALTER TABLE agent.cards ADD CONSTRAINT agent_cards_blocked_state_check CHECK (
 (status = 'blocked' AND resume_status IN ('planning_queued','implementation_queued','review_queued','deployment_queued')
 AND blocked_reason IS NOT NULL AND char_length(btrim(blocked_reason)) BETWEEN 1 AND 2000 AND blocked_until IS NOT NULL)
 OR (status <> 'blocked' AND resume_status IS NULL AND blocked_reason IS NULL AND blocked_until IS NULL));
ALTER TABLE agent.runs DROP CONSTRAINT agent_runs_phase_check;
ALTER TABLE agent.runs ADD CONSTRAINT agent_runs_phase_check CHECK (phase IN ('planning','implementation','review','deployment'));
ALTER TABLE agent.runs ADD COLUMN review_cycle uuid, ADD COLUMN review_attempt integer;
ALTER TABLE agent.runs ADD CONSTRAINT agent_review_identity_check CHECK (
 (review_cycle IS NULL AND review_attempt IS NULL AND phase <> 'review')
 OR (review_cycle IS NOT NULL AND review_attempt IS NOT NULL AND review_attempt BETWEEN 1 AND 3));
-- Per authorized revision, even a different cycle cannot reset the budget.
CREATE UNIQUE INDEX agent_review_attempt_uidx ON agent.runs(card_id, card_revision, review_attempt) WHERE phase = 'review';
DO $grants$
DECLARE worker_role text;
BEGIN
 IF session_user ~ '_migrator$' THEN worker_role := regexp_replace(session_user, '_migrator$', '_agent_worker');
 ELSIF current_database() = 'remihub' THEN worker_role := 'remihub_agent_worker';
 ELSIF current_database() = 'remihub_qa' THEN worker_role := 'remihub_qa_agent_worker'; END IF;
 IF worker_role IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = worker_role) THEN
 EXECUTE format('GRANT INSERT ON agent.runs TO %I', worker_role);
 EXECUTE format('GRANT UPDATE (review_cycle, review_attempt) ON agent.runs TO %I', worker_role);
 END IF;
END;
$grants$;

-- Keep rollback/older application code from silently reopening unreviewed
-- deployment approval. The application also verifies the live candidate bytes.
CREATE FUNCTION agent.require_independent_review_for_deployment()
RETURNS trigger LANGUAGE plpgsql AS $review_gate$
BEGIN
 IF NEW.status IN ('deployment_queued', 'deploying') AND NOT EXISTS (
   SELECT 1 FROM agent.runs AS implementation
   JOIN agent.runs AS review ON review.card_id = implementation.card_id
     AND review.card_revision = implementation.card_revision
     AND review.phase = 'review' AND review.status = 'succeeded'
     AND review.result_metadata #>> '{independent_review,verdict}' = 'pass'
     AND review.result_metadata #>> '{independent_review,implementation_run_id}' = implementation.id::text
     AND review.result_metadata #>> '{independent_review,candidate_digest}' = implementation.result_metadata ->> 'candidate_digest'
   JOIN agent.approvals AS approval ON approval.card_id = implementation.card_id
     AND approval.card_revision = implementation.card_revision
     AND approval.approval_type = 'deployment' AND approval.decision = 'approved'
   WHERE implementation.card_id = NEW.id AND implementation.card_revision = NEW.revision
     AND implementation.phase = 'implementation' AND implementation.status = 'succeeded'
     AND implementation.id = (SELECT latest.id FROM agent.runs AS latest
       WHERE latest.card_id = NEW.id AND latest.card_revision = NEW.revision
         AND latest.phase = 'implementation' AND latest.status = 'succeeded'
       ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1)
 ) THEN
   RAISE EXCEPTION 'Deployment requires independent review and explicit human approval';
 END IF;
 RETURN NEW;
END;
$review_gate$;
CREATE TRIGGER agent_independent_review_deployment_gate
BEFORE INSERT OR UPDATE OF status ON agent.cards
FOR EACH ROW EXECUTE FUNCTION agent.require_independent_review_for_deployment();

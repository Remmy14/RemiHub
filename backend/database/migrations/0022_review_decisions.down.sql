-- Refuse destructive rollback while new workflow state/history requires new code.
DO $guard$ BEGIN
 IF EXISTS (SELECT 1 FROM agent.cards WHERE status='review_corrections_paused')
 OR EXISTS (SELECT 1 FROM agent.runs WHERE review_attempt > 3)
 OR EXISTS (SELECT 1 FROM agent.runs WHERE result_metadata #>> '{independent_review,decision}' IS NOT NULL)
 OR EXISTS (
   SELECT 1 FROM agent.runs AS run JOIN agent.cards AS card ON card.id = run.card_id
   WHERE card.status NOT IN ('cancelled', 'closed', 'completed')
     AND run.status IN ('queued', 'claimed', 'running', 'blocked', 'failed')
     AND (run.result_metadata ?| ARRAY['correction_allowance', 'refresh_base', 'review_execution_allowance', 'review_executions']
       OR run.result_metadata #> '{review_source,correction_allowance}' IS NOT NULL
       OR EXISTS (SELECT 1 FROM agent.events AS event WHERE event.card_id = run.card_id
         AND event.event_type IN ('review.corrections_continued', 'review.correction_queued', 'review.execution_retry_requested', 'review.execution_retry_queued')
         AND COALESCE(event.payload ->> 'run_id', event.payload ->> 'review_run_id') = run.id::text))
 )
 THEN RAISE EXCEPTION 'Drain/reconcile review decisions before schema rollback; no history is discarded';
 END IF;
END $guard$;
ALTER TABLE agent.cards DROP CONSTRAINT agent_cards_status_check;
ALTER TABLE agent.cards ADD CONSTRAINT agent_cards_status_check CHECK ((status = ANY (ARRAY['planning_queued'::text, 'planning'::text, 'awaiting_feedback'::text, 'awaiting_implementation_approval'::text, 'implementation_queued'::text, 'implementing'::text, 'review_queued'::text, 'reviewing'::text, 'review_ready'::text, 'deployment_queued'::text, 'deploying'::text, 'blocked'::text, 'completed'::text, 'failed'::text, 'cancelled'::text, 'closed'::text])));
ALTER TABLE agent.runs DROP CONSTRAINT agent_review_identity_check;
ALTER TABLE agent.runs ADD CONSTRAINT agent_review_identity_check CHECK ((((review_cycle IS NULL) AND (review_attempt IS NULL) AND (phase <> 'review'::text)) OR ((review_cycle IS NOT NULL) AND (review_attempt IS NOT NULL) AND ((review_attempt >= 1) AND (review_attempt <= 3)))));
CREATE OR REPLACE FUNCTION agent.require_independent_review_for_deployment()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$
;

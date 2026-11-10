"""Admin-only failed-closed review recovery: frozen-candidate and retry-budget guards."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from backend.core.agent_state import CardStatus, require_card_transition
from backend.services import agent_service as svc

CARD = "b7c138e5-7f02-41e6-b590-7f57239e95ee"
RUN = "6f640dfb-db73-4aa8-aaa1-5d9aae13eb72"
IMPLEMENTATION = "ed0c04f0-9901-4c2e-abc1-a2ec44b06184"
DIGEST = "a" * 64


def fixtures():
    impl_meta = {"candidate_digest": DIGEST, "repository_scope": "backend"}
    source = {"review_cycle": IMPLEMENTATION, "review_attempt": 1,
              "implementation_run_id": IMPLEMENTATION, "implementation_metadata": impl_meta}
    evidence = {"verdict": "blocked", "findings": [], "summary": "Review failed closed: ValueError",
                "failure_type": "ValueError", "thread_id": None, "turn_id": None,
                "review_cycle": IMPLEMENTATION, "review_attempt": 1,
                "implementation_run_id": IMPLEMENTATION,
                "card_revision": 1, "candidate_digest": DIGEST}
    card = {"id": CARD, "status": "blocked", "resume_status": "review_queued",
            "repository_scope": "backend", "revision": 1}
    review = {"id": RUN, "card_revision": 1, "phase": "review", "status": "succeeded",
              "attempt_count": 1, "review_cycle": IMPLEMENTATION, "review_attempt": 1,
              "result_metadata": {"review_source": source, "independent_review": evidence},
              "result_message_id": "original-message", "finished_at": "2026-11-09T09:04:46",
              "started_at": "2026-11-09T09:04:01", "worker_id": "reviewer-backend-01",
              "error_message": None, "blocked_reason": None}
    implementation = {"id": IMPLEMENTATION, "result_metadata": impl_meta}
    return card, review, implementation


class FailedClosedReviewRecoveryTests(unittest.TestCase):
    def test_only_fail_closed_review_is_advertised(self):
        card, review, _ = fixtures()
        self.assertIn("retry_review", svc._allowed_actions(card, latest_run=review))
        review["result_metadata"]["independent_review"].pop("failure_type")
        self.assertNotIn("retry_review", svc._allowed_actions(card, latest_run=review))
        self.assertIn("cancel", svc._allowed_actions(card, latest_run=review))

    def test_typed_malformed_response_with_thread_ids_can_retry(self):
        card, review, impl = fixtures()
        evidence = review["result_metadata"]["independent_review"]
        evidence.update({
            "summary": "Review failed closed: ReviewOutputValidationError",
            "failure_type": "ReviewOutputValidationError",
            "failure_code": "review_output_invalid",
            "thread_id": "codex-reviewer-thread",
            "turn_id": "codex-reviewer-turn",
        })
        self.assertIn("retry_review", svc._allowed_actions(card, latest_run=review))
        result, error, conn, _, event, _, _ = self.run_recovery(
            card=card, review=review, implementation=impl)
        self.assertIsNone(error)
        self.assertEqual(result["status"], "review_queued")
        conn.commit.assert_called_once()
        self.assertEqual(event.call_args.kwargs["payload"]["previous_review"]["turn_id"],
                         "codex-reviewer-turn")

    def test_unclassified_and_legacy_blocked_reviews_with_thread_ids_cannot_retry(self):
        for typed in (False, True):
            with self.subTest(typed=typed):
                card, review, _ = fixtures()
                evidence = review["result_metadata"]["independent_review"]
                evidence["thread_id"] = "review-thread"
                if typed:
                    evidence["failure_type"] = "ReviewOutputValidationError"
                    evidence["failure_code"] = "review_execution_failed"
                self.assertNotIn("retry_review", svc._allowed_actions(card, latest_run=review))

    def test_review_queued_transition_requires_explicit_status_change(self):
        require_card_transition(CardStatus.BLOCKED, CardStatus.REVIEW_QUEUED)
        with self.assertRaises(ValueError):
            require_card_transition(CardStatus.COMPLETED, CardStatus.REVIEW_QUEUED)

    def run_recovery(self, *, card=None, review=None, implementation=None, run_id=RUN,
                     digest=DIGEST, tamper=False):
        default_card, default_review, default_impl = fixtures()
        card = card or default_card
        review = review or default_review
        implementation = implementation or default_impl
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [review, implementation]
        cur.rowcount = 1
        with (patch.object(svc, "get_db_conn", return_value=conn),
              patch.object(svc, "put_db_conn"),
              patch.object(svc, "_locked_card", return_value=card),
              patch.object(svc, "_row_to_dict", side_effect=lambda c, r: r),
              patch.object(svc, "_insert_event") as event,
              patch.object(svc, "_update_card_status") as status,
              patch.object(svc, "_card_detail", return_value={"status": "review_queued"}),
              patch("backend.core.codex_review.verify_candidate",
                    side_effect=(ValueError("tampered") if tamper else None)) as verify):
            try:
                result = svc.retry_infrastructure_review(card_id=CARD, review_run_id=run_id,
                        candidate_digest=digest, requested_by="admin")
                error = None
            except Exception as exc:
                result = None
                error = exc
        return result, error, conn, cur, event, status, verify

    def test_v10_requeues_same_run_and_preserves_original_evidence_in_event(self):
        result, error, conn, cur, event, status, verify = self.run_recovery()
        self.assertIsNone(error)
        self.assertEqual(result["status"], "review_queued")
        conn.commit.assert_called_once()
        verify.assert_called_once()
        status.assert_called_once()
        self.assertEqual(event.call_args.kwargs['event_type'], "review.infrastructure_retry_requested")
        payload = event.call_args.kwargs['payload']
        self.assertEqual(payload['previous_review']['failure_type'], "ValueError")
        self.assertEqual(payload['previous_execution']['attempt_count'], 1)
        self.assertEqual(payload['previous_execution']['worker_id'], "reviewer-backend-01")
        self.assertEqual(payload['previous_execution']['result_message_id'], "original-message")
        self.assertEqual(payload['previous_execution']['result_metadata'], fixtures()[1]['result_metadata'])
        statements = [call.args[0] for call in cur.execute.call_args_list]
        self.assertTrue(any("UPDATE agent.runs" in s and "result_metadata = %s::jsonb" in s for s in statements))
        self.assertFalse(any("INSERT INTO agent.runs" in s or "INSERT INTO agent.approvals" in s for s in statements))
        self.assertFalse(any("attempt_count = 0" in s for s in statements))

    def test_rejects_stale_run_and_bad_digest(self):
        for args in ({"run_id": "different"}, {"digest": "b" * 64}):
            with self.subTest(args=args):
                result, error, conn, _, event, _, _ = self.run_recovery(**args)
                self.assertIsNone(result)
                self.assertIsInstance(error, svc.AgentStateConflictError)
                conn.rollback.assert_called_once()
                event.assert_not_called()

    def test_rejects_approvals_and_exhausted_execution_budget(self):
        for mutation in ({"status": "review_ready"}, {"attempt_count": 3},
                         {"result_metadata": {"independent_review": {"verdict": "blocked"}}}):
            with self.subTest(mutation=mutation):
                card, review, impl = fixtures()
                target = card if "status" in mutation else review
                target.update(mutation)
                _, error, conn, _, event, _, _ = self.run_recovery(
                    card=card, review=review, implementation=impl)
                self.assertIsInstance(error, svc.AgentStateConflictError)
                conn.rollback.assert_called_once()
                event.assert_not_called()

    def test_mutated_candidate_refuses_requeue(self):
        _, error, conn, _, event, _, _ = self.run_recovery(tamper=True)
        self.assertIsInstance(error, svc.AgentStateConflictError)
        conn.rollback.assert_called_once()
        event.assert_not_called()


if __name__ == '__main__':
    unittest.main()

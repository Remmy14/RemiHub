import unittest
from unittest.mock import MagicMock, patch

from backend.services.agent_service import (
    AgentStateConflictError,
    _decorate_card,
    _deployment_recovery_from_run,
    retry_deployment_github_sync,
    retry_card,
)


CARD_ID = "3d8549c4-a965-4d2e-aacf-9df7e6ccdbb4"
USER_ID = "c346f3f4-3867-4ddb-83ea-7d24db8817bc"


def card(*, status: str, scope: str = "backend") -> dict:
    return {
        "id": CARD_ID,
        "status": status,
        "repository_scope": scope,
        "revision": 2,
    }


class AgentFrontendContractTests(unittest.TestCase):
    def test_allowed_actions_are_derived_by_the_backend(self):
        self.assertEqual(
            _decorate_card(
                card(status="awaiting_implementation_approval"),
                latest_run=None,
            )["allowed_actions"],
            ["add_follow_up", "approve_implementation", "cancel"],
        )
        self.assertEqual(
            _decorate_card(
                card(status="review_ready", scope="android"),
                latest_run=None,
            )["allowed_actions"],
            ["add_follow_up", "approve_deployment", "cancel"],
        )
        self.assertEqual(
            _decorate_card(card(status="failed"), latest_run=None)[
                "allowed_actions"
            ],
            ["retry", "cancel", "close"],
        )
        self.assertEqual(
            _decorate_card(card(status="completed"), latest_run=None)[
                "allowed_actions"
            ],
            ["close"],
        )

    def test_combined_scope_does_not_advertise_uninstalled_actions(self):
        result = _decorate_card(
            card(
                status="awaiting_implementation_approval",
                scope="backend_and_android",
            ),
            latest_run=None,
        )
        self.assertEqual(result["allowed_actions"], ["add_follow_up", "cancel"])

    def test_latest_run_is_normalized_for_list_and_detail_views(self):
        latest = {
            "id": "58debd09-e31c-4b5f-9f19-d9dff4bf3f71",
            "card_id": CARD_ID,
            "phase": "implementation",
            "status": "failed",
            "card_revision": 2,
            "attempt_count": 3,
            "blocked_reason": None,
            "error_message": "Build failed",
            "created_at": "2026-07-29T15:00:00+00:00",
            "updated_at": "2026-07-29T15:05:00+00:00",
            "result_metadata": {"private": "not copied into summary"},
        }
        result = _decorate_card(card(status="failed"), latest_run=latest)
        self.assertEqual(result["latest_run"]["phase"], "implementation")
        self.assertEqual(result["latest_run"]["attempt_count"], 3)
        self.assertNotIn("result_metadata", result["latest_run"])
        self.assertNotIn("card_id", result["latest_run"])

    def test_github_pending_recovery_is_structured_without_message_parsing(self):
        deployment_run_id = "58debd09-e31c-4b5f-9f19-d9dff4bf3f71"
        latest = {
            "id": deployment_run_id,
            "phase": "deployment",
            "status": "blocked",
            "card_revision": 2,
            "attempt_count": 1,
            "blocked_reason": "human-readable text may change",
            "error_message": None,
            "created_at": "2026-07-29T15:00:00+00:00",
            "updated_at": "2026-07-29T15:05:00+00:00",
            "result_metadata": {
                "candidate": {"candidate_commit": "a" * 40},
                "github_sync": {
                    "status": "pending",
                    "candidate_commit": "a" * 40,
                    "blocker_code": "github_sync_canonical_dirty",
                    "failure_reason": "dirty generated file",
                    "retryable": True,
                },
            },
        }

        result = _decorate_card(card(status="blocked"), latest_run=latest)

        self.assertEqual(
            result["deployment_recovery"]["github_sync_status"],
            "github_sync_failed_retryable",
        )
        self.assertEqual(
            result["deployment_recovery"]["blocker_code"],
            "github_sync_canonical_dirty",
        )
        self.assertEqual(
            result["deployment_recovery"]["deployment_run_id"],
            deployment_run_id,
        )
        self.assertTrue(result["deployment_recovery"]["production_deployed"])
        self.assertIn("retry_github_sync", result["allowed_actions"])

    def test_non_github_blocked_state_keeps_current_behavior(self):
        latest = {
            "id": "58debd09-e31c-4b5f-9f19-d9dff4bf3f71",
            "phase": "implementation",
            "status": "blocked",
            "card_revision": 2,
            "attempt_count": 1,
            "blocked_reason": "SDK usage limit",
            "error_message": None,
            "created_at": "2026-07-29T15:00:00+00:00",
            "updated_at": "2026-07-29T15:05:00+00:00",
            "result_metadata": {},
        }

        result = _decorate_card(card(status="blocked"), latest_run=latest)

        self.assertIsNone(result["deployment_recovery"])
        self.assertEqual(result["allowed_actions"], ["cancel"])

    def test_deployment_recovery_distinguishes_required_states(self):
        base_run = {
            "id": "58debd09-e31c-4b5f-9f19-d9dff4bf3f71",
            "phase": "deployment",
            "card_revision": 2,
            "attempt_count": 1,
            "blocked_reason": None,
            "error_message": None,
            "created_at": "2026-07-29T15:00:00+00:00",
            "updated_at": "2026-07-29T15:05:00+00:00",
        }

        self.assertEqual(
            _deployment_recovery_from_run(
                {**base_run, "status": "running", "result_metadata": {}}
            )["github_sync_status"],
            "local_deployment_incomplete",
        )
        running_retry = _deployment_recovery_from_run(
            {
                **base_run,
                "status": "running",
                "result_metadata": {
                    "deployment_recovery": {
                        "github_sync_status": "github_sync_failed_retryable",
                        "retryable": True,
                        "blocker_code": "github_sync_canonical_dirty",
                        "last_error": "dirty generated file",
                        "candidate_commit": "a" * 40,
                        "deployment_run_id": base_run["id"],
                        "production_deployed": True,
                    }
                },
            }
        )
        self.assertEqual(
            running_retry["github_sync_status"],
            "github_sync_running",
        )
        self.assertTrue(running_retry["production_deployed"])
        self.assertEqual(running_retry["candidate_commit"], "a" * 40)
        self.assertEqual(
            _deployment_recovery_from_run(
                {
                    **base_run,
                    "status": "blocked",
                    "result_metadata": {
                        "github_sync": {
                            "status": "pending",
                            "retryable": True,
                        }
                    },
                }
            )["github_sync_status"],
            "github_sync_pending",
        )
        self.assertEqual(
            _deployment_recovery_from_run(
                {
                    **base_run,
                    "status": "blocked",
                    "result_metadata": {
                        "github_sync": {
                            "status": "pending",
                            "retryable": False,
                            "failure_reason": "remote is divergent",
                        }
                    },
                }
            )["github_sync_status"],
            "github_sync_failed_non_retryable",
        )
        self.assertEqual(
            _deployment_recovery_from_run(
                {
                    **base_run,
                    "status": "succeeded",
                    "result_metadata": {
                        "github_sync": {"status": "verified"}
                    },
                }
            )["github_sync_status"],
            "github_sync_succeeded",
        )

    @patch("backend.services.agent_service._card_detail")
    @patch("backend.services.agent_service._insert_event")
    @patch("backend.services.agent_service._update_card_status")
    @patch("backend.services.agent_service._insert_run")
    @patch("backend.services.agent_service._row_to_dict")
    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_failed_planning_run_can_be_retried(
        self,
        get_db_conn,
        put_db_conn,
        locked_card,
        row_to_dict,
        insert_run,
        update_card_status,
        insert_event,
        card_detail,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        get_db_conn.return_value = connection
        locked_card.return_value = card(status="failed")
        row_to_dict.return_value = {
            "id": "failed-run",
            "phase": "planning",
            "card_revision": 2,
            "input_message_id": "message-id",
        }
        insert_run.return_value = "retry-run"
        card_detail.return_value = {"status": "planning_queued"}

        result = retry_card(
            card_id=CARD_ID,
            requested_by=USER_ID,
            notes="Try again",
        )

        self.assertEqual(result["status"], "planning_queued")
        insert_run.assert_called_once()
        self.assertEqual(insert_run.call_args.kwargs["phase"].value, "planning")
        self.assertEqual(
            insert_run.call_args.kwargs["input_message_id"], "message-id"
        )
        self.assertEqual(
            update_card_status.call_args.kwargs["status"].value,
            "planning_queued",
        )
        self.assertEqual(
            insert_event.call_args.kwargs["event_type"],
            "card.retry_requested",
        )
        cursor.execute.assert_called()
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_retry_rejects_nonfailed_cards(
        self,
        get_db_conn,
        put_db_conn,
        locked_card,
    ):
        connection = MagicMock()
        get_db_conn.return_value = connection
        locked_card.return_value = card(status="review_ready")

        with self.assertRaisesRegex(
            AgentStateConflictError,
            "only when the card status is failed",
        ):
            retry_card(card_id=CARD_ID, requested_by=USER_ID)

        connection.rollback.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_service._row_to_dict")
    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_github_sync_retry_refuses_mismatched_run(
        self,
        get_db_conn,
        put_db_conn,
        locked_card,
        row_to_dict,
    ):
        connection = MagicMock()
        get_db_conn.return_value = connection
        locked_card.return_value = card(status="blocked")
        row_to_dict.return_value = None

        with self.assertRaisesRegex(AgentStateConflictError, "exact deployment run"):
            retry_deployment_github_sync(
                card_id=CARD_ID,
                deployment_run_id="58debd09-e31c-4b5f-9f19-d9dff4bf3f71",
                requested_by=USER_ID,
            )

        connection.rollback.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_service._request_deployment_worker")
    @patch("backend.services.agent_service._card_detail")
    @patch("backend.services.agent_service._insert_event")
    @patch("backend.services.agent_service._row_to_dict")
    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_github_sync_retry_is_noop_for_already_succeeded_run(
        self,
        get_db_conn,
        put_db_conn,
        locked_card,
        row_to_dict,
        insert_event,
        card_detail,
        request_deployment_worker,
    ):
        deployment_run_id = "58debd09-e31c-4b5f-9f19-d9dff4bf3f71"
        connection = MagicMock()
        get_db_conn.return_value = connection
        locked_card.return_value = card(status="completed")
        row_to_dict.return_value = {
            "id": deployment_run_id,
            "phase": "deployment",
            "status": "succeeded",
            "card_revision": 2,
            "attempt_count": 1,
            "blocked_reason": None,
            "error_message": None,
            "created_at": "2026-07-29T15:00:00+00:00",
            "updated_at": "2026-07-29T15:05:00+00:00",
            "result_metadata": {
                "deployment_recovery": {
                    "github_sync_status": "github_sync_succeeded",
                    "retryable": False,
                    "blocker_code": None,
                    "last_error": None,
                    "candidate_commit": "a" * 40,
                    "deployment_run_id": deployment_run_id,
                    "production_deployed": True,
                }
            },
        }
        card_detail.return_value = {"status": "completed"}

        result = retry_deployment_github_sync(
            card_id=CARD_ID,
            deployment_run_id=deployment_run_id,
            requested_by=USER_ID,
        )

        self.assertEqual(result["status"], "completed")
        request_deployment_worker.assert_not_called()
        insert_event.assert_called_once()
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_service._request_deployment_worker")
    @patch("backend.services.agent_service._card_detail")
    @patch("backend.services.agent_service._insert_event")
    @patch("backend.services.agent_service._row_to_dict")
    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_github_sync_retry_requeues_exact_blocked_run(
        self,
        get_db_conn,
        put_db_conn,
        locked_card,
        row_to_dict,
        insert_event,
        card_detail,
        request_deployment_worker,
    ):
        deployment_run_id = "58debd09-e31c-4b5f-9f19-d9dff4bf3f71"
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1
        get_db_conn.return_value = connection
        locked_card.return_value = card(status="blocked")
        row_to_dict.return_value = {
            "id": deployment_run_id,
            "phase": "deployment",
            "status": "blocked",
            "card_revision": 2,
            "attempt_count": 1,
            "blocked_reason": "GitHub pending",
            "error_message": None,
            "created_at": "2026-07-29T15:00:00+00:00",
            "updated_at": "2026-07-29T15:05:00+00:00",
            "result_metadata": {
                "deployment_recovery": {
                    "github_sync_status": "github_sync_failed_retryable",
                    "retryable": True,
                    "blocker_code": "github_sync_canonical_dirty",
                    "last_error": "dirty generated file",
                    "candidate_commit": "a" * 40,
                    "deployment_run_id": deployment_run_id,
                    "production_deployed": True,
                }
            },
        }
        card_detail.return_value = {
            "status": "blocked",
            "deployment_recovery": {
                "github_sync_status": "github_sync_running",
                "retryable": False,
                "candidate_commit": "a" * 40,
                "deployment_run_id": deployment_run_id,
                "production_deployed": True,
            },
        }

        result = retry_deployment_github_sync(
            card_id=CARD_ID,
            deployment_run_id=deployment_run_id,
            requested_by=USER_ID,
            notes="Retry now",
        )

        self.assertEqual(result["status"], "blocked")
        request_deployment_worker.assert_called_once()
        self.assertEqual(
            request_deployment_worker.call_args.args[0].value,
            "backend",
        )
        self.assertEqual(
            insert_event.call_args.kwargs["event_type"],
            "card.github_sync_retry_requested",
        )
        executed_sql = "\n".join(
            call.args[0]
            for call in cursor.execute.call_args_list
            if call.args and isinstance(call.args[0], str)
        )
        self.assertIn("available_at = CURRENT_TIMESTAMP", executed_sql)
        self.assertIn("result_metadata = %s::jsonb", executed_sql)
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_service._row_to_dict")
    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_github_sync_retry_refuses_stale_card_revision(
        self,
        get_db_conn,
        put_db_conn,
        locked_card,
        row_to_dict,
    ):
        deployment_run_id = "58debd09-e31c-4b5f-9f19-d9dff4bf3f71"
        connection = MagicMock()
        get_db_conn.return_value = connection
        locked_card.return_value = card(status="blocked")
        row_to_dict.return_value = {
            "id": deployment_run_id,
            "phase": "deployment",
            "status": "blocked",
            "card_revision": 1,
            "attempt_count": 1,
            "blocked_reason": "GitHub pending",
            "error_message": None,
            "created_at": "2026-07-29T15:00:00+00:00",
            "updated_at": "2026-07-29T15:05:00+00:00",
            "result_metadata": {
                "deployment_recovery": {
                    "github_sync_status": "github_sync_failed_retryable",
                    "retryable": True,
                    "candidate_commit": "a" * 40,
                    "deployment_run_id": deployment_run_id,
                    "production_deployed": True,
                }
            },
        }

        with self.assertRaisesRegex(
            AgentStateConflictError,
            "current card revision",
        ):
            retry_deployment_github_sync(
                card_id=CARD_ID,
                deployment_run_id=deployment_run_id,
                requested_by=USER_ID,
            )

        connection.rollback.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)


if __name__ == "__main__":
    unittest.main()

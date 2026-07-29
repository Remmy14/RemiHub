import unittest
from unittest.mock import MagicMock, patch

from backend.services.agent_service import (
    AgentStateConflictError,
    _decorate_card,
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


if __name__ == "__main__":
    unittest.main()

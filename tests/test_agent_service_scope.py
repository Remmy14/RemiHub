import unittest
from unittest.mock import MagicMock, patch

from backend.services.agent_service import (
    AgentStateConflictError,
    CARD_COLUMNS,
    approve_deployment,
    approve_implementation,
)


def card(repository_scope: str) -> dict:
    return {
        "id": "3d8549c4-a965-4d2e-aacf-9df7e6ccdbb4",
        "status": "awaiting_implementation_approval",
        "repository_scope": repository_scope,
        "revision": 1,
    }


class AgentServiceRepositoryScopeTests(unittest.TestCase):
    def test_card_serialization_selects_repository_scope(self):
        self.assertIn("repository_scope", CARD_COLUMNS)

    @patch("backend.services.agent_service._card_detail")
    @patch("backend.services.agent_service._insert_event")
    @patch("backend.services.agent_service._insert_run")
    @patch("backend.services.agent_service._insert_approval")
    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_android_implementation_approval_is_allowed(
        self,
        get_db_conn,
        put_db_conn,
        locked_card,
        insert_approval,
        insert_run,
        insert_event,
        card_detail,
    ):
        connection = MagicMock()
        get_db_conn.return_value = connection
        locked_card.return_value = card("android")
        insert_approval.return_value = "approval-id"
        insert_run.return_value = "run-id"
        card_detail.return_value = {"repository_scope": "android"}

        result = approve_implementation(
            card_id=card("android")["id"],
            approved_by="user-id",
        )

        self.assertEqual(result["repository_scope"], "android")
        insert_approval.assert_called_once()
        insert_run.assert_called_once()
        insert_event.assert_called_once()
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_service._card_detail")
    @patch("backend.services.agent_service._insert_event")
    @patch("backend.services.agent_service._insert_run")
    @patch("backend.services.agent_service._insert_approval")
    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_backend_implementation_approval_remains_allowed(
        self,
        get_db_conn,
        put_db_conn,
        locked_card,
        insert_approval,
        insert_run,
        insert_event,
        card_detail,
    ):
        connection = MagicMock()
        get_db_conn.return_value = connection
        locked_card.return_value = card("backend")
        insert_approval.return_value = "approval-id"
        insert_run.return_value = "run-id"
        card_detail.return_value = {"repository_scope": "backend"}

        result = approve_implementation(
            card_id=card("backend")["id"],
            approved_by="user-id",
        )

        self.assertEqual(result["repository_scope"], "backend")
        insert_approval.assert_called_once()
        insert_run.assert_called_once()
        insert_event.assert_called_once()
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_service._insert_approval")
    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_unsupported_implementation_scopes_are_rejected(
        self,
        get_db_conn,
        put_db_conn,
        locked_card,
        insert_approval,
    ):
        connection = MagicMock()
        get_db_conn.return_value = connection

        for scope in ("auto", "backend_and_android"):
            with self.subTest(scope=scope):
                locked_card.return_value = card(scope)
                with self.assertRaises(AgentStateConflictError):
                    approve_implementation(
                        card_id=card(scope)["id"],
                        approved_by="user-id",
                    )

        insert_approval.assert_not_called()
        self.assertEqual(connection.rollback.call_count, 2)
        put_db_conn.assert_called()

    @patch("backend.services.agent_service._deployment_implementation_result")
    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_non_backend_deployment_scopes_are_rejected(
        self,
        get_db_conn,
        put_db_conn,
        locked_card,
        deployment_result,
    ):
        connection = MagicMock()
        get_db_conn.return_value = connection
        for scope in ("android", "backend_and_android"):
            with self.subTest(scope=scope):
                deployment_card = card(scope)
                deployment_card["status"] = "review_ready"
                locked_card.return_value = deployment_card

                with self.assertRaisesRegex(
                    AgentStateConflictError,
                    "backend-scoped",
                ):
                    approve_deployment(
                        card_id=deployment_card["id"],
                        approved_by="user-id",
                    )

        deployment_result.assert_not_called()
        self.assertEqual(connection.rollback.call_count, 2)
        self.assertEqual(put_db_conn.call_count, 2)


if __name__ == "__main__":
    unittest.main()

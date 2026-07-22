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

    @patch("backend.services.agent_service._card_detail")
    @patch("backend.services.agent_service._insert_event")
    @patch("backend.services.agent_service._insert_run")
    @patch("backend.services.agent_service._insert_approval")
    @patch("backend.services.agent_service._deployment_implementation_result")
    @patch("backend.services.agent_service._require_android_deployment_ready")
    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_android_deployment_approval_is_allowed_when_signing_is_ready(
        self,
        get_db_conn,
        put_db_conn,
        locked_card,
        require_ready,
        deployment_result,
        insert_approval,
        insert_run,
        insert_event,
        card_detail,
    ):
        connection = MagicMock()
        get_db_conn.return_value = connection
        deployment_card = card("android")
        deployment_card["status"] = "review_ready"
        locked_card.return_value = deployment_card
        deployment_result.return_value = {"id": "implementation-run"}
        insert_approval.return_value = "approval-id"
        insert_run.return_value = "deployment-run"
        card_detail.return_value = {"repository_scope": "android"}

        result = approve_deployment(
            card_id=deployment_card["id"],
            approved_by="user-id",
        )

        self.assertEqual(result["repository_scope"], "android")
        require_ready.assert_called_once_with()
        deployment_result.assert_called_once()
        self.assertEqual(
            deployment_result.call_args.kwargs["repository_scope"].value,
            "android",
        )
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_service._deployment_implementation_result")
    @patch("backend.services.agent_service._require_android_deployment_ready")
    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_android_deployment_remains_fail_closed_without_signing(
        self,
        get_db_conn,
        put_db_conn,
        locked_card,
        require_ready,
        deployment_result,
    ):
        connection = MagicMock()
        get_db_conn.return_value = connection
        deployment_card = card("android")
        deployment_card["status"] = "review_ready"
        locked_card.return_value = deployment_card
        require_ready.side_effect = AgentStateConflictError(
            "Android deployment signing is not provisioned"
        )

        with self.assertRaisesRegex(AgentStateConflictError, "not provisioned"):
            approve_deployment(
                card_id=deployment_card["id"],
                approved_by="user-id",
            )

        deployment_result.assert_not_called()
        connection.rollback.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_service._deployment_implementation_result")
    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_combined_deployment_scope_is_rejected(
        self,
        get_db_conn,
        put_db_conn,
        locked_card,
        deployment_result,
    ):
        connection = MagicMock()
        get_db_conn.return_value = connection
        deployment_card = card("backend_and_android")
        deployment_card["status"] = "review_ready"
        locked_card.return_value = deployment_card

        with self.assertRaisesRegex(
            AgentStateConflictError,
            "combined backend-and-Android",
        ):
            approve_deployment(
                card_id=deployment_card["id"],
                approved_by="user-id",
            )

        deployment_result.assert_not_called()
        connection.rollback.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import MagicMock, patch

from backend.core.agent_state import RepositoryScope
from backend.services.agent_service import approve_deployment, retry_card


class AgentServiceDeploymentTriggerTests(unittest.TestCase):
    @patch("backend.services.agent_service._request_deployment_worker")
    @patch("backend.services.agent_service._card_detail")
    @patch("backend.services.agent_service._insert_event")
    @patch("backend.services.agent_service._insert_run")
    @patch("backend.services.agent_service._insert_approval")
    @patch("backend.services.agent_service._deployment_implementation_result")
    @patch("backend.services.agent_service._require_android_deployment_ready")
    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_android_approval_commits_before_triggering_run_once_worker(
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
        request_worker,
    ):
        connection = MagicMock()
        committed = {"value": False}
        connection.commit.side_effect = lambda: committed.__setitem__("value", True)
        get_db_conn.return_value = connection
        locked_card.return_value = {
            "id": "3d8549c4-a965-4d2e-aacf-9df7e6ccdbb4",
            "status": "review_ready",
            "repository_scope": "android",
            "revision": 1,
        }
        deployment_result.return_value = {"id": "implementation-run"}
        insert_approval.return_value = "approval-id"
        insert_run.return_value = "deployment-run"
        card_detail.return_value = {"repository_scope": "android"}

        def assert_committed(scope):
            self.assertTrue(committed["value"])
            self.assertEqual(scope, RepositoryScope.ANDROID)

        request_worker.side_effect = assert_committed

        approve_deployment(
            card_id=locked_card.return_value["id"],
            approved_by="user-id",
        )

        request_worker.assert_called_once_with(RepositoryScope.ANDROID)
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_service._request_deployment_worker")
    @patch("backend.services.agent_service._card_detail")
    @patch("backend.services.agent_service._insert_event")
    @patch("backend.services.agent_service._insert_run")
    @patch("backend.services.agent_service._deployment_retry_binding")
    @patch("backend.services.agent_service._require_android_deployment_ready")
    @patch("backend.services.agent_service._row_to_dict")
    @patch("backend.services.agent_service._locked_card")
    @patch("backend.services.agent_service.put_db_conn")
    @patch("backend.services.agent_service.get_db_conn")
    def test_android_deployment_retry_commits_before_triggering_worker(
        self,
        get_db_conn,
        put_db_conn,
        locked_card,
        row_to_dict,
        require_ready,
        deployment_retry_binding,
        insert_run,
        insert_event,
        card_detail,
        request_worker,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("failed-deployment-run",)
        committed = {"value": False}
        connection.commit.side_effect = lambda: committed.__setitem__("value", True)
        get_db_conn.return_value = connection
        locked_card.return_value = {
            "id": "3d8549c4-a965-4d2e-aacf-9df7e6ccdbb4",
            "status": "failed",
            "repository_scope": "android",
            "revision": 1,
        }
        row_to_dict.return_value = {
            "id": "failed-deployment-run",
            "phase": "deployment",
            "card_revision": 1,
            "input_message_id": None,
        }
        deployment_retry_binding.return_value = (
            "approval-id",
            {"id": "implementation-run"},
        )
        insert_run.return_value = "retry-run"
        card_detail.return_value = {"repository_scope": "android"}

        def assert_committed(scope):
            self.assertTrue(committed["value"])
            self.assertEqual(scope, RepositoryScope.ANDROID)

        request_worker.side_effect = assert_committed

        retry_card(
            card_id=locked_card.return_value["id"],
            requested_by="user-id",
        )

        request_worker.assert_called_once_with(RepositoryScope.ANDROID)
        deployment_retry_binding.assert_called_once_with(
            cursor,
            card_id=locked_card.return_value["id"],
            card_revision=1,
            failed_deployment_run_id="failed-deployment-run",
            repository_scope=RepositoryScope.ANDROID,
        )
        binding_events = [
            call.kwargs
            for call in insert_event.call_args_list
            if call.kwargs["event_type"] == "card.deployment_retry_bound"
        ]
        self.assertEqual(len(binding_events), 1)
        self.assertEqual(binding_events[0]["payload"]["approval_id"], "approval-id")
        self.assertEqual(
            binding_events[0]["payload"]["implementation_run_id"],
            "implementation-run",
        )
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)


if __name__ == "__main__":
    unittest.main()

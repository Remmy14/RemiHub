import unittest
from unittest.mock import MagicMock, patch

from backend.core.agent_state import CardStatus, RunPhase
from backend.core.agent_worker import AgentLeaseLostError
from backend.services.agent_worker_service import (
    AgentQueueStateError,
    _validate_candidate,
    heartbeat_run,
    verify_worker_identity,
)
from tests.test_agent_worker import claimed_run


def candidate(**overrides) -> dict:
    row = {
        "id": "4c0056d9-cfab-4a7e-b8a8-369ea90efee8",
        "card_id": "3d8549c4-a965-4d2e-aacf-9df7e6ccdbb4",
        "phase": "planning",
        "run_status": "queued",
        "card_status": "planning_queued",
        "resume_status": None,
    }
    row.update(overrides)
    return row


class AgentClaimCandidateTests(unittest.TestCase):
    def test_queued_candidate_maps_to_active_phase(self):
        phase, previous, active = _validate_candidate(candidate())

        self.assertEqual(phase, RunPhase.PLANNING)
        self.assertEqual(previous, CardStatus.PLANNING_QUEUED)
        self.assertEqual(active, CardStatus.PLANNING)

    def test_expired_running_candidate_can_be_reclaimed(self):
        phase, previous, active = _validate_candidate(
            candidate(
                run_status="running",
                card_status="planning",
            )
        )

        self.assertEqual(phase, RunPhase.PLANNING)
        self.assertEqual(previous, active)

    def test_blocked_candidate_requires_matching_resume_status(self):
        with self.assertRaisesRegex(
            AgentQueueStateError,
            "invalid resume status",
        ):
            _validate_candidate(
                candidate(
                    run_status="blocked",
                    card_status="blocked",
                    resume_status="implementation_queued",
                )
            )

    def test_inconsistent_card_and_run_fail_closed(self):
        with self.assertRaisesRegex(AgentQueueStateError, "expected planning"):
            _validate_candidate(candidate(card_status="implementing"))


class AgentHeartbeatTests(unittest.TestCase):
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_heartbeat_extends_owned_lease(self, get_db_conn, put_db_conn):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1
        get_db_conn.return_value = connection
        claim = claimed_run()

        heartbeat_run(claim, lease_seconds=120)

        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()
        put_db_conn.assert_called_once_with(connection)
        parameters = cursor.execute.call_args.args[1]
        self.assertEqual(
            parameters,
            (
                120,
                claim.id,
                claim.card_id,
                claim.worker_id,
                claim.lease_token,
            ),
        )

    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_heartbeat_rejects_stale_lease(self, get_db_conn, put_db_conn):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 0
        get_db_conn.return_value = connection

        with self.assertRaises(AgentLeaseLostError):
            heartbeat_run(claimed_run(), lease_seconds=120)

        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()
        put_db_conn.assert_called_once_with(connection)


class AgentWorkerIdentityTests(unittest.TestCase):
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_qa_worker_requires_exact_database_and_role(
        self,
        get_db_conn,
        put_db_conn,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (
            "remihub_qa",
            "remihub_qa_agent_worker",
            "remihub_qa_agent_worker",
        )
        get_db_conn.return_value = connection

        identity = verify_worker_identity("qa")

        self.assertEqual(identity[0], "remihub_qa")
        connection.rollback.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_qa_worker_rejects_production_database(
        self,
        get_db_conn,
        put_db_conn,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (
            "remihub",
            "remihub_agent_worker",
            "remihub_agent_worker",
        )
        get_db_conn.return_value = connection

        with self.assertRaisesRegex(
            AgentQueueStateError,
            "identity mismatch",
        ):
            verify_worker_identity("qa")

        connection.rollback.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)


if __name__ == "__main__":
    unittest.main()

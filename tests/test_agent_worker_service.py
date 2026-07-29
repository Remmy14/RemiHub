import unittest
from unittest.mock import MagicMock, patch

from backend.core.agent_state import CardStatus, RepositoryScope, RunPhase
from backend.core.agent_worker import AgentLeaseLostError, ExecutionResult
from backend.services.agent_worker_service import (
    AgentQueueStateError,
    _claimed_run_from_row,
    _validate_candidate,
    claim_next_run,
    complete_run,
    heartbeat_run,
    persist_codex_thread_id,
    persist_implementation_workspace,
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
        "repository_scope": "auto",
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


    def test_deployment_candidate_requires_approval_and_implementation_evidence(self):
        with self.assertRaisesRegex(AgentQueueStateError, "incomplete"):
            _validate_candidate(
                candidate(
                    phase="deployment",
                    card_status="deployment_queued",
                    deployment_approval_id="approval",
                )
            )

    def test_deployment_candidate_accepts_bound_implementation_evidence(self):
        phase, previous, active = _validate_candidate(
            candidate(
                phase="deployment",
                card_status="deployment_queued",
                deployment_approval_id="approval",
                implementation_run_id="implementation-run",
                implementation_result_metadata={"phase": "implementation"},
            )
        )

        self.assertEqual(phase, RunPhase.DEPLOYMENT)
        self.assertEqual(previous, CardStatus.DEPLOYMENT_QUEUED)
        self.assertEqual(active, CardStatus.DEPLOYING)

    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_claim_query_is_filtered_to_executor_phases(
        self,
        get_db_conn,
        put_db_conn,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        get_db_conn.return_value = connection

        result = claim_next_run(
            worker_id="qa-worker",
            lease_seconds=120,
            allowed_phases=frozenset({RunPhase.PLANNING}),
        )

        self.assertIsNone(result)
        sql, parameters = cursor.execute.call_args.args
        self.assertIn("runs.phase = ANY(%s)", sql)
        self.assertEqual(parameters, (["planning"],))
        connection.rollback.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)


    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_claim_query_can_filter_repository_scope(
        self,
        get_db_conn,
        put_db_conn,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        get_db_conn.return_value = connection

        result = claim_next_run(
            worker_id="android-worker",
            lease_seconds=120,
            allowed_phases=frozenset({RunPhase.IMPLEMENTATION}),
            allowed_repository_scopes=frozenset({RepositoryScope.ANDROID}),
        )

        self.assertIsNone(result)
        sql, parameters = cursor.execute.call_args.args
        self.assertIn("cards.repository_scope = ANY(%s)", sql)
        self.assertEqual(parameters, (["implementation"], ["android"]))
        connection.rollback.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    def test_claim_rejects_empty_phase_capability(self):
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            claim_next_run(
                worker_id="qa-worker",
                lease_seconds=120,
                allowed_phases=frozenset(),
            )

    def test_claimed_run_includes_repository_scope(self):
        claim = _claimed_run_from_row(
            {
                **candidate(active_card_status="planning"),
                "card_revision": 1,
                "attempt_count": 1,
                "lease_token": "lease",
                "worker_id": "qa-worker",
                "title": "Android refresh",
                "description": "Plan Android UI.",
                "base_branch": "main",
                "feature_branch": None,
                "worktree_path": None,
                "codex_thread_id": None,
                "repository_scope": "android",
            },
            [],
        )

        self.assertEqual(claim.repository_scope, RepositoryScope.ANDROID)


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


class CodexThreadPersistenceTests(unittest.TestCase):
    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_thread_id_is_saved_only_under_the_owned_lease(
        self,
        get_db_conn,
        put_db_conn,
        lock_owned_run,
        insert_event,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1
        get_db_conn.return_value = connection
        claim = claimed_run()

        persist_codex_thread_id(claim, thread_id="thr_remihub_123")

        lock_owned_run.assert_called_once()
        sql, parameters = cursor.execute.call_args.args
        self.assertIn("SET codex_thread_id = %s", sql)
        self.assertEqual(
            parameters,
            ("thr_remihub_123", claim.card_id, "thr_remihub_123"),
        )
        insert_event.assert_called_once()
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_conflicting_thread_id_fails_closed(
        self,
        get_db_conn,
        put_db_conn,
        _lock_owned_run,
        insert_event,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 0
        get_db_conn.return_value = connection

        with self.assertRaisesRegex(AgentQueueStateError, "different Codex thread"):
            persist_codex_thread_id(
                claimed_run(),
                thread_id="thr_conflict",
            )

        insert_event.assert_not_called()
        connection.rollback.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)


class ImplementationWorkspacePersistenceTests(unittest.TestCase):
    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_workspace_is_saved_only_under_the_owned_lease(
        self,
        get_db_conn,
        put_db_conn,
        lock_owned_run,
        insert_event,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1
        get_db_conn.return_value = connection
        claim = claimed_run(phase=RunPhase.IMPLEMENTATION)

        persist_implementation_workspace(
            claim,
            feature_branch=f"agent/card-{claim.card_id}",
            worktree_path=f"/opt/remihub-agent/worktrees/card-{claim.card_id}",
        )

        lock_owned_run.assert_called_once()
        sql, parameters = cursor.execute.call_args.args
        self.assertIn("SET feature_branch = %s", sql)
        self.assertEqual(parameters[2], claim.card_id)
        insert_event.assert_called_once()
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_conflicting_workspace_fails_closed(
        self,
        get_db_conn,
        put_db_conn,
        _lock_owned_run,
        insert_event,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 0
        get_db_conn.return_value = connection
        claim = claimed_run(phase=RunPhase.IMPLEMENTATION)

        with self.assertRaisesRegex(AgentQueueStateError, "different"):
            persist_implementation_workspace(
                claim,
                feature_branch=f"agent/card-{claim.card_id}",
                worktree_path=(
                    f"/opt/remihub-agent/worktrees/card-{claim.card_id}"
                ),
            )

        insert_event.assert_not_called()
        connection.rollback.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    def test_planning_run_cannot_attach_implementation_workspace(self):
        with self.assertRaisesRegex(AgentQueueStateError, "implementation run"):
            persist_implementation_workspace(
                claimed_run(),
                feature_branch="agent/card-example",
                worktree_path="/tmp/card-example",
            )


class RunCompletionTests(unittest.TestCase):
    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._insert_message")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_planning_success_persists_repository_scope_atomically(
        self,
        get_db_conn,
        put_db_conn,
        lock_owned_run,
        insert_message,
        insert_event,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        get_db_conn.return_value = connection
        lock_owned_run.return_value = {
            "card_status": "planning",
            "phase": "planning",
            "repository_scope": "auto",
        }
        insert_message.return_value = "message-id"
        claim = claimed_run()

        complete_run(
            claim,
            ExecutionResult(
                message="Plan ready",
                card_status=CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
                repository_scope=RepositoryScope.BACKEND_AND_ANDROID,
                metadata={"executor": "codex_planning"},
            ),
        )

        run_update_sql, run_update_parameters = cursor.execute.call_args_list[0].args
        self.assertIn("result_metadata = %s::jsonb", run_update_sql)
        self.assertIn("backend_and_android", run_update_parameters[2])
        card_update_sql, card_update_parameters = cursor.execute.call_args_list[1].args
        self.assertIn("repository_scope = %s", card_update_sql)
        self.assertEqual(
            card_update_parameters,
            (
                "awaiting_implementation_approval",
                "backend_and_android",
                claim.card_id,
            ),
        )
        insert_event.assert_called_once()
        self.assertEqual(
            insert_event.call_args.kwargs["payload"]["repository_scope"],
            "backend_and_android",
        )
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._insert_message")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_android_planning_success_sets_master_base_branch_atomically(
        self,
        get_db_conn,
        put_db_conn,
        lock_owned_run,
        insert_message,
        insert_event,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        get_db_conn.return_value = connection
        lock_owned_run.return_value = {
            "card_status": "planning",
            "phase": "planning",
            "repository_scope": "auto",
        }
        insert_message.return_value = "message-id"
        claim = claimed_run()

        complete_run(
            claim,
            ExecutionResult(
                message="Android plan ready",
                card_status=CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
                repository_scope=RepositoryScope.ANDROID,
                metadata={"executor": "codex_planning"},
            ),
        )

        card_update_sql, card_update_parameters = cursor.execute.call_args_list[1].args
        self.assertIn("repository_scope = %s", card_update_sql)
        self.assertIn("base_branch = %s", card_update_sql)
        self.assertEqual(
            card_update_parameters,
            (
                "awaiting_implementation_approval",
                "android",
                "master",
                claim.card_id,
            ),
        )
        insert_event.assert_called_once()
        self.assertEqual(
            insert_event.call_args.kwargs["payload"]["repository_scope"],
            "android",
        )
        self.assertEqual(
            insert_event.call_args.kwargs["payload"]["base_branch"],
            "master",
        )
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    def test_planning_success_rejects_unresolved_repository_scope(self):
        with self.assertRaisesRegex(AgentQueueStateError, "resolved"):
            complete_run(
                claimed_run(),
                ExecutionResult(
                    message="Plan ready",
                    card_status=CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
                ),
            )



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

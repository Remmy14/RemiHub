import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from backend.core.agent_state import CardStatus, RepositoryScope, RunPhase
from backend.core.agent_worker import AgentLeaseLostError, ExecutionResult
import backend.services.agent_worker_service as agent_worker_service_module
from backend.services.agent_worker_service import (
    AgentQueueStateError,
    DatabaseAgentQueue,
    _claimed_run_from_row,
    _validate_candidate,
    block_run,
    claim_next_run,
    complete_run,
    fail_run,
    heartbeat_run,
    persist_codex_thread_id,
    persist_implementation_workspace,
    rollover_codex_thread_id,
    verify_worker_identity,
)
from tests.test_agent_worker import claimed_run


class AgentWorkerConnectionBoundaryTests(unittest.TestCase):
    def test_import_and_queue_construction_do_not_open_database_boundary(self):
        script = """
from unittest.mock import patch
from psycopg2 import pool

with (
    patch.object(pool, "ThreadedConnectionPool") as threaded_pool,
    patch("psycopg2.connect") as connect,
    patch("backend.config.resolve_database_config_path") as resolve_config,
    patch("backend.config.load_config") as load_config,
):
    import backend.services.agent_worker_service as service
    queue = service.DatabaseAgentQueue(environment="production")
    assert queue.environment == "production"
    threaded_pool.assert_not_called()
    connect.assert_not_called()
    resolve_config.assert_not_called()
    load_config.assert_not_called()
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    @patch("backend.services.agent_worker_service.psycopg2.connect")
    @patch("backend.services.agent_worker_service.load_config")
    @patch("backend.services.agent_worker_service.resolve_database_config_path")
    def test_direct_worker_connection_is_lazy_and_closed(
        self,
        resolve_config,
        load_config,
        connect,
    ):
        resolve_config.return_value = "/secure/worker.ini"
        load_config.return_value = {
            "Database": {
                "user": "remihub_agent_worker",
                "password": "secret",
                "host": "127.0.0.1",
                "port": "5432",
                "database": "remihub",
            }
        }
        connection = MagicMock()
        connect.return_value = connection

        conn = agent_worker_service_module.get_db_conn()
        agent_worker_service_module.put_db_conn(conn)

        resolve_config.assert_called_once_with(
            agent_worker_service_module.DEFAULT_DATABASE_CONFIG
        )
        load_config.assert_called_once_with("/secure/worker.ini")
        connect.assert_called_once_with(
            user="remihub_agent_worker",
            password="secret",
            host="127.0.0.1",
            port="5432",
            database="remihub",
        )
        connection.close.assert_called_once_with()

    @patch("backend.services.agent_worker_service.claim_next_run")
    def test_database_queue_carries_deployment_environment(self, claim):
        queue = DatabaseAgentQueue(
            environment="production",
            deployment_environment="production",
        )
        claim.return_value = None

        result = queue.claim_next_run(
            worker_id="worker",
            lease_seconds=120,
            allowed_phases=frozenset({RunPhase.DEPLOYMENT}),
            allowed_repository_scopes=frozenset({RepositoryScope.BACKEND}),
        )

        self.assertIsNone(result)
        self.assertEqual(
            claim.call_args.kwargs["deployment_environment"],
            "production",
        )


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


    def test_deployment_candidate_rejects_missing_binding(self):
        with self.assertRaisesRegex(AgentQueueStateError, "context is missing"):
            _validate_candidate(
                candidate(
                    phase="deployment",
                    card_status="deployment_queued",
                    deployment_approval_id=None,
                    implementation_run_id=None,
                    implementation_result_metadata=None,
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
        self.assertIn("card.deployment_retry_bound", sql)
        self.assertIn("events.payload ->> 'run_id' = runs.id::text", sql)
        self.assertNotIn("ORDER BY prior_runs.created_at DESC", sql)
        self.assertEqual(parameters, (["planning"],))
        connection.rollback.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)


    @patch("backend.services.agent_worker_service._rows_to_dicts")
    @patch("backend.services.agent_worker_service._row_to_dict")
    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_claim_blocked_run_preserves_result_metadata_with_matching_parameters(
        self,
        get_db_conn,
        put_db_conn,
        insert_event,
        row_to_dict,
        rows_to_dicts,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = object()
        cursor.fetchall.return_value = []
        get_db_conn.return_value = connection
        recovery_metadata = {
            "deployment_recovery": {
                "github_sync_status": "github_sync_failed_retryable",
                "retryable": True,
            }
        }
        row_to_dict.return_value = candidate(
            run_status="blocked",
            card_status="blocked",
            resume_status="planning_queued",
            result_metadata=recovery_metadata,
            card_revision=1,
            attempt_count=1,
            title="Retry metadata",
            description="Preserve structured state.",
            base_branch="main",
            feature_branch=None,
            worktree_path=None,
            codex_thread_id=None,
            deployment_approval_id=None,
            implementation_run_id=None,
            implementation_result_metadata=None,
        )
        rows_to_dicts.return_value = []

        result = claim_next_run(
            worker_id="production-worker",
            lease_seconds=120,
            allowed_phases=frozenset({RunPhase.PLANNING}),
        )

        self.assertIsNotNone(result)
        run_update_sql, run_update_parameters = cursor.execute.call_args_list[1].args
        self.assertIn("result_metadata = %s::jsonb", run_update_sql)
        self.assertEqual(run_update_sql.count("%s"), len(run_update_parameters))
        self.assertEqual(
            run_update_parameters[-2],
            '{"deployment_recovery": {"github_sync_status": "github_sync_failed_retryable", "retryable": true}}',
        )
        self.assertEqual(run_update_parameters[-1], row_to_dict.return_value["id"])
        insert_event.assert_called_once()
        connection.commit.assert_called_once_with()
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

    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_backend_deployment_qa_claim_excludes_qa_succeeded_runs(
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
            allowed_phases=frozenset({RunPhase.DEPLOYMENT}),
            allowed_repository_scopes=frozenset({RepositoryScope.BACKEND}),
            deployment_environment="qa",
        )

        self.assertIsNone(result)
        sql, parameters = cursor.execute.call_args.args
        self.assertIn("runs.result_metadata", sql)
        self.assertIn("deployment_pipeline,stage", sql)
        self.assertIn("<> 'qa_succeeded'", sql)
        self.assertEqual(parameters, (["deployment"], ["backend"]))
        connection.rollback.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_backend_deployment_production_claim_requires_qa_succeeded(
        self,
        get_db_conn,
        put_db_conn,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        get_db_conn.return_value = connection

        result = claim_next_run(
            worker_id="production-worker",
            lease_seconds=120,
            allowed_phases=frozenset({RunPhase.DEPLOYMENT}),
            allowed_repository_scopes=frozenset({RepositoryScope.BACKEND}),
            deployment_environment="production",
        )

        self.assertIsNone(result)
        sql, parameters = cursor.execute.call_args.args
        self.assertIn("runs.result_metadata", sql)
        self.assertIn("deployment_pipeline,stage", sql)
        self.assertIn("= 'qa_succeeded'", sql)
        self.assertEqual(parameters, (["deployment"], ["backend"]))
        connection.rollback.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_claim_rejects_invalid_deployment_environment_before_database(
        self,
        get_db_conn,
    ):
        with self.assertRaisesRegex(ValueError, "deployment_environment"):
            claim_next_run(
                worker_id="qa-worker",
                lease_seconds=120,
                allowed_phases=frozenset({RunPhase.DEPLOYMENT}),
                deployment_environment="staging",
            )

        get_db_conn.assert_not_called()

    def test_claim_rejects_empty_phase_capability(self):
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            claim_next_run(
                worker_id="qa-worker",
                lease_seconds=120,
                allowed_phases=frozenset(),
            )

    def test_claimed_run_includes_repository_scope(self):
        result_metadata = {"deployment_pipeline": {"stage": "qa_succeeded"}}
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
                "result_metadata": result_metadata,
            },
            [],
        )

        self.assertEqual(claim.repository_scope, RepositoryScope.ANDROID)
        self.assertEqual(claim.result_metadata, result_metadata)


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


    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_thread_rollover_is_compare_and_swap_audited_under_owned_lease(
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

        rollover_codex_thread_id(
            claim,
            old_thread_id="thr_old",
            new_thread_id="thr_new",
            reason="remote_compact_404",
        )

        lock_owned_run.assert_called_once()
        sql, parameters = cursor.execute.call_args.args
        self.assertIn("SET codex_thread_id = %s", sql)
        self.assertIn("AND codex_thread_id = %s", sql)
        self.assertEqual(parameters, ("thr_new", claim.card_id, "thr_old"))
        insert_event.assert_called_once()
        event = insert_event.call_args.kwargs
        self.assertEqual(event["event_type"], "codex.thread_rolled_over")
        self.assertEqual(event["payload"]["old_thread_id"], "thr_old")
        self.assertEqual(event["payload"]["new_thread_id"], "thr_new")
        self.assertEqual(event["payload"]["reason"], "remote_compact_404")
        self.assertEqual(event["payload"]["run_id"], claim.id)
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_thread_rollover_rejects_stale_old_thread(
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

        with self.assertRaisesRegex(AgentQueueStateError, "no longer owns"):
            rollover_codex_thread_id(
                claimed_run(phase=RunPhase.IMPLEMENTATION),
                old_thread_id="thr_old",
                new_thread_id="thr_new",
                reason="remote_compact_404",
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
    def _mock_successful_completion(
        self,
        get_db_conn,
        lock_owned_run,
        insert_message,
        *,
        card_status: str,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        get_db_conn.return_value = connection
        lock_owned_run.return_value = {
            "card_status": card_status,
            "phase": card_status,
            "repository_scope": "backend",
        }
        insert_message.return_value = "message-id"
        return connection, cursor

    @patch("backend.services.agent_worker_service.insert_notification")
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
        insert_notification,
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
        insert_notification.assert_called_once()
        notification = insert_notification.call_args.args[0]
        self.assertEqual(notification.title, "Agent plan is ready")
        self.assertEqual(notification.module, "Agent")
        self.assertEqual(notification.data["card_id"], claim.card_id)
        self.assertEqual(notification.data["run_id"], claim.id)
        self.assertEqual(notification.data["status"], "awaiting_implementation_approval")
        self.assertEqual(notification.data["action"], "approve_implementation")

    @patch("backend.services.agent_worker_service.insert_notification")
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
        insert_notification,
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
        insert_notification.assert_called_once()

    @patch("backend.services.agent_worker_service.insert_notification")
    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._insert_message")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_planning_feedback_completion_inserts_actionable_notification(
        self,
        get_db_conn,
        put_db_conn,
        lock_owned_run,
        insert_message,
        insert_event,
        insert_notification,
    ):
        self._mock_successful_completion(
            get_db_conn,
            lock_owned_run,
            insert_message,
            card_status="planning",
        )
        claim = claimed_run()

        complete_run(
            claim,
            ExecutionResult(
                message="Need an answer",
                card_status=CardStatus.AWAITING_FEEDBACK,
                repository_scope=RepositoryScope.BACKEND,
            ),
        )

        insert_notification.assert_called_once()
        notification = insert_notification.call_args.args[0]
        self.assertEqual(notification.title, "Agent needs feedback")
        self.assertEqual(notification.priority, 1)
        self.assertEqual(notification.data["phase"], "planning")
        self.assertEqual(notification.data["status"], "awaiting_feedback")
        self.assertEqual(notification.data["action"], "add_follow_up")

    @patch("backend.services.agent_worker_service.insert_notification")
    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._insert_message")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_implementation_completion_inserts_review_notification(
        self,
        get_db_conn,
        put_db_conn,
        lock_owned_run,
        insert_message,
        insert_event,
        insert_notification,
    ):
        self._mock_successful_completion(
            get_db_conn,
            lock_owned_run,
            insert_message,
            card_status="implementing",
        )
        claim = claimed_run(phase=RunPhase.IMPLEMENTATION)

        complete_run(
            claim,
            ExecutionResult(
                message="Done",
                card_status=CardStatus.REVIEW_READY,
                metadata={"workspace": {"changed_files": ["backend/example.py"]}},
            ),
        )

        insert_notification.assert_called_once()
        notification = insert_notification.call_args.args[0]
        self.assertEqual(notification.title, "Implementation is ready for review")
        self.assertEqual(notification.data["phase"], "implementation")
        self.assertEqual(notification.data["status"], "review_ready")
        self.assertEqual(notification.data["action"], "approve_deployment")

    @patch("backend.services.agent_worker_service.insert_notification")
    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._insert_message")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_backend_completion_inserts_completed_notification(
        self,
        get_db_conn,
        put_db_conn,
        lock_owned_run,
        insert_message,
        insert_event,
        insert_notification,
    ):
        self._mock_successful_completion(
            get_db_conn,
            lock_owned_run,
            insert_message,
            card_status="deploying",
        )
        claim = claimed_run(phase=RunPhase.DEPLOYMENT)

        complete_run(
            claim,
            ExecutionResult(
                message="Deployed",
                card_status=CardStatus.COMPLETED,
                metadata={"candidate": {"changed_files": ["backend/example.py"]}},
            ),
        )

        insert_notification.assert_called_once()
        notification = insert_notification.call_args.args[0]
        self.assertEqual(notification.title, "Agent task completed")
        self.assertEqual(notification.priority, 0)
        self.assertEqual(notification.data["phase"], "deployment")
        self.assertEqual(notification.data["status"], "completed")
        self.assertEqual(notification.data["action"], "view_card")
        self.assertNotIn("frontend_build_ready", notification.data)

    @patch("backend.services.agent_worker_service.insert_notification")
    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._insert_message")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_qa_success_requeues_same_backend_deployment_run(
        self,
        get_db_conn,
        put_db_conn,
        lock_owned_run,
        insert_message,
        insert_event,
        insert_notification,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        get_db_conn.return_value = connection
        lock_owned_run.return_value = {
            "card_status": "deploying",
            "phase": "deployment",
            "repository_scope": "backend",
        }
        insert_message.return_value = "message-id"
        claim = claimed_run(phase=RunPhase.DEPLOYMENT)
        pipeline = {
            "stage": "qa_succeeded",
            "deployment_run_id": claim.id,
            "candidate_commit": "a" * 40,
        }

        complete_run(
            claim,
            ExecutionResult(
                message="QA deployed",
                card_status=CardStatus.DEPLOYMENT_QUEUED,
                metadata={
                    "deployment_pipeline": pipeline,
                    "candidate": {"candidate_commit": "a" * 40},
                },
            ),
        )

        run_update_sql, run_update_parameters = cursor.execute.call_args_list[0].args
        self.assertIn("status = %s", run_update_sql)
        self.assertIn("finished_at = NULL", run_update_sql)
        self.assertIn("result_metadata = %s::jsonb", run_update_sql)
        self.assertEqual(run_update_parameters[0], "queued")
        self.assertEqual(run_update_parameters[1], "message-id")
        self.assertIn('"stage": "qa_succeeded"', run_update_parameters[2])
        self.assertEqual(run_update_parameters[3], claim.id)
        card_update_sql, card_update_parameters = cursor.execute.call_args_list[1].args
        self.assertIn("status = %s", card_update_sql)
        self.assertEqual(card_update_parameters, ("deployment_queued", claim.card_id))
        insert_event.assert_called_once()
        self.assertEqual(insert_event.call_args.kwargs["event_type"], "run.qa_succeeded")
        insert_notification.assert_not_called()
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_worker_service.insert_notification")
    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._insert_message")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_frontend_completion_inserts_build_ready_notification(
        self,
        get_db_conn,
        put_db_conn,
        lock_owned_run,
        insert_message,
        insert_event,
        insert_notification,
    ):
        self._mock_successful_completion(
            get_db_conn,
            lock_owned_run,
            insert_message,
            card_status="deploying",
        )
        claim = claimed_run(phase=RunPhase.DEPLOYMENT)

        complete_run(
            claim,
            ExecutionResult(
                message="Deployed",
                card_status=CardStatus.COMPLETED,
                metadata={
                    "candidate": {
                        "changed_files": [
                            "backend/example.py",
                            "frontend-web/src/App.tsx",
                        ],
                    }
                },
            ),
        )

        insert_notification.assert_called_once()
        notification = insert_notification.call_args.args[0]
        self.assertEqual(notification.title, "Frontend build is ready")
        self.assertEqual(notification.data["frontend_build_ready"], "true")

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

class RunFailureMetadataTests(unittest.TestCase):
    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_block_run_merges_metadata_without_losing_deployment_pipeline(
        self,
        get_db_conn,
        put_db_conn,
        lock_owned_run,
        insert_event,
    ):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        get_db_conn.return_value = connection
        claim = claimed_run(phase=RunPhase.DEPLOYMENT)
        claim = type(claim)(
            **{
                **claim.__dict__,
                "result_metadata": {
                    "deployment_pipeline": {
                        "stage": "qa_succeeded",
                        "candidate_commit": "a" * 40,
                    }
                },
            }
        )
        lock_owned_run.return_value = {
            "card_status": "deploying",
            "phase": "deployment",
            "repository_scope": "backend",
        }

        block_run(
            claim,
            reason="GitHub sync pending",
            retry_after_seconds=60,
            metadata={"deployment_recovery": {"retryable": True}},
        )

        run_update_sql, run_update_parameters = cursor.execute.call_args_list[0].args
        self.assertIn("result_metadata = %s::jsonb", run_update_sql)
        self.assertIn('"deployment_pipeline"', run_update_parameters[3])
        self.assertIn('"deployment_recovery"', run_update_parameters[3])
        insert_event.assert_called_once()
        self.assertIn(
            "deployment_pipeline",
            insert_event.call_args.kwargs["payload"]["metadata"],
        )
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

    @patch("backend.services.agent_worker_service._insert_event")
    @patch("backend.services.agent_worker_service._insert_message")
    @patch("backend.services.agent_worker_service._lock_owned_run")
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_fail_run_preserves_deployment_pipeline_without_parameter_mismatch(
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
        claim = claimed_run()
        claim = type(claim)(
            **{
                **claim.__dict__,
                "result_metadata": {
                    "deployment_pipeline": {
                        "stage": "qa_succeeded",
                        "candidate_commit": "a" * 40,
                    },
                    "discarded": True,
                },
            }
        )
        lock_owned_run.return_value = {
            "card_status": "planning",
            "phase": "planning",
            "repository_scope": "backend",
        }

        fail_run(claim, error_message="boom")

        run_update_sql, run_update_parameters = cursor.execute.call_args_list[0].args
        self.assertIn("result_metadata = %s::jsonb", run_update_sql)
        self.assertEqual(run_update_sql.count("%s"), len(run_update_parameters))
        self.assertEqual(
            run_update_parameters[0:2],
            ("failed", "boom"),
        )
        self.assertIn('"deployment_pipeline"', run_update_parameters[2])
        self.assertNotIn("discarded", run_update_parameters[2])
        self.assertEqual(run_update_parameters[3], claim.id)
        insert_message.assert_called_once()
        insert_event.assert_called_once()
        connection.commit.assert_called_once_with()
        put_db_conn.assert_called_once_with(connection)

if __name__ == "__main__":
    unittest.main()

import os
import threading
import unittest
from unittest.mock import MagicMock, patch

from backend.agent_worker import AgentWorkerSettings, build_executor
from backend.core.agent_state import CardStatus, RepositoryScope, RunPhase
from backend.core.agent_worker import (
    AgentLeaseLostError,
    AgentTemporarilyBlockedError,
    AgentWorker,
    AgentWorkerConfigurationError,
    ClaimedRun,
    ExecutionResult,
    FakeAgentExecutor,
)


def _wait_until(predicate, *, timeout: float) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def claimed_run(
    *,
    phase: RunPhase = RunPhase.PLANNING,
    attempt_count: int = 1,
    repository_scope: RepositoryScope | None = None,
) -> ClaimedRun:
    active_status = {
        RunPhase.PLANNING: CardStatus.PLANNING,
        RunPhase.IMPLEMENTATION: CardStatus.IMPLEMENTING,
        RunPhase.DEPLOYMENT: CardStatus.DEPLOYING,
    }[phase]
    return ClaimedRun(
        id="4c0056d9-cfab-4a7e-b8a8-369ea90efee8",
        card_id="3d8549c4-a965-4d2e-aacf-9df7e6ccdbb4",
        phase=phase,
        card_status=active_status,
        card_revision=1,
        attempt_count=attempt_count,
        lease_token="a65bce12-7ab7-47a9-9e93-cb0a58fd49ea",
        worker_id="qa-worker",
        title="Medication tracking",
        description="Plan a medication tracking module.",
        repository_scope=(
            repository_scope
            if repository_scope is not None
            else (
                RepositoryScope.AUTO
                if phase is RunPhase.PLANNING
                else RepositoryScope.BACKEND
            )
        ),
        messages=(),
    )


class AgentWorkerOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.queue = MagicMock()
        self.executor = MagicMock()
        self.executor.allowed_phases = frozenset({RunPhase.PLANNING})
        self.worker = AgentWorker(
            queue=self.queue,
            executor=self.executor,
            worker_id="qa-worker",
            lease_seconds=120,
            heartbeat_seconds=30,
            max_attempts=3,
        )

    def test_empty_queue_returns_false(self):
        self.queue.claim_next_run.return_value = None

        self.assertFalse(self.worker.process_once())
        self.queue.claim_next_run.assert_called_once_with(
            worker_id="qa-worker",
            lease_seconds=120,
            allowed_phases=frozenset({RunPhase.PLANNING}),
        )
        self.queue.start_run.assert_not_called()

    def test_successful_execution_completes_run(self):
        claim = claimed_run()
        result = ExecutionResult(
            message="Plan ready",
            card_status=CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
        )
        self.queue.claim_next_run.return_value = claim
        self.executor.execute.return_value = result

        self.assertTrue(self.worker.process_once())

        self.queue.start_run.assert_called_once_with(
            claim,
            lease_seconds=120,
        )
        self.executor.execute.assert_called_once_with(claim)
        self.queue.complete_run.assert_called_once_with(claim, result)
        self.queue.fail_run.assert_not_called()

    def test_long_execution_renews_lease(self):
        claim = claimed_run()
        result = ExecutionResult(
            message="Plan ready",
            card_status=CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
        )
        execution_started = threading.Event()
        execution_release = threading.Event()

        def execute(_claim):
            execution_started.set()
            self.assertTrue(execution_release.wait(timeout=2))
            return result

        self.worker.heartbeat_seconds = 0.01
        self.queue.claim_next_run.return_value = claim
        self.executor.execute.side_effect = execute

        worker_thread = threading.Thread(target=self.worker.process_once)
        worker_thread.start()
        self.assertTrue(execution_started.wait(timeout=1))
        self.assertTrue(
            _wait_until(
                lambda: self.queue.heartbeat_run.call_count >= 1,
                timeout=1,
            )
        )
        execution_release.set()
        worker_thread.join(timeout=2)

        self.assertFalse(worker_thread.is_alive())
        self.queue.complete_run.assert_called_once_with(claim, result)

    def test_lease_loss_during_execution_fences_completion(self):
        claim = claimed_run()
        execution_started = threading.Event()
        execution_release = threading.Event()

        def execute(_claim):
            execution_started.set()
            self.assertTrue(execution_release.wait(timeout=2))
            return ExecutionResult(
                message="Plan ready",
                card_status=CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
            )

        self.worker.heartbeat_seconds = 0.01
        self.queue.claim_next_run.return_value = claim
        self.queue.heartbeat_run.side_effect = AgentLeaseLostError("reclaimed")
        self.executor.execute.side_effect = execute

        with self.assertLogs("remihub.agent_worker", level="WARNING"):
            worker_thread = threading.Thread(target=self.worker.process_once)
            worker_thread.start()
            self.assertTrue(execution_started.wait(timeout=1))
            self.assertTrue(
                _wait_until(
                    lambda: self.queue.heartbeat_run.call_count >= 1,
                    timeout=1,
                )
            )
            execution_release.set()
            worker_thread.join(timeout=2)

        self.queue.complete_run.assert_not_called()
        self.queue.fail_run.assert_not_called()
        self.executor.cancel.assert_called_once_with(claim)

    def test_temporary_limit_blocks_run_for_retry(self):
        claim = claimed_run()
        self.queue.claim_next_run.return_value = claim
        self.executor.execute.side_effect = AgentTemporarilyBlockedError(
            "Usage limit reached",
            retry_after_seconds=900,
        )

        self.assertTrue(self.worker.process_once())

        self.queue.block_run.assert_called_once_with(
            claim,
            reason="Usage limit reached",
            retry_after_seconds=900,
            metadata={},
        )
        self.queue.fail_run.assert_not_called()

    def test_executor_error_marks_run_failed(self):
        claim = claimed_run()
        self.queue.claim_next_run.return_value = claim
        self.executor.execute.side_effect = RuntimeError("executor exploded")

        with self.assertLogs("remihub.agent_worker", level="ERROR"):
            self.assertTrue(self.worker.process_once())

        self.queue.fail_run.assert_called_once_with(
            claim,
            error_message="RuntimeError: executor exploded",
        )

    def test_executor_failure_cannot_overwrite_reclaimed_run(self):
        claim = claimed_run()
        self.queue.claim_next_run.return_value = claim
        self.executor.execute.side_effect = RuntimeError("executor exploded")
        self.queue.fail_run.side_effect = AgentLeaseLostError("reclaimed")

        with self.assertLogs("remihub.agent_worker", level="WARNING"):
            self.assertTrue(self.worker.process_once())

        self.queue.fail_run.assert_called_once()

    def test_completion_database_error_is_not_reclassified(self):
        claim = claimed_run()
        result = ExecutionResult(
            message="Plan ready",
            card_status=CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
        )
        self.queue.claim_next_run.return_value = claim
        self.executor.execute.return_value = result
        self.queue.complete_run.side_effect = RuntimeError("database unavailable")

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            self.worker.process_once()

        self.queue.fail_run.assert_not_called()

    def test_stale_worker_does_not_fail_reclaimed_run(self):
        claim = claimed_run()
        self.queue.claim_next_run.return_value = claim
        self.queue.start_run.side_effect = AgentLeaseLostError("reclaimed")

        with self.assertLogs("remihub.agent_worker", level="WARNING"):
            self.assertTrue(self.worker.process_once())

        self.executor.execute.assert_not_called()
        self.queue.fail_run.assert_not_called()

    def test_maximum_attempts_fails_without_execution(self):
        claim = claimed_run(attempt_count=4)
        self.queue.claim_next_run.return_value = claim

        self.assertTrue(self.worker.process_once())

        self.executor.execute.assert_not_called()
        self.queue.fail_run.assert_called_once_with(
            claim,
            error_message="Maximum worker attempts exceeded (3)",
        )


class FakeAgentExecutorTests(unittest.TestCase):
    def test_fake_executor_returns_phase_appropriate_states(self):
        executor = FakeAgentExecutor()

        self.assertEqual(
            executor.execute(claimed_run(phase=RunPhase.PLANNING)).card_status,
            CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
        )
        self.assertEqual(
            executor.execute(claimed_run(phase=RunPhase.IMPLEMENTATION)).card_status,
            CardStatus.REVIEW_READY,
        )
        self.assertEqual(
            executor.execute(claimed_run(phase=RunPhase.DEPLOYMENT)).card_status,
            CardStatus.COMPLETED,
        )

    def test_fake_planning_resolves_backend_scope(self):
        result = FakeAgentExecutor().execute(claimed_run())

        self.assertEqual(result.repository_scope, RepositoryScope.BACKEND)


class AgentWorkerSettingsTests(unittest.TestCase):
    def test_worker_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = AgentWorkerSettings.from_environment()

        self.assertEqual(settings.environment, "production")
        self.assertEqual(settings.executor_name, "disabled")
        with self.assertRaises(AgentWorkerConfigurationError):
            build_executor(settings)

    def test_fake_executor_requires_qa_and_explicit_gate(self):
        with patch.dict(
            os.environ,
            {
                "REMIHUB_AGENT_ENVIRONMENT": "qa",
                "REMIHUB_AGENT_EXECUTOR": "fake",
                "REMIHUB_AGENT_ALLOW_FAKE_EXECUTOR": "true",
            },
            clear=True,
        ):
            settings = AgentWorkerSettings.from_environment()

        self.assertIsInstance(build_executor(settings), FakeAgentExecutor)

    def test_fake_executor_is_rejected_in_production(self):
        with patch.dict(
            os.environ,
            {
                "REMIHUB_AGENT_ENVIRONMENT": "production",
                "REMIHUB_AGENT_EXECUTOR": "fake",
                "REMIHUB_AGENT_ALLOW_FAKE_EXECUTOR": "true",
            },
            clear=True,
        ):
            settings = AgentWorkerSettings.from_environment()

        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "restricted to QA",
        ):
            build_executor(settings)

    @patch("backend.agent_worker.CodexPlanningExecutor")
    def test_planning_executor_supports_legacy_backend_repository(
        self,
        planning_executor,
    ):
        with patch.dict(
            os.environ,
            {
                "REMIHUB_AGENT_EXECUTOR": "codex-planning",
                "REMIHUB_AGENT_REPOSITORY": "/srv/agent/backend-planning",
            },
            clear=True,
        ):
            settings = AgentWorkerSettings.from_environment()

        queue = MagicMock()
        result = build_executor(settings, queue=queue)

        self.assertEqual(result, planning_executor.return_value)
        planning_executor.assert_called_once_with(
            repository_path="/srv/agent/backend-planning",
            planning_workspace_path=None,
            backend_repository_path=None,
            android_repository_path=None,
            thread_store=queue,
            model=None,
            retry_after_seconds=900,
        )

    @patch("backend.agent_worker.CodexPlanningExecutor")
    def test_planning_executor_uses_dual_repository_workspace(
        self,
        planning_executor,
    ):
        with patch.dict(
            os.environ,
            {
                "REMIHUB_AGENT_EXECUTOR": "codex-planning",
                "REMIHUB_AGENT_PLANNING_WORKSPACE": "/srv/agent/planning",
                "REMIHUB_AGENT_BACKEND_REPOSITORY": "/srv/agent/planning/backend",
                "REMIHUB_AGENT_ANDROID_REPOSITORY": "/srv/agent/planning/android",
            },
            clear=True,
        ):
            settings = AgentWorkerSettings.from_environment()

        queue = MagicMock()
        result = build_executor(settings, queue=queue)

        self.assertEqual(result, planning_executor.return_value)
        planning_executor.assert_called_once_with(
            repository_path=None,
            planning_workspace_path="/srv/agent/planning",
            backend_repository_path="/srv/agent/planning/backend",
            android_repository_path="/srv/agent/planning/android",
            thread_store=queue,
            model=None,
            retry_after_seconds=900,
        )

    @patch("backend.agent_worker.CodexImplementationExecutor")
    @patch("backend.agent_worker.GitImplementationWorkspaceManager")
    def test_implementation_executor_requires_and_uses_workspace_paths(
        self,
        workspace_manager,
        implementation_executor,
    ):
        with patch.dict(
            os.environ,
            {
                "REMIHUB_AGENT_EXECUTOR": "codex-implementation",
                "REMIHUB_AGENT_REPOSITORY": "/srv/agent/source.git",
                "REMIHUB_AGENT_WORKTREE_ROOT": "/srv/agent/worktrees",
                "REMIHUB_AGENT_ARTIFACT_ROOT": "/srv/agent/artifacts",
                "REMIHUB_AGENT_GIT_TIMEOUT_SECONDS": "45",
                "REMIHUB_CODEX_BIN": "/srv/agent/bin/codex-sandbox",
            },
            clear=True,
        ):
            settings = AgentWorkerSettings.from_environment()

        queue = MagicMock()
        result = build_executor(settings, queue=queue)

        self.assertEqual(result, implementation_executor.return_value)
        workspace_manager.assert_called_once_with(
            source_repository="/srv/agent/source.git",
            worktree_root="/srv/agent/worktrees",
            artifact_root="/srv/agent/artifacts",
            command_timeout_seconds=45,
        )
        implementation_executor.assert_called_once_with(
            workspace_manager=workspace_manager.return_value,
            workspace_store=queue,
            codex_bin="/srv/agent/bin/codex-sandbox",
            model=None,
            retry_after_seconds=900,
        )


    @patch("backend.agent_worker.CodexImplementationExecutor")
    @patch("backend.agent_worker.CommandImplementationValidator")
    @patch("backend.agent_worker.GitImplementationWorkspaceManager")
    def test_android_implementation_executor_uses_scope_and_validator(
        self,
        workspace_manager,
        implementation_validator,
        implementation_executor,
    ):
        with patch.dict(
            os.environ,
            {
                "REMIHUB_AGENT_EXECUTOR": "codex-implementation",
                "REMIHUB_AGENT_REPOSITORY": "/srv/agent/android.git",
                "REMIHUB_AGENT_WORKTREE_ROOT": "/srv/agent/android-worktrees",
                "REMIHUB_AGENT_ARTIFACT_ROOT": "/srv/agent/android-artifacts",
                "REMIHUB_AGENT_REPOSITORY_SCOPE": "android",
                "REMIHUB_AGENT_BASE_BRANCH_OVERRIDE": "master",
                "REMIHUB_AGENT_IMPLEMENTATION_VALIDATOR": (
                    "/srv/agent/bin/android-validator"
                ),
                "REMIHUB_AGENT_IMPLEMENTATION_VALIDATION_TIMEOUT_SECONDS": (
                    "1800"
                ),
                "REMIHUB_CODEX_BIN": "/srv/agent/bin/android-codex-sandbox",
            },
            clear=True,
        ):
            settings = AgentWorkerSettings.from_environment()

        queue = MagicMock()
        result = build_executor(settings, queue=queue)

        self.assertEqual(result, implementation_executor.return_value)
        workspace_manager.assert_called_once_with(
            source_repository="/srv/agent/android.git",
            worktree_root="/srv/agent/android-worktrees",
            artifact_root="/srv/agent/android-artifacts",
            command_timeout_seconds=120,
            base_branch_override="master",
        )
        implementation_validator.assert_called_once_with(
            command="/srv/agent/bin/android-validator",
            timeout_seconds=1800,
        )
        implementation_executor.assert_called_once_with(
            workspace_manager=workspace_manager.return_value,
            workspace_store=queue,
            codex_bin="/srv/agent/bin/android-codex-sandbox",
            model=None,
            retry_after_seconds=900,
            repository_scope=RepositoryScope.ANDROID,
            validator=implementation_validator.return_value,
        )

    def test_android_implementation_requires_trusted_validator(self):
        with patch.dict(
            os.environ,
            {
                "REMIHUB_AGENT_EXECUTOR": "codex-implementation",
                "REMIHUB_AGENT_REPOSITORY": "/srv/agent/android.git",
                "REMIHUB_AGENT_WORKTREE_ROOT": "/srv/agent/android-worktrees",
                "REMIHUB_AGENT_ARTIFACT_ROOT": "/srv/agent/android-artifacts",
                "REMIHUB_AGENT_REPOSITORY_SCOPE": "android",
                "REMIHUB_CODEX_BIN": "/srv/agent/bin/android-codex-sandbox",
            },
            clear=True,
        ):
            settings = AgentWorkerSettings.from_environment()

        with patch("backend.agent_worker.GitImplementationWorkspaceManager"):
            with self.assertRaisesRegex(
                AgentWorkerConfigurationError,
                "Android implementation requires",
            ):
                build_executor(settings, queue=MagicMock())

    def test_implementation_executor_rejects_missing_artifact_root(self):
        with patch.dict(
            os.environ,
            {
                "REMIHUB_AGENT_EXECUTOR": "codex-implementation",
                "REMIHUB_AGENT_REPOSITORY": "/srv/agent/source.git",
                "REMIHUB_AGENT_WORKTREE_ROOT": "/srv/agent/worktrees",
                "REMIHUB_CODEX_BIN": "/srv/agent/bin/codex-sandbox",
            },
            clear=True,
        ):
            settings = AgentWorkerSettings.from_environment()

        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "REMIHUB_AGENT_ARTIFACT_ROOT",
        ):
            build_executor(settings, queue=MagicMock())

    def test_implementation_executor_rejects_missing_sandbox_wrapper(self):
        with patch.dict(
            os.environ,
            {
                "REMIHUB_AGENT_EXECUTOR": "codex-implementation",
                "REMIHUB_AGENT_REPOSITORY": "/srv/agent/source.git",
                "REMIHUB_AGENT_WORKTREE_ROOT": "/srv/agent/worktrees",
                "REMIHUB_AGENT_ARTIFACT_ROOT": "/srv/agent/artifacts",
            },
            clear=True,
        ):
            settings = AgentWorkerSettings.from_environment()

        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "REMIHUB_CODEX_BIN",
        ):
            build_executor(settings, queue=MagicMock())


class BackendDeploymentWorkerSettingsTests(unittest.TestCase):
    @patch("backend.agent_worker.GitBackendDeploymentExecutor")
    @patch("backend.agent_worker.GitBackendDeploymentManager")
    @patch("backend.agent_worker.PrivilegedDeploymentRuntime")
    @patch("backend.agent_worker.PostgresDeploymentDatabase")
    @patch("backend.agent_worker.SandboxBackendValidator")
    def test_qa_backend_deployment_executor_uses_separate_authority_objects(
        self,
        sandbox_validator,
        deployment_database,
        deployment_runtime,
        deployment_manager,
        deployment_executor,
    ):
        with patch.dict(
            os.environ,
            {
                "REMIHUB_AGENT_ENVIRONMENT": "qa",
                "REMIHUB_AGENT_EXECUTOR": "git-backend-deployment",
                "REMIHUB_AGENT_REPOSITORY": "/srv/agent/source.git",
                "REMIHUB_AGENT_WORKTREE_ROOT": "/srv/agent/worktrees",
                "REMIHUB_AGENT_ARTIFACT_ROOT": "/srv/agent/artifacts",
                "REMIHUB_AGENT_DEPLOYMENT_TARGET_REPOSITORY": (
                    "/srv/agent/qa-deployment.git"
                ),
                "REMIHUB_AGENT_DEPLOYMENT_WORKTREE_ROOT": (
                    "/srv/agent/deployment-worktrees"
                ),
                "REMIHUB_AGENT_DEPLOYMENT_ARTIFACT_ROOT": (
                    "/srv/agent/deployment-artifacts"
                ),
                "REMIHUB_AGENT_DEPLOYMENT_TARGET_BRANCH": "qa-main",
                "REMIHUB_AGENT_DEPLOYMENT_DATABASE_CONFIG": (
                    "/srv/agent/config/qa-migrator.ini"
                ),
                "REMIHUB_AGENT_DEPLOYMENT_DATABASE_OWNER_ROLE": "remihub_qa_owner",
                "REMIHUB_AGENT_DEPLOYMENT_BACKUP_ROOT": "/srv/agent/backups",
                "REMIHUB_AGENT_DEPLOYMENT_PG_DUMP_BINARY": (
                    "/usr/lib/postgresql/16/bin/pg_dump"
                ),
                "REMIHUB_AGENT_DEPLOYMENT_PG_RESTORE_BINARY": (
                    "/usr/lib/postgresql/16/bin/pg_restore"
                ),
                "REMIHUB_AGENT_DEPLOYMENT_VALIDATOR": "/srv/agent/bin/validate",
                "REMIHUB_AGENT_DEPLOYMENT_RUNTIME_HELPER": (
                    "/srv/agent/bin/runtime-helper"
                ),
                "REMIHUB_AGENT_DEPLOYMENT_HEALTH_URL": (
                    "http://127.0.0.1:8001/openapi.json"
                ),
                "REMIHUB_AGENT_DEPLOYMENT_TIMEOUT_SECONDS": "600",
                "REMIHUB_AGENT_DEPLOYMENT_RETRY_SECONDS": "75",
                "REMIHUB_AGENT_GIT_TIMEOUT_SECONDS": "45",
            },
            clear=True,
        ):
            settings = AgentWorkerSettings.from_environment()

        result = build_executor(settings, queue=MagicMock())

        self.assertEqual(result, deployment_executor.return_value)
        sandbox_validator.assert_called_once_with(
            validation_command="/srv/agent/bin/validate",
            timeout_seconds=600,
        )
        deployment_database.assert_called_once_with(
            config_path="/srv/agent/config/qa-migrator.ini",
            backup_root="/srv/agent/backups",
            owner_role="remihub_qa_owner",
            pg_dump_binary="/usr/lib/postgresql/16/bin/pg_dump",
            pg_restore_binary="/usr/lib/postgresql/16/bin/pg_restore",
            command_timeout_seconds=600,
        )
        deployment_runtime.assert_called_once_with(
            environment="qa",
            helper_path="/srv/agent/bin/runtime-helper",
            health_url="http://127.0.0.1:8001/openapi.json",
            command_timeout_seconds=600,
        )
        deployment_manager.assert_called_once_with(
            environment="qa",
            source_repository="/srv/agent/source.git",
            source_worktree_root="/srv/agent/worktrees",
            source_artifact_root="/srv/agent/artifacts",
            target_repository="/srv/agent/qa-deployment.git",
            candidate_worktree_root="/srv/agent/deployment-worktrees",
            deployment_artifact_root="/srv/agent/deployment-artifacts",
            target_branch="qa-main",
            validator=sandbox_validator.return_value,
            database=deployment_database.return_value,
            runtime=deployment_runtime.return_value,
            qa_history_reader=None,
            command_timeout_seconds=45,
        )
        deployment_executor.assert_called_once_with(
            deployment_manager=deployment_manager.return_value,
            github_synchronizer=None,
            retry_after_seconds=75,
        )

    @patch("backend.agent_worker.GitBackendDeploymentExecutor")
    @patch("backend.agent_worker.GitBackendDeploymentManager")
    @patch("backend.agent_worker.verify_distinct_database_identities")
    @patch("backend.agent_worker.PrivilegedBackendGitHubSynchronizer")
    @patch("backend.agent_worker.PrivilegedDeploymentRuntime")
    @patch("backend.agent_worker.PostgresMigrationHistoryReader")
    @patch("backend.agent_worker.PostgresDeploymentDatabase")
    @patch("backend.agent_worker.SandboxBackendValidator")
    def test_production_backend_deployment_uses_production_target(
        self,
        sandbox_validator,
        deployment_database,
        qa_history_reader,
        deployment_runtime,
        github_synchronizer,
        verify_distinct,
        deployment_manager,
        deployment_executor,
    ):
        environment = {
            "REMIHUB_AGENT_ENVIRONMENT": "production",
            "REMIHUB_AGENT_EXECUTOR": "git-backend-deployment",
            "REMIHUB_AGENT_REPOSITORY": "/srv/agent/source.git",
            "REMIHUB_AGENT_WORKTREE_ROOT": "/srv/agent/worktrees",
            "REMIHUB_AGENT_ARTIFACT_ROOT": "/srv/agent/artifacts",
            "REMIHUB_AGENT_DEPLOYMENT_TARGET_REPOSITORY": (
                "/srv/agent/production-deployment.git"
            ),
            "REMIHUB_AGENT_DEPLOYMENT_WORKTREE_ROOT": (
                "/srv/agent/production-worktrees"
            ),
            "REMIHUB_AGENT_DEPLOYMENT_ARTIFACT_ROOT": (
                "/srv/agent/production-artifacts"
            ),
            "REMIHUB_AGENT_DEPLOYMENT_DATABASE_CONFIG": (
                "/srv/agent/config/prod-migrator.ini"
            ),
            "REMIHUB_AGENT_DEPLOYMENT_DATABASE_OWNER_ROLE": "remihub",
            "REMIHUB_AGENT_DEPLOYMENT_QA_PARITY_DATABASE_CONFIG": (
                "/srv/agent/config/qa-parity-reader.ini"
            ),
            "REMIHUB_AGENT_DEPLOYMENT_QA_PARITY_DATABASE_ROLE": (
                "remihub_qa_migration_reader"
            ),
            "REMIHUB_AGENT_DEPLOYMENT_BACKUP_ROOT": "/srv/agent/backups",
            "REMIHUB_AGENT_DEPLOYMENT_PG_DUMP_BINARY": (
                "/usr/lib/postgresql/16/bin/pg_dump"
            ),
            "REMIHUB_AGENT_DEPLOYMENT_PG_RESTORE_BINARY": (
                "/usr/lib/postgresql/16/bin/pg_restore"
            ),
            "REMIHUB_AGENT_DEPLOYMENT_VALIDATOR": "/srv/agent/bin/validate",
            "REMIHUB_AGENT_DEPLOYMENT_RUNTIME_HELPER": (
                "/srv/agent/bin/runtime-helper"
            ),
            "REMIHUB_AGENT_DEPLOYMENT_GITHUB_SYNC_HELPER": (
                "/srv/agent/bin/github-sync-helper"
            ),
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = AgentWorkerSettings.from_environment()

        result = build_executor(settings, queue=MagicMock())

        self.assertEqual(settings.deployment_target_branch, "production-main")
        self.assertEqual(result, deployment_executor.return_value)
        self.assertEqual(
            deployment_manager.call_args.kwargs["environment"],
            "production",
        )
        self.assertEqual(
            deployment_manager.call_args.kwargs["target_branch"],
            "production-main",
        )
        qa_history_reader.assert_called_once_with(
            config_path="/srv/agent/config/qa-parity-reader.ini",
            role="remihub_qa_migration_reader",
        )
        verify_distinct.assert_called_once_with(
            deployment_database.return_value,
            qa_history_reader.return_value,
        )
        self.assertEqual(
            deployment_manager.call_args.kwargs["qa_history_reader"],
            qa_history_reader.return_value,
        )
        github_synchronizer.assert_called_once_with(
            helper_path="/srv/agent/bin/github-sync-helper",
            command_timeout_seconds=900,
        )
        deployment_executor.assert_called_once_with(
            deployment_manager=deployment_manager.return_value,
            github_synchronizer=github_synchronizer.return_value,
            retry_after_seconds=60,
        )

    def test_production_backend_deployment_requires_github_sync_helper(self):
        environment = {
            "REMIHUB_AGENT_ENVIRONMENT": "production",
            "REMIHUB_AGENT_EXECUTOR": "git-backend-deployment",
            "REMIHUB_AGENT_REPOSITORY": "/srv/agent/source.git",
            "REMIHUB_AGENT_WORKTREE_ROOT": "/srv/agent/worktrees",
            "REMIHUB_AGENT_ARTIFACT_ROOT": "/srv/agent/artifacts",
            "REMIHUB_AGENT_DEPLOYMENT_TARGET_REPOSITORY": "/srv/agent/production.git",
            "REMIHUB_AGENT_DEPLOYMENT_WORKTREE_ROOT": "/srv/agent/production-worktrees",
            "REMIHUB_AGENT_DEPLOYMENT_ARTIFACT_ROOT": "/srv/agent/production-artifacts",
            "REMIHUB_AGENT_DEPLOYMENT_DATABASE_CONFIG": "/srv/agent/prod.ini",
            "REMIHUB_AGENT_DEPLOYMENT_DATABASE_OWNER_ROLE": "remihub",
            "REMIHUB_AGENT_DEPLOYMENT_BACKUP_ROOT": "/srv/agent/backups",
            "REMIHUB_AGENT_DEPLOYMENT_PG_DUMP_BINARY": "/usr/bin/pg_dump",
            "REMIHUB_AGENT_DEPLOYMENT_PG_RESTORE_BINARY": "/usr/bin/pg_restore",
            "REMIHUB_AGENT_DEPLOYMENT_VALIDATOR": "/srv/agent/validate",
            "REMIHUB_AGENT_DEPLOYMENT_RUNTIME_HELPER": "/srv/agent/runtime-helper",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = AgentWorkerSettings.from_environment()

        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "REMIHUB_AGENT_DEPLOYMENT_GITHUB_SYNC_HELPER",
        ):
            build_executor(settings, queue=MagicMock())

    def test_production_backend_deployment_requires_qa_parity_config(self):
        environment = {
            "REMIHUB_AGENT_ENVIRONMENT": "production",
            "REMIHUB_AGENT_EXECUTOR": "git-backend-deployment",
            "REMIHUB_AGENT_REPOSITORY": "/srv/agent/source.git",
            "REMIHUB_AGENT_WORKTREE_ROOT": "/srv/agent/worktrees",
            "REMIHUB_AGENT_ARTIFACT_ROOT": "/srv/agent/artifacts",
            "REMIHUB_AGENT_DEPLOYMENT_TARGET_REPOSITORY": "/srv/agent/production.git",
            "REMIHUB_AGENT_DEPLOYMENT_WORKTREE_ROOT": "/srv/agent/production-worktrees",
            "REMIHUB_AGENT_DEPLOYMENT_ARTIFACT_ROOT": "/srv/agent/production-artifacts",
            "REMIHUB_AGENT_DEPLOYMENT_DATABASE_CONFIG": "/srv/agent/prod.ini",
            "REMIHUB_AGENT_DEPLOYMENT_DATABASE_OWNER_ROLE": "remihub",
            "REMIHUB_AGENT_DEPLOYMENT_BACKUP_ROOT": "/srv/agent/backups",
            "REMIHUB_AGENT_DEPLOYMENT_PG_DUMP_BINARY": "/usr/bin/pg_dump",
            "REMIHUB_AGENT_DEPLOYMENT_PG_RESTORE_BINARY": "/usr/bin/pg_restore",
            "REMIHUB_AGENT_DEPLOYMENT_VALIDATOR": "/srv/agent/validate",
            "REMIHUB_AGENT_DEPLOYMENT_RUNTIME_HELPER": "/srv/agent/runtime-helper",
            "REMIHUB_AGENT_DEPLOYMENT_GITHUB_SYNC_HELPER": "/srv/agent/github-sync",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = AgentWorkerSettings.from_environment()

        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "REMIHUB_AGENT_DEPLOYMENT_QA_PARITY_DATABASE_CONFIG",
        ):
            build_executor(settings, queue=MagicMock())

    def test_backend_deployment_requires_explicit_postgresql_clients(self):
        environment = {
            "REMIHUB_AGENT_ENVIRONMENT": "qa",
            "REMIHUB_AGENT_EXECUTOR": "git-backend-deployment",
            "REMIHUB_AGENT_REPOSITORY": "/srv/agent/source.git",
            "REMIHUB_AGENT_WORKTREE_ROOT": "/srv/agent/worktrees",
            "REMIHUB_AGENT_ARTIFACT_ROOT": "/srv/agent/artifacts",
            "REMIHUB_AGENT_DEPLOYMENT_TARGET_REPOSITORY": "/srv/agent/qa.git",
            "REMIHUB_AGENT_DEPLOYMENT_WORKTREE_ROOT": "/srv/agent/qa-worktrees",
            "REMIHUB_AGENT_DEPLOYMENT_ARTIFACT_ROOT": "/srv/agent/qa-artifacts",
            "REMIHUB_AGENT_DEPLOYMENT_DATABASE_CONFIG": "/srv/agent/qa.ini",
            "REMIHUB_AGENT_DEPLOYMENT_DATABASE_OWNER_ROLE": "remihub_qa_owner",
            "REMIHUB_AGENT_DEPLOYMENT_BACKUP_ROOT": "/srv/agent/backups",
            "REMIHUB_AGENT_DEPLOYMENT_VALIDATOR": "/srv/agent/validate",
            "REMIHUB_AGENT_DEPLOYMENT_RUNTIME_HELPER": "/srv/agent/helper",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = AgentWorkerSettings.from_environment()

        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "REMIHUB_AGENT_DEPLOYMENT_PG_DUMP_BINARY",
        ):
            build_executor(settings, queue=MagicMock())

    def test_legacy_qa_executor_name_is_rejected_in_production(self):
        with patch.dict(
            os.environ,
            {
                "REMIHUB_AGENT_ENVIRONMENT": "production",
                "REMIHUB_AGENT_EXECUTOR": "git-deployment-qa",
            },
            clear=True,
        ):
            settings = AgentWorkerSettings.from_environment()

        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "restricted to QA",
        ):
            build_executor(settings, queue=MagicMock())

    def test_backend_deployment_requires_target_repository_first(
        self,
    ):
        with patch.dict(
            os.environ,
            {
                "REMIHUB_AGENT_ENVIRONMENT": "qa",
                "REMIHUB_AGENT_EXECUTOR": "git-backend-deployment",
                "REMIHUB_AGENT_REPOSITORY": "/srv/agent/source.git",
                "REMIHUB_AGENT_WORKTREE_ROOT": "/srv/agent/worktrees",
                "REMIHUB_AGENT_ARTIFACT_ROOT": "/srv/agent/artifacts",
            },
            clear=True,
        ):
            settings = AgentWorkerSettings.from_environment()

        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "REMIHUB_AGENT_DEPLOYMENT_TARGET_REPOSITORY",
        ):
            build_executor(settings, queue=MagicMock())


if __name__ == "__main__":
    unittest.main()

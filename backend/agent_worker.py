from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from dataclasses import dataclass

from backend.core.android_deployment import (
    CommandAndroidReleaseValidator,
    GitAndroidDeploymentExecutor,
    GitAndroidDeploymentManager,
    PrivilegedAndroidReleaseRuntime,
)
from backend.core.agent_deployment import (
    GitBackendDeploymentExecutor,
    GitBackendDeploymentManager,
    PostgresMigrationHistoryReader,
    PostgresDeploymentDatabase,
    PrivilegedBackendGitHubSynchronizer,
    PrivilegedDeploymentRuntime,
    SandboxBackendValidator,
    verify_distinct_database_identities,
)
from backend.core.agent_worker import (
    AgentWorker,
    AgentWorkerConfigurationError,
    FakeAgentExecutor,
)
from backend.core.agent_workspace import GitImplementationWorkspaceManager
from backend.core.codex_implementation import (
    CodexImplementationExecutor,
    CommandImplementationValidator,
)
from backend.core.codex_planning import CodexPlanningExecutor
from backend.core.agent_state import RepositoryScope
from backend.services.agent_worker_service import DatabaseAgentQueue


logger = logging.getLogger("remihub.agent_worker")


def _positive_int(name: str, default: int, *, minimum: int = 1) -> int:
    configured = os.environ.get(name, str(default)).strip()
    try:
        value = int(configured)
    except ValueError as exc:
        raise AgentWorkerConfigurationError(f"{name} must be an integer") from exc

    if value < minimum:
        raise AgentWorkerConfigurationError(f"{name} must be at least {minimum}")
    return value


def _boolean(name: str, default: bool = False) -> bool:
    configured = os.environ.get(name)
    if configured is None:
        return default

    normalized = configured.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise AgentWorkerConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True)
class AgentWorkerSettings:
    environment: str
    executor_name: str
    worker_id: str
    poll_seconds: int
    lease_seconds: int
    heartbeat_seconds: int
    max_attempts: int
    run_once: bool
    allow_fake_executor: bool
    repository_path: str | None
    planning_workspace_path: str | None
    backend_repository_path: str | None
    android_repository_path: str | None
    worktree_root: str | None
    artifact_root: str | None
    repository_scope: str | None
    base_branch_override: str | None
    implementation_validator: str | None
    implementation_validation_timeout_seconds: int
    deployment_target_repository: str | None
    deployment_worktree_root: str | None
    deployment_artifact_root: str | None
    deployment_target_branch: str
    deployment_database_config: str | None
    deployment_database_owner_role: str | None
    deployment_qa_parity_database_config: str | None
    deployment_qa_parity_database_role: str | None
    deployment_backup_root: str | None
    deployment_pg_dump_binary: str | None
    deployment_pg_restore_binary: str | None
    deployment_validator: str | None
    deployment_runtime_helper: str | None
    deployment_github_sync_helper: str | None
    deployment_health_url: str
    deployment_timeout_seconds: int
    deployment_retry_seconds: int
    git_timeout_seconds: int
    codex_bin: str | None
    codex_model: str | None
    codex_retry_seconds: int

    @classmethod
    def from_environment(cls) -> "AgentWorkerSettings":
        environment = (
            os.environ.get(
                "REMIHUB_AGENT_ENVIRONMENT",
                "production",
            )
            .strip()
            .lower()
        )
        if environment not in {"qa", "production"}:
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_ENVIRONMENT must be qa or production"
            )

        executor_name = (
            os.environ.get(
                "REMIHUB_AGENT_EXECUTOR",
                "disabled",
            )
            .strip()
            .lower()
        )
        worker_id = os.environ.get(
            "REMIHUB_AGENT_WORKER_ID",
            f"{socket.gethostname()}:{os.getpid()}",
        ).strip()

        if not worker_id:
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_WORKER_ID must not be blank"
            )
        if len(worker_id) > 200:
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_WORKER_ID must be at most 200 characters"
            )

        lease_seconds = _positive_int(
            "REMIHUB_AGENT_LEASE_SECONDS",
            120,
            minimum=5,
        )
        heartbeat_seconds = _positive_int(
            "REMIHUB_AGENT_HEARTBEAT_SECONDS",
            30,
        )
        if heartbeat_seconds >= lease_seconds:
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_HEARTBEAT_SECONDS must be less than "
                "REMIHUB_AGENT_LEASE_SECONDS"
            )

        repository_path = os.environ.get("REMIHUB_AGENT_REPOSITORY")
        planning_workspace_path = os.environ.get("REMIHUB_AGENT_PLANNING_WORKSPACE")
        backend_repository_path = os.environ.get("REMIHUB_AGENT_BACKEND_REPOSITORY")
        android_repository_path = os.environ.get("REMIHUB_AGENT_ANDROID_REPOSITORY")
        worktree_root = os.environ.get("REMIHUB_AGENT_WORKTREE_ROOT")
        artifact_root = os.environ.get("REMIHUB_AGENT_ARTIFACT_ROOT")
        repository_scope = os.environ.get("REMIHUB_AGENT_REPOSITORY_SCOPE")
        base_branch_override = os.environ.get(
            "REMIHUB_AGENT_BASE_BRANCH_OVERRIDE"
        )
        implementation_validator = os.environ.get(
            "REMIHUB_AGENT_IMPLEMENTATION_VALIDATOR"
        )
        deployment_target_repository = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_TARGET_REPOSITORY"
        )
        deployment_worktree_root = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_WORKTREE_ROOT"
        )
        deployment_artifact_root = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_ARTIFACT_ROOT"
        )
        deployment_target_branch = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_TARGET_BRANCH",
            "qa-main" if environment == "qa" else "production-main",
        ).strip()
        if not deployment_target_branch:
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_DEPLOYMENT_TARGET_BRANCH must not be blank"
            )
        deployment_database_config = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_DATABASE_CONFIG"
        )
        deployment_database_owner_role = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_DATABASE_OWNER_ROLE"
        )
        deployment_qa_parity_database_config = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_QA_PARITY_DATABASE_CONFIG"
        )
        deployment_qa_parity_database_role = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_QA_PARITY_DATABASE_ROLE"
        )
        deployment_backup_root = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_BACKUP_ROOT"
        )
        deployment_pg_dump_binary = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_PG_DUMP_BINARY"
        )
        deployment_pg_restore_binary = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_PG_RESTORE_BINARY"
        )
        deployment_validator = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_VALIDATOR"
        )
        deployment_runtime_helper = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_RUNTIME_HELPER"
        )
        deployment_github_sync_helper = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_GITHUB_SYNC_HELPER"
        )
        deployment_health_url = os.environ.get(
            "REMIHUB_AGENT_DEPLOYMENT_HEALTH_URL",
            "http://127.0.0.1:8001/openapi.json"
            if environment == "qa"
            else "http://127.0.0.1:8000/openapi.json",
        ).strip()
        codex_bin = os.environ.get("REMIHUB_CODEX_BIN")
        codex_model = os.environ.get("REMIHUB_CODEX_MODEL")

        return cls(
            environment=environment,
            executor_name=executor_name,
            worker_id=worker_id,
            poll_seconds=_positive_int("REMIHUB_AGENT_POLL_SECONDS", 5),
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
            max_attempts=_positive_int("REMIHUB_AGENT_MAX_ATTEMPTS", 3),
            run_once=_boolean("REMIHUB_AGENT_RUN_ONCE"),
            allow_fake_executor=_boolean("REMIHUB_AGENT_ALLOW_FAKE_EXECUTOR"),
            repository_path=(
                repository_path.strip()
                if repository_path and repository_path.strip()
                else None
            ),
            planning_workspace_path=(
                planning_workspace_path.strip()
                if planning_workspace_path and planning_workspace_path.strip()
                else None
            ),
            backend_repository_path=(
                backend_repository_path.strip()
                if backend_repository_path and backend_repository_path.strip()
                else None
            ),
            android_repository_path=(
                android_repository_path.strip()
                if android_repository_path and android_repository_path.strip()
                else None
            ),
            worktree_root=(
                worktree_root.strip()
                if worktree_root and worktree_root.strip()
                else None
            ),
            artifact_root=(
                artifact_root.strip()
                if artifact_root and artifact_root.strip()
                else None
            ),
            repository_scope=(
                repository_scope.strip().lower()
                if repository_scope and repository_scope.strip()
                else None
            ),
            base_branch_override=(
                base_branch_override.strip()
                if base_branch_override and base_branch_override.strip()
                else None
            ),
            implementation_validator=(
                implementation_validator.strip()
                if implementation_validator and implementation_validator.strip()
                else None
            ),
            implementation_validation_timeout_seconds=_positive_int(
                "REMIHUB_AGENT_IMPLEMENTATION_VALIDATION_TIMEOUT_SECONDS",
                1200,
            ),
            deployment_target_repository=(
                deployment_target_repository.strip()
                if deployment_target_repository
                and deployment_target_repository.strip()
                else None
            ),
            deployment_worktree_root=(
                deployment_worktree_root.strip()
                if deployment_worktree_root and deployment_worktree_root.strip()
                else None
            ),
            deployment_artifact_root=(
                deployment_artifact_root.strip()
                if deployment_artifact_root and deployment_artifact_root.strip()
                else None
            ),
            deployment_target_branch=deployment_target_branch,
            deployment_database_config=(
                deployment_database_config.strip()
                if deployment_database_config and deployment_database_config.strip()
                else None
            ),
            deployment_database_owner_role=(
                deployment_database_owner_role.strip()
                if deployment_database_owner_role
                and deployment_database_owner_role.strip()
                else None
            ),
            deployment_qa_parity_database_config=(
                deployment_qa_parity_database_config.strip()
                if deployment_qa_parity_database_config
                and deployment_qa_parity_database_config.strip()
                else None
            ),
            deployment_qa_parity_database_role=(
                deployment_qa_parity_database_role.strip()
                if deployment_qa_parity_database_role
                and deployment_qa_parity_database_role.strip()
                else None
            ),
            deployment_backup_root=(
                deployment_backup_root.strip()
                if deployment_backup_root and deployment_backup_root.strip()
                else None
            ),
            deployment_pg_dump_binary=(
                deployment_pg_dump_binary.strip()
                if deployment_pg_dump_binary
                and deployment_pg_dump_binary.strip()
                else None
            ),
            deployment_pg_restore_binary=(
                deployment_pg_restore_binary.strip()
                if deployment_pg_restore_binary
                and deployment_pg_restore_binary.strip()
                else None
            ),
            deployment_validator=(
                deployment_validator.strip()
                if deployment_validator and deployment_validator.strip()
                else None
            ),
            deployment_runtime_helper=(
                deployment_runtime_helper.strip()
                if deployment_runtime_helper and deployment_runtime_helper.strip()
                else None
            ),
            deployment_github_sync_helper=(
                deployment_github_sync_helper.strip()
                if deployment_github_sync_helper
                and deployment_github_sync_helper.strip()
                else None
            ),
            deployment_health_url=deployment_health_url,
            deployment_timeout_seconds=_positive_int(
                "REMIHUB_AGENT_DEPLOYMENT_TIMEOUT_SECONDS",
                900,
            ),
            deployment_retry_seconds=_positive_int(
                "REMIHUB_AGENT_DEPLOYMENT_RETRY_SECONDS",
                60,
            ),
            git_timeout_seconds=_positive_int(
                "REMIHUB_AGENT_GIT_TIMEOUT_SECONDS",
                120,
            ),
            codex_bin=(
                codex_bin.strip()
                if codex_bin and codex_bin.strip()
                else None
            ),
            codex_model=(
                codex_model.strip()
                if codex_model and codex_model.strip()
                else None
            ),
            codex_retry_seconds=_positive_int(
                "REMIHUB_CODEX_RETRY_SECONDS",
                900,
            ),
        )


def build_executor(
    settings: AgentWorkerSettings,
    *,
    queue: DatabaseAgentQueue | None = None,
):
    if settings.executor_name == "disabled":
        raise AgentWorkerConfigurationError(
            "Agent execution is disabled; configure REMIHUB_AGENT_EXECUTOR"
        )

    if settings.executor_name == "fake":
        if settings.environment != "qa":
            raise AgentWorkerConfigurationError(
                "The fake agent executor is restricted to QA"
            )
        if not settings.allow_fake_executor:
            raise AgentWorkerConfigurationError(
                "The fake executor requires REMIHUB_AGENT_ALLOW_FAKE_EXECUTOR=true"
            )
        return FakeAgentExecutor()

    if settings.executor_name == "codex-planning":
        if queue is None:
            raise AgentWorkerConfigurationError(
                "The codex planning executor requires an agent queue"
            )
        if (
            settings.repository_path is None
            and settings.planning_workspace_path is None
        ):
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_REPOSITORY or REMIHUB_AGENT_PLANNING_WORKSPACE "
                "is required for codex-planning"
            )
        return CodexPlanningExecutor(
            repository_path=settings.repository_path,
            planning_workspace_path=settings.planning_workspace_path,
            backend_repository_path=settings.backend_repository_path,
            android_repository_path=settings.android_repository_path,
            thread_store=queue,
            model=settings.codex_model,
            retry_after_seconds=settings.codex_retry_seconds,
        )

    if settings.executor_name == "codex-implementation":
        if queue is None:
            raise AgentWorkerConfigurationError(
                "The codex implementation executor requires an agent queue"
            )
        if settings.repository_path is None:
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_REPOSITORY is required for codex-implementation"
            )
        if settings.worktree_root is None:
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_WORKTREE_ROOT is required for "
                "codex-implementation"
            )
        if settings.artifact_root is None:
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_ARTIFACT_ROOT is required for "
                "codex-implementation"
            )
        if settings.codex_bin is None:
            raise AgentWorkerConfigurationError(
                "REMIHUB_CODEX_BIN is required for codex-implementation; "
                "configure the approved outer sandbox wrapper"
            )

        configured_scope = settings.repository_scope or RepositoryScope.BACKEND.value
        try:
            implementation_scope = RepositoryScope(configured_scope)
        except ValueError as exc:
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_REPOSITORY_SCOPE must be backend or android "
                "for codex-implementation"
            ) from exc
        if implementation_scope not in {
            RepositoryScope.BACKEND,
            RepositoryScope.ANDROID,
        }:
            raise AgentWorkerConfigurationError(
                "codex-implementation supports only backend or android scope"
            )
        workspace_arguments = {
            "source_repository": settings.repository_path,
            "worktree_root": settings.worktree_root,
            "artifact_root": settings.artifact_root,
            "command_timeout_seconds": settings.git_timeout_seconds,
        }
        if settings.base_branch_override is not None:
            workspace_arguments["base_branch_override"] = (
                settings.base_branch_override
            )
        workspace_manager = GitImplementationWorkspaceManager(
            **workspace_arguments
        )
        validator = (
            CommandImplementationValidator(
                command=settings.implementation_validator,
                timeout_seconds=(
                    settings.implementation_validation_timeout_seconds
                ),
            )
            if settings.implementation_validator is not None
            else None
        )
        if implementation_scope is RepositoryScope.ANDROID and validator is None:
            raise AgentWorkerConfigurationError(
                "Android implementation requires "
                "REMIHUB_AGENT_IMPLEMENTATION_VALIDATOR"
            )
        executor_arguments = {
            "workspace_manager": workspace_manager,
            "workspace_store": queue,
            "codex_bin": settings.codex_bin,
            "model": settings.codex_model,
            "retry_after_seconds": settings.codex_retry_seconds,
        }
        if implementation_scope is not RepositoryScope.BACKEND:
            executor_arguments["repository_scope"] = implementation_scope
        if validator is not None:
            executor_arguments["validator"] = validator
        return CodexImplementationExecutor(**executor_arguments)

    if settings.executor_name == "git-android-deployment":
        if settings.environment != "production":
            raise AgentWorkerConfigurationError(
                "git-android-deployment is restricted to production"
            )
        required_paths = {
            "REMIHUB_AGENT_REPOSITORY": settings.repository_path,
            "REMIHUB_AGENT_WORKTREE_ROOT": settings.worktree_root,
            "REMIHUB_AGENT_ARTIFACT_ROOT": settings.artifact_root,
            "REMIHUB_AGENT_DEPLOYMENT_TARGET_REPOSITORY": (
                settings.deployment_target_repository
            ),
            "REMIHUB_AGENT_DEPLOYMENT_WORKTREE_ROOT": (
                settings.deployment_worktree_root
            ),
            "REMIHUB_AGENT_DEPLOYMENT_ARTIFACT_ROOT": (
                settings.deployment_artifact_root
            ),
            "REMIHUB_AGENT_DEPLOYMENT_VALIDATOR": settings.deployment_validator,
            "REMIHUB_AGENT_DEPLOYMENT_RUNTIME_HELPER": (
                settings.deployment_runtime_helper
            ),
        }
        missing = [name for name, value in required_paths.items() if value is None]
        if missing:
            raise AgentWorkerConfigurationError(
                f"{missing[0]} is required for git-android-deployment"
            )
        validator = CommandAndroidReleaseValidator(
            validation_command=settings.deployment_validator,
            timeout_seconds=settings.deployment_timeout_seconds,
        )
        runtime = PrivilegedAndroidReleaseRuntime(
            helper_path=settings.deployment_runtime_helper,
            command_timeout_seconds=settings.deployment_timeout_seconds,
        )
        deployment_manager = GitAndroidDeploymentManager(
            source_repository=settings.repository_path,
            source_worktree_root=settings.worktree_root,
            source_artifact_root=settings.artifact_root,
            target_repository=settings.deployment_target_repository,
            candidate_worktree_root=settings.deployment_worktree_root,
            deployment_artifact_root=settings.deployment_artifact_root,
            target_branch=settings.deployment_target_branch,
            validator=validator,
            runtime=runtime,
            command_timeout_seconds=settings.git_timeout_seconds,
        )
        return GitAndroidDeploymentExecutor(
            deployment_manager=deployment_manager,
            retry_after_seconds=settings.deployment_retry_seconds,
        )

    if settings.executor_name in {"git-backend-deployment", "git-deployment-qa"}:
        if settings.executor_name == "git-deployment-qa" and settings.environment != "qa":
            raise AgentWorkerConfigurationError(
                "The legacy git-deployment-qa executor is restricted to QA"
            )
        required_paths = {
            "REMIHUB_AGENT_REPOSITORY": settings.repository_path,
            "REMIHUB_AGENT_WORKTREE_ROOT": settings.worktree_root,
            "REMIHUB_AGENT_ARTIFACT_ROOT": settings.artifact_root,
            "REMIHUB_AGENT_DEPLOYMENT_TARGET_REPOSITORY": (
                settings.deployment_target_repository
            ),
            "REMIHUB_AGENT_DEPLOYMENT_WORKTREE_ROOT": (
                settings.deployment_worktree_root
            ),
            "REMIHUB_AGENT_DEPLOYMENT_ARTIFACT_ROOT": (
                settings.deployment_artifact_root
            ),
            "REMIHUB_AGENT_DEPLOYMENT_DATABASE_CONFIG": (
                settings.deployment_database_config
            ),
            "REMIHUB_AGENT_DEPLOYMENT_DATABASE_OWNER_ROLE": (
                settings.deployment_database_owner_role
            ),
            "REMIHUB_AGENT_DEPLOYMENT_BACKUP_ROOT": (
                settings.deployment_backup_root
            ),
            "REMIHUB_AGENT_DEPLOYMENT_PG_DUMP_BINARY": (
                settings.deployment_pg_dump_binary
            ),
            "REMIHUB_AGENT_DEPLOYMENT_PG_RESTORE_BINARY": (
                settings.deployment_pg_restore_binary
            ),
            "REMIHUB_AGENT_DEPLOYMENT_VALIDATOR": settings.deployment_validator,
            "REMIHUB_AGENT_DEPLOYMENT_RUNTIME_HELPER": (
                settings.deployment_runtime_helper
            ),
        }
        if settings.environment == "production":
            required_paths["REMIHUB_AGENT_DEPLOYMENT_GITHUB_SYNC_HELPER"] = (
                settings.deployment_github_sync_helper
            )
            required_paths["REMIHUB_AGENT_DEPLOYMENT_QA_PARITY_DATABASE_CONFIG"] = (
                settings.deployment_qa_parity_database_config
            )
        missing = [name for name, value in required_paths.items() if value is None]
        if missing:
            raise AgentWorkerConfigurationError(
                f"{missing[0]} is required for git-backend-deployment"
            )
        validator = SandboxBackendValidator(
            validation_command=settings.deployment_validator,
            timeout_seconds=settings.deployment_timeout_seconds,
        )
        database = PostgresDeploymentDatabase(
            config_path=settings.deployment_database_config,
            backup_root=settings.deployment_backup_root,
            owner_role=settings.deployment_database_owner_role,
            pg_dump_binary=settings.deployment_pg_dump_binary,
            pg_restore_binary=settings.deployment_pg_restore_binary,
            command_timeout_seconds=settings.deployment_timeout_seconds,
        )
        qa_history_reader = None
        if settings.environment == "production":
            qa_history_reader = PostgresMigrationHistoryReader(
                config_path=settings.deployment_qa_parity_database_config,
                role=settings.deployment_qa_parity_database_role,
            )
            verify_distinct_database_identities(database, qa_history_reader)
        runtime = PrivilegedDeploymentRuntime(
            environment=settings.environment,
            helper_path=settings.deployment_runtime_helper,
            health_url=settings.deployment_health_url,
            command_timeout_seconds=settings.deployment_timeout_seconds,
        )
        deployment_manager = GitBackendDeploymentManager(
            environment=settings.environment,
            source_repository=settings.repository_path,
            source_worktree_root=settings.worktree_root,
            source_artifact_root=settings.artifact_root,
            target_repository=settings.deployment_target_repository,
            candidate_worktree_root=settings.deployment_worktree_root,
            deployment_artifact_root=settings.deployment_artifact_root,
            target_branch=settings.deployment_target_branch,
            validator=validator,
            database=database,
            runtime=runtime,
            qa_history_reader=qa_history_reader,
            command_timeout_seconds=settings.git_timeout_seconds,
        )
        github_synchronizer = None
        if settings.environment == "production":
            github_synchronizer = PrivilegedBackendGitHubSynchronizer(
                helper_path=settings.deployment_github_sync_helper,
                command_timeout_seconds=settings.deployment_timeout_seconds,
            )
        return GitBackendDeploymentExecutor(
            deployment_manager=deployment_manager,
            github_synchronizer=github_synchronizer,
            retry_after_seconds=settings.deployment_retry_seconds,
        )

    raise AgentWorkerConfigurationError(
        f"Unknown agent executor: {settings.executor_name!r}"
    )


def run_worker(settings: AgentWorkerSettings) -> None:
    queue = DatabaseAgentQueue(environment=settings.environment)
    executor = build_executor(settings, queue=queue)
    identity = queue.verify_identity()
    logger.info(
        "Agent worker database identity verified: database=%s role=%s",
        identity[0],
        identity[1],
    )
    worker = AgentWorker(
        queue=queue,
        executor=executor,
        worker_id=settings.worker_id,
        lease_seconds=settings.lease_seconds,
        heartbeat_seconds=settings.heartbeat_seconds,
        max_attempts=settings.max_attempts,
    )

    if settings.run_once:
        processed = worker.process_once()
        logger.info("Agent worker run-once complete: processed=%s", processed)
        return

    stop_event = threading.Event()

    def request_stop(signum, _frame):
        logger.info("Agent worker received signal %s", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    logger.info(
        "Agent worker started: worker=%s executor=%s environment=%s",
        settings.worker_id,
        settings.executor_name,
        settings.environment,
    )

    while not stop_event.is_set():
        processed = worker.process_once()
        if not processed:
            stop_event.wait(settings.poll_seconds)

    logger.info("Agent worker stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = AgentWorkerSettings.from_environment()
    run_worker(settings)


if __name__ == "__main__":
    main()

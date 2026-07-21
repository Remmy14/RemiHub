from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Protocol, Sequence

from backend.config import load_config
from backend.core.agent_state import (
    CardStatus,
    RepositoryScope,
    RunPhase,
    require_backend_repository_scope,
)
from backend.core.agent_worker import (
    AgentTemporarilyBlockedError,
    AgentWorkerConfigurationError,
    ClaimedRun,
    DeploymentSource,
    ExecutionResult,
)
from backend.database import migration_runner


class AgentDeploymentError(RuntimeError):
    pass


class DeploymentValidationError(AgentDeploymentError):
    pass


class DeploymentRollbackError(AgentDeploymentError):
    pass


class DeploymentRolledBackError(AgentDeploymentError):
    """A deployment failed, but code/database state was restored safely."""


@dataclass(frozen=True)
class ApprovedImplementation:
    approval_id: str
    implementation_run_id: str
    base_branch: str
    base_commit: str
    feature_branch: str
    worktree_path: str
    head_commit: str
    changed_files: tuple[str, ...]
    status_porcelain: str
    patch_path: str
    patch_size_bytes: int
    patch_sha256: str
    expected_tree: str
    implementation_tests: tuple[dict, ...]


@dataclass(frozen=True)
class MigrationPlan:
    versions: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationEvidence:
    command: str
    duration_ms: int
    stdout_sha256: str
    stdout_tail: str
    stderr_tail: str


@dataclass(frozen=True)
class BackupEvidence:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class RuntimeHealth:
    service_active: bool
    url: str
    status_code: int
    response_sha256: str


@dataclass(frozen=True)
class DeploymentCandidate:
    approval_id: str
    implementation_run_id: str
    environment: str
    candidate_branch: str
    candidate_commit: str
    target_branch: str
    target_before: str
    target_after: str
    base_commit: str
    changed_files: tuple[str, ...]
    patch_sha256: str
    patch_size_bytes: int
    manifest_path: str
    rollback_ref: str
    validation: dict
    service_restart_performed: bool = False
    migrations_applied: tuple[str, ...] = ()
    database_backup: dict | None = None
    rollback_performed: bool = False


class BackendValidator(Protocol):
    def validate(self, candidate_worktree: Path) -> ValidationEvidence: ...


class DeploymentDatabase(Protocol):
    def pending_versions(self, migrations_dir: Path) -> tuple[str, ...]: ...

    def backup(self, *, card_id: str, deployment_run_id: str) -> BackupEvidence: ...

    def upgrade(
        self,
        migrations_dir: Path,
        expected_versions: tuple[str, ...],
    ) -> tuple[str, ...]: ...

    def downgrade(
        self,
        migrations_dir: Path,
        versions: tuple[str, ...],
    ) -> tuple[str, ...]: ...


class DeploymentRuntime(Protocol):
    def stop(self) -> None: ...

    def start(self) -> None: ...

    def promote(
        self,
        *,
        candidate_branch: str,
        candidate_commit: str,
        expected_before: str,
        rollback_ref: str,
    ) -> None: ...

    def restore(self, *, expected_current: str, rollback_commit: str) -> None: ...

    def synchronize_sources(
        self,
        *,
        candidate_branch: str,
        candidate_commit: str,
        expected_before: str,
        rollback_ref: str,
    ) -> None: ...

    def restore_sources(self, *, expected_current: str, rollback_commit: str) -> None: ...

    def verify(self) -> RuntimeHealth: ...


class SandboxBackendValidator:
    def __init__(
        self,
        *,
        validation_command: str | Path,
        timeout_seconds: int = 900,
    ):
        command = Path(validation_command).expanduser()
        if not command.is_absolute():
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_DEPLOYMENT_VALIDATOR must be an absolute path"
            )
        if not command.is_file() or not os.access(command, os.X_OK):
            raise AgentWorkerConfigurationError(
                f"Deployment validator is not executable: {command}"
            )
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")
        self.validation_command = command.resolve()
        self.timeout_seconds = timeout_seconds

    def validate(self, candidate_worktree: Path) -> ValidationEvidence:
        started = time.monotonic()
        try:
            result = subprocess.run(
                [str(self.validation_command), str(candidate_worktree)],
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env={
                    "HOME": "/nonexistent",
                    "LANG": os.environ.get("LANG", "C.UTF-8"),
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                },
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentValidationError(
                "Unable to execute the isolated backend validator"
            ) from exc
        duration_ms = round((time.monotonic() - started) * 1000)
        stdout = result.stdout
        stderr = result.stderr
        evidence = ValidationEvidence(
            command=f"{self.validation_command} {candidate_worktree}",
            duration_ms=duration_ms,
            stdout_sha256=hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            stdout_tail=_tail(stdout, 8000),
            stderr_tail=_tail(stderr, 8000),
        )
        if result.returncode != 0:
            detail = evidence.stderr_tail or evidence.stdout_tail
            suffix = f": {detail}" if detail else ""
            raise DeploymentValidationError(
                f"Backend compile/test validation failed{suffix}"
            )
        return evidence


class PostgresDeploymentDatabase:
    def __init__(
        self,
        *,
        config_path: str | Path,
        backup_root: str | Path,
        owner_role: str,
        pg_dump_binary: str = "/usr/bin/pg_dump",
        pg_restore_binary: str = "/usr/bin/pg_restore",
        command_timeout_seconds: int = 900,
    ):
        self.config_path = _required_absolute_file(
            config_path,
            field="REMIHUB_AGENT_DEPLOYMENT_DATABASE_CONFIG",
        )
        self.backup_root = _required_absolute_directory(
            backup_root,
            field="REMIHUB_AGENT_DEPLOYMENT_BACKUP_ROOT",
        )
        normalized_role = owner_role.strip()
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", normalized_role):
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_DEPLOYMENT_DATABASE_OWNER_ROLE is invalid"
            )
        self.owner_role = normalized_role
        self.pg_dump_binary = _required_executable(
            pg_dump_binary,
            field="pg_dump",
        )
        self.pg_restore_binary = _required_executable(
            pg_restore_binary,
            field="pg_restore",
        )
        if command_timeout_seconds < 1:
            raise ValueError("command_timeout_seconds must be at least 1")
        self.command_timeout_seconds = command_timeout_seconds

    def pending_versions(self, migrations_dir: Path) -> tuple[str, ...]:
        migrations = migration_runner.discover_migrations(migrations_dir)
        conn = self._connect()
        try:
            migration_runner._ensure_migration_table(conn)
            migration_runner._acquire_lock(conn)
            applied = migration_runner._applied_migrations(conn)
            migration_runner._validate_applied_checksums(migrations, applied)
            return tuple(
                migration.version
                for migration in migrations
                if migration.version not in applied
            )
        finally:
            try:
                migration_runner._release_lock(conn)
            finally:
                conn.close()

    def backup(self, *, card_id: str, deployment_run_id: str) -> BackupEvidence:
        card_root = self.backup_root / card_id
        card_root.mkdir(mode=0o750, exist_ok=True)
        if card_root.is_symlink():
            raise AgentDeploymentError("Database backup directory must not be a symlink")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = card_root / f"{deployment_run_id}-{timestamp}.dump"
        if backup_path.exists() or backup_path.is_symlink():
            raise AgentDeploymentError("Database backup path already exists")

        database = load_config(self.config_path)["Database"]
        environment = {
            "HOME": "/nonexistent",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": "/usr/bin:/bin",
            "PGPASSWORD": database["password"],
        }
        command = [
            str(self.pg_dump_binary),
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--host",
            database["host"],
            "--port",
            str(database["port"]),
            "--username",
            database["user"],
            "--role",
            self.owner_role,
            "--file",
            str(backup_path),
            database["database"],
        ]
        self._run(command, environment=environment, context="PostgreSQL backup failed")
        os.chmod(backup_path, 0o600)
        self._run(
            [str(self.pg_restore_binary), "--list", str(backup_path)],
            environment={
                "HOME": "/nonexistent",
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "PATH": "/usr/bin:/bin",
            },
            context="PostgreSQL backup verification failed",
        )
        return BackupEvidence(
            path=str(backup_path),
            size_bytes=backup_path.stat().st_size,
            sha256=_sha256_file(backup_path),
        )

    def upgrade(
        self,
        migrations_dir: Path,
        expected_versions: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not expected_versions:
            return ()
        migrations = migration_runner.discover_migrations(migrations_dir)
        conn = self._connect()
        try:
            migration_runner._ensure_migration_table(conn)
            migration_runner._acquire_lock(conn)
            before = migration_runner._applied_migrations(conn)
            migration_runner._validate_applied_checksums(migrations, before)
            pending = tuple(
                migration.version
                for migration in migrations
                if migration.version not in before
            )
            if pending != expected_versions:
                raise AgentDeploymentError(
                    "Database pending migrations do not match the approved candidate: "
                    f"expected {expected_versions!r}, found {pending!r}"
                )
            migration_runner.upgrade(conn, migrations)
            after = migration_runner._applied_migrations(conn)
            applied = tuple(version for version in after if version not in before)
            if applied != expected_versions:
                raise AgentDeploymentError(
                    "Applied migrations do not match the approved candidate"
                )
            return applied
        finally:
            try:
                migration_runner._release_lock(conn)
            finally:
                conn.close()

    def downgrade(
        self,
        migrations_dir: Path,
        versions: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not versions:
            return ()
        migrations = migration_runner.discover_migrations(migrations_dir)
        conn = self._connect()
        try:
            migration_runner._ensure_migration_table(conn)
            migration_runner._acquire_lock(conn)
            applied = migration_runner._applied_migrations(conn)
            migration_runner._validate_applied_checksums(migrations, applied)
            current_tail = tuple(sorted(applied, reverse=True)[: len(versions)])
            expected_tail = tuple(reversed(versions))
            if current_tail != expected_tail:
                raise DeploymentRollbackError(
                    "Database migration tail changed before rollback: "
                    f"expected {expected_tail!r}, found {current_tail!r}"
                )
            migration_runner.downgrade(conn, migrations, len(versions))
            remaining = migration_runner._applied_migrations(conn)
            still_applied = tuple(version for version in versions if version in remaining)
            if still_applied:
                raise DeploymentRollbackError(
                    f"Migration rollback did not remove {still_applied!r}"
                )
            return expected_tail
        finally:
            try:
                migration_runner._release_lock(conn)
            finally:
                conn.close()

    def _connect(self):
        conn = migration_runner._connect(self.config_path)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET ROLE {self.owner_role}")
            return conn
        except Exception:
            conn.close()
            raise

    def _run(
        self,
        command: list[str],
        *,
        environment: dict[str, str],
        context: str,
    ) -> None:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=self.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentDeploymentError(context) from exc
        if result.returncode != 0:
            detail = _tail(result.stderr, 2000)
            suffix = f": {detail}" if detail else ""
            raise AgentDeploymentError(f"{context}{suffix}")


class PrivilegedDeploymentRuntime:
    def __init__(
        self,
        *,
        environment: str,
        helper_path: str | Path,
        health_url: str,
        sudo_binary: str | Path = "/usr/bin/sudo",
        command_timeout_seconds: int = 180,
        health_attempts: int = 10,
        health_delay_seconds: float = 1.0,
    ):
        normalized_environment = environment.strip().lower()
        if normalized_environment not in {"qa", "production"}:
            raise AgentWorkerConfigurationError("environment must be qa or production")
        self.environment = normalized_environment
        self.helper_path = _required_executable(
            helper_path,
            field="REMIHUB_AGENT_DEPLOYMENT_RUNTIME_HELPER",
        )
        self.sudo_binary = _required_executable(sudo_binary, field="sudo")
        normalized_url = health_url.strip()
        if not re.fullmatch(r"http://127\.0\.0\.1:\d+/openapi\.json", normalized_url):
            raise AgentWorkerConfigurationError(
                "Deployment health URL must be a loopback /openapi.json URL"
            )
        self.health_url = normalized_url
        if command_timeout_seconds < 1:
            raise ValueError("command_timeout_seconds must be at least 1")
        if health_attempts < 1:
            raise ValueError("health_attempts must be at least 1")
        self.command_timeout_seconds = command_timeout_seconds
        self.health_attempts = health_attempts
        self.health_delay_seconds = health_delay_seconds

    def stop(self) -> None:
        self._helper("stop")

    def start(self) -> None:
        self._helper("start")

    def promote(
        self,
        *,
        candidate_branch: str,
        candidate_commit: str,
        expected_before: str,
        rollback_ref: str,
    ) -> None:
        self._helper(
            "promote",
            candidate_branch,
            candidate_commit,
            expected_before,
            rollback_ref,
        )

    def restore(self, *, expected_current: str, rollback_commit: str) -> None:
        self._helper("restore", expected_current, rollback_commit)

    def synchronize_sources(
        self,
        *,
        candidate_branch: str,
        candidate_commit: str,
        expected_before: str,
        rollback_ref: str,
    ) -> None:
        if self.environment != "production":
            return
        self._helper(
            "synchronize-sources",
            candidate_branch,
            candidate_commit,
            expected_before,
            rollback_ref,
        )

    def restore_sources(self, *, expected_current: str, rollback_commit: str) -> None:
        if self.environment != "production":
            return
        self._helper("restore-sources", expected_current, rollback_commit)

    def verify(self) -> RuntimeHealth:
        status_output = self._helper("status").strip()
        if status_output != "active":
            raise AgentDeploymentError(
                f"Deployment service is not active: {status_output or 'unknown'}"
            )
        last_error: Exception | None = None
        for attempt in range(self.health_attempts):
            try:
                request = urllib.request.Request(
                    self.health_url,
                    headers={"Accept": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = response.read(5_000_000)
                    status_code = int(response.status)
                payload = json.loads(body.decode("utf-8"))
                if status_code != 200 or not isinstance(payload, dict):
                    raise AgentDeploymentError(
                        "Deployment /openapi.json response is invalid"
                    )
                if "openapi" not in payload or "paths" not in payload:
                    raise AgentDeploymentError(
                        "Deployment /openapi.json is missing OpenAPI fields"
                    )
                return RuntimeHealth(
                    service_active=True,
                    url=self.health_url,
                    status_code=status_code,
                    response_sha256=hashlib.sha256(body).hexdigest(),
                )
            except (
                AgentDeploymentError,
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                urllib.error.URLError,
            ) as exc:
                last_error = exc
                if attempt + 1 < self.health_attempts:
                    time.sleep(self.health_delay_seconds)
        raise AgentDeploymentError(
            f"Deployment health verification failed: {last_error}"
        )

    def _helper(self, action: str, *arguments: str) -> str:
        command = [
            str(self.sudo_binary),
            "-n",
            str(self.helper_path),
            action,
            self.environment,
            *arguments,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env={
                    "HOME": "/nonexistent",
                    "LANG": os.environ.get("LANG", "C.UTF-8"),
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                },
                timeout=self.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentDeploymentError(
                f"Deployment runtime helper failed during {action}"
            ) from exc
        if result.returncode != 0:
            detail = _tail(result.stderr, 3000) or _tail(result.stdout, 3000)
            suffix = f": {detail}" if detail else ""
            raise AgentDeploymentError(
                f"Deployment runtime helper failed during {action}{suffix}"
            )
        return result.stdout


class GitBackendDeploymentManager:
    """Validate, test, migrate, promote, verify, and roll back one backend candidate."""

    MAX_PATCH_BYTES = 5_000_000
    MAX_CHANGED_FILES = 100
    PROTECTED_BACKEND_PATHS = frozenset(
        {
            "backend/agent_worker.py",
            "backend/config.py",
            "backend/core/agent_deployment.py",
            "backend/core/agent_worker.py",
            "backend/core/agent_workspace.py",
            "backend/core/codex_implementation.py",
            "backend/core/codex_planning.py",
            "backend/core/auth.py",
            "backend/core/firebase_auth.py",
            "backend/database/database.py",
            "backend/database/migration_runner.py",
            "backend/routers/auth.py",
            "backend/services/agent_worker_service.py",
            "backend/services/auth_service.py",
        }
    )

    def __init__(
        self,
        *,
        environment: str,
        source_repository: str | Path,
        source_worktree_root: str | Path,
        source_artifact_root: str | Path,
        target_repository: str | Path,
        candidate_worktree_root: str | Path,
        deployment_artifact_root: str | Path,
        target_branch: str,
        validator: BackendValidator,
        database: DeploymentDatabase,
        runtime: DeploymentRuntime,
        git_binary: str = "git",
        command_timeout_seconds: int = 120,
    ):
        normalized_environment = environment.strip().lower()
        if normalized_environment not in {"qa", "production"}:
            raise AgentWorkerConfigurationError("environment must be qa or production")
        self.environment = normalized_environment
        self.git_binary = git_binary.strip()
        if not self.git_binary:
            raise AgentWorkerConfigurationError("git_binary must not be blank")
        if command_timeout_seconds < 1:
            raise ValueError("command_timeout_seconds must be at least 1")
        self.command_timeout_seconds = command_timeout_seconds
        self.validator = validator
        self.database = database
        self.runtime = runtime

        self.source_repository = _required_absolute_directory(
            source_repository,
            field="REMIHUB_AGENT_REPOSITORY",
        )
        self.source_worktree_root = _required_absolute_directory(
            source_worktree_root,
            field="REMIHUB_AGENT_WORKTREE_ROOT",
        )
        self.source_artifact_root = _required_absolute_directory(
            source_artifact_root,
            field="REMIHUB_AGENT_ARTIFACT_ROOT",
        )
        self.target_repository = _required_absolute_directory(
            target_repository,
            field="REMIHUB_AGENT_DEPLOYMENT_TARGET_REPOSITORY",
        )
        self.candidate_worktree_root = _required_absolute_directory(
            candidate_worktree_root,
            field="REMIHUB_AGENT_DEPLOYMENT_WORKTREE_ROOT",
        )
        self.deployment_artifact_root = _required_absolute_directory(
            deployment_artifact_root,
            field="REMIHUB_AGENT_DEPLOYMENT_ARTIFACT_ROOT",
        )
        self.target_branch = self._validate_branch_name(
            self.target_repository,
            target_branch,
            field="deployment target branch",
        )
        expected_target_branch = (
            "qa-main" if self.environment == "qa" else "production-main"
        )
        if self.target_branch != expected_target_branch:
            raise AgentWorkerConfigurationError(
                f"{self.environment} deployment target branch must be "
                f"{expected_target_branch}"
            )

        self._run_git(
            self.source_repository,
            "rev-parse",
            "--git-dir",
            error_context="The implementation source is not a Git repository",
        )
        self._run_git(
            self.target_repository,
            "rev-parse",
            "--git-dir",
            error_context="The deployment target is not a Git repository",
        )
        self.source_common_directory = self._common_git_directory(
            self.source_repository
        )
        self.target_common_directory = self._common_git_directory(
            self.target_repository
        )
        if self.source_common_directory == self.target_common_directory:
            raise AgentWorkerConfigurationError(
                "Deployment target must be separate from implementation source"
            )
        if self._run_git(
            self.target_repository,
            "rev-parse",
            "--is-bare-repository",
            error_context="Unable to inspect the deployment target",
        ).stdout.strip() != "true":
            raise AgentWorkerConfigurationError(
                "Deployment target must be a bare Git repository"
            )
        if self._run_git(
            self.target_repository,
            "remote",
            error_context="Unable to inspect deployment target remotes",
        ).stdout.strip():
            raise AgentWorkerConfigurationError(
                "Deployment target must not have Git remotes"
            )
        self._resolve_commit(self.target_repository, self.target_branch)

        self.lock_root = self.candidate_worktree_root / ".locks"
        self.lock_root.mkdir(mode=0o750, exist_ok=True)
        if self.lock_root.is_symlink():
            raise AgentWorkerConfigurationError(
                "Deployment worktree lock directory must not be a symlink"
            )

    def deploy(self, claim: ClaimedRun) -> DeploymentCandidate:
        if claim.phase is not RunPhase.DEPLOYMENT:
            raise AgentDeploymentError(
                "The backend deployment manager accepts only deployment runs"
            )
        if claim.deployment_source is None:
            raise AgentDeploymentError(
                "Deployment requires an approved implementation result"
            )

        lock_path = self.lock_root / f"{claim.card_id}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            os.chmod(lock_path, 0o640)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                approved = self._validate_approved_implementation(
                    claim,
                    claim.deployment_source,
                )
                candidate_branch, candidate_commit, candidate_path = (
                    self._materialize_candidate(claim, approved)
                )
                migration_plan = self._migration_plan(
                    approved.base_commit,
                    candidate_commit,
                    candidate_path,
                )
                rollback_ref = self._rollback_ref(claim)
                manifest_path = self._manifest_path(claim)
                manifest = self._load_or_initialize_manifest(
                    claim,
                    approved,
                    candidate_branch=candidate_branch,
                    candidate_commit=candidate_commit,
                    rollback_ref=rollback_ref,
                    migration_plan=migration_plan,
                )

                prior_success = self._successful_attempt(manifest)
                current_target = self._resolve_commit(
                    self.target_repository,
                    self.target_branch,
                )
                if prior_success is not None and current_target == candidate_commit:
                    health = self.runtime.verify()
                    return self._candidate_from_success(
                        approved,
                        candidate_branch=candidate_branch,
                        candidate_commit=candidate_commit,
                        rollback_ref=rollback_ref,
                        manifest_path=manifest_path,
                        attempt=prior_success,
                        health=health,
                    )

                if current_target != approved.base_commit:
                    raise AgentDeploymentError(
                        "Deployment target advanced before candidate execution"
                    )

                attempt = self._begin_attempt(manifest, claim)
                self._write_manifest(manifest_path, manifest)
                return self._execute_candidate(
                    claim,
                    approved,
                    candidate_branch=candidate_branch,
                    candidate_commit=candidate_commit,
                    candidate_path=candidate_path,
                    migration_plan=migration_plan,
                    rollback_ref=rollback_ref,
                    manifest_path=manifest_path,
                    manifest=manifest,
                    attempt=attempt,
                )
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _execute_candidate(
        self,
        claim: ClaimedRun,
        approved: ApprovedImplementation,
        *,
        candidate_branch: str,
        candidate_commit: str,
        candidate_path: Path,
        migration_plan: MigrationPlan,
        rollback_ref: str,
        manifest_path: Path,
        manifest: dict,
        attempt: dict,
    ) -> DeploymentCandidate:
        validation: ValidationEvidence | None = None
        backup: BackupEvidence | None = None
        migrations_applied: tuple[str, ...] = ()
        runtime_promotion_attempted = False
        runtime_promoted = False
        target_update_attempted = False
        target_updated = False
        source_sync_attempted = False
        sources_synchronized = False
        service_stopped = False

        try:
            validation = self.validator.validate(candidate_path)
            attempt["validation"] = asdict(validation)
            attempt["stage"] = "validated"
            self._write_manifest(manifest_path, manifest)

            migrations_dir = candidate_path / "backend" / "database" / "migrations"
            pending = self.database.pending_versions(migrations_dir)
            if pending != migration_plan.versions:
                raise AgentDeploymentError(
                    "Database pending migrations do not match the approved candidate: "
                    f"expected {migration_plan.versions!r}, found {pending!r}"
                )

            self._ensure_rollback_ref(rollback_ref, approved.base_commit)
            attempt["stage"] = "rollback_reference_created"
            self._write_manifest(manifest_path, manifest)

            if migration_plan.versions:
                backup = self.database.backup(
                    card_id=claim.card_id,
                    deployment_run_id=claim.id,
                )
                attempt["database_backup"] = asdict(backup)
                attempt["stage"] = "database_backed_up"
                self._write_manifest(manifest_path, manifest)

            self.runtime.stop()
            service_stopped = True
            attempt["stage"] = "service_stopped"
            self._write_manifest(manifest_path, manifest)

            if migration_plan.versions:
                try:
                    migrations_applied = self.database.upgrade(
                        migrations_dir,
                        migration_plan.versions,
                    )
                except Exception:
                    pending_after_failure = self.database.pending_versions(
                        migrations_dir
                    )
                    migrations_applied = tuple(
                        version
                        for version in migration_plan.versions
                        if version not in pending_after_failure
                    )
                    attempt["migrations_applied"] = list(migrations_applied)
                    attempt["stage"] = "migration_failed"
                    self._write_manifest(manifest_path, manifest)
                    raise
                attempt["migrations_applied"] = list(migrations_applied)
                attempt["stage"] = "migrations_applied"
                self._write_manifest(manifest_path, manifest)

            runtime_promotion_attempted = True
            self.runtime.promote(
                candidate_branch=candidate_branch,
                candidate_commit=candidate_commit,
                expected_before=approved.base_commit,
                rollback_ref=rollback_ref,
            )
            runtime_promoted = True
            attempt["stage"] = "runtime_promoted"
            self._write_manifest(manifest_path, manifest)

            self.runtime.start()
            service_stopped = False
            health = self.runtime.verify()
            attempt["health"] = asdict(health)
            attempt["stage"] = "health_verified"
            self._write_manifest(manifest_path, manifest)

            target_update_attempted = True
            self._run_git(
                self.target_repository,
                "update-ref",
                f"refs/heads/{self.target_branch}",
                candidate_commit,
                approved.base_commit,
                error_context="Unable to advance the deployment target",
            )
            target_updated = True
            attempt["stage"] = "target_advanced"
            self._write_manifest(manifest_path, manifest)

            source_sync_attempted = self.environment == "production"
            self.runtime.synchronize_sources(
                candidate_branch=candidate_branch,
                candidate_commit=candidate_commit,
                expected_before=approved.base_commit,
                rollback_ref=rollback_ref,
            )
            sources_synchronized = self.environment == "production"
            attempt["stage"] = "sources_synchronized"
            attempt["status"] = "succeeded"
            attempt["finished_at"] = _utc_now()
            self._write_manifest(manifest_path, manifest)

            return DeploymentCandidate(
                approval_id=approved.approval_id,
                implementation_run_id=approved.implementation_run_id,
                environment=self.environment,
                candidate_branch=candidate_branch,
                candidate_commit=candidate_commit,
                target_branch=self.target_branch,
                target_before=approved.base_commit,
                target_after=candidate_commit,
                base_commit=approved.base_commit,
                changed_files=approved.changed_files,
                patch_sha256=approved.patch_sha256,
                patch_size_bytes=approved.patch_size_bytes,
                manifest_path=str(manifest_path),
                rollback_ref=rollback_ref,
                validation=asdict(validation),
                service_restart_performed=True,
                migrations_applied=migrations_applied,
                database_backup=asdict(backup) if backup else None,
                rollback_performed=False,
            )
        except DeploymentValidationError as exc:
            attempt["status"] = "failed_validation"
            attempt["error"] = str(exc)[:10000]
            attempt["finished_at"] = _utc_now()
            self._write_manifest(manifest_path, manifest)
            raise
        except Exception as exc:
            rollback_errors: list[str] = []
            attempt["failure_stage"] = attempt.get("stage")
            attempt["error"] = f"{type(exc).__name__}: {exc}"[:10000]

            # Treat stop/start/promote/sync timeouts as ambiguous. Always force the
            # service down before touching code or database state, then reconcile
            # each durable boundary from its observed state instead of trusting a
            # local boolean that may not have been updated before the failure.
            try:
                self.runtime.stop()
                service_stopped = True
            except Exception as rollback_exc:
                rollback_errors.append(f"stop: {rollback_exc}")

            if source_sync_attempted:
                try:
                    self.runtime.restore_sources(
                        expected_current=candidate_commit,
                        rollback_commit=approved.base_commit,
                    )
                    sources_synchronized = False
                except Exception as rollback_exc:
                    rollback_errors.append(f"restore_sources: {rollback_exc}")

            if target_update_attempted or target_updated:
                try:
                    observed_target = self._resolve_commit(
                        self.target_repository,
                        self.target_branch,
                    )
                    if observed_target == candidate_commit:
                        self._run_git(
                            self.target_repository,
                            "update-ref",
                            f"refs/heads/{self.target_branch}",
                            approved.base_commit,
                            candidate_commit,
                            error_context="Unable to restore the deployment target",
                        )
                    elif observed_target != approved.base_commit:
                        raise AgentDeploymentError(
                            "Deployment target is neither the candidate nor rollback commit"
                        )
                    target_updated = False
                except Exception as rollback_exc:
                    rollback_errors.append(f"target: {rollback_exc}")

            if runtime_promotion_attempted or runtime_promoted:
                try:
                    self.runtime.restore(
                        expected_current=candidate_commit,
                        rollback_commit=approved.base_commit,
                    )
                    runtime_promoted = False
                except Exception as rollback_exc:
                    rollback_errors.append(f"runtime: {rollback_exc}")

            if migrations_applied:
                try:
                    migrations_dir = (
                        candidate_path / "backend" / "database" / "migrations"
                    )
                    rolled_back = self.database.downgrade(
                        migrations_dir,
                        migrations_applied,
                    )
                    attempt["migrations_rolled_back"] = list(rolled_back)
                    migrations_applied = ()
                except Exception as rollback_exc:
                    rollback_errors.append(f"database: {rollback_exc}")

            try:
                self.runtime.start()
                service_stopped = False
                restored_health = self.runtime.verify()
                attempt["restored_health"] = asdict(restored_health)
            except Exception as rollback_exc:
                rollback_errors.append(f"restart: {rollback_exc}")

            attempt["rollback_performed"] = True
            attempt["rollback_errors"] = rollback_errors
            attempt["finished_at"] = _utc_now()
            if rollback_errors:
                attempt["status"] = "rollback_failed"
                self._write_manifest(manifest_path, manifest)
                raise DeploymentRollbackError(
                    "Deployment failed and automatic rollback was incomplete: "
                    + " | ".join(rollback_errors)
                ) from exc

            attempt["status"] = "rolled_back"
            self._write_manifest(manifest_path, manifest)
            raise DeploymentRolledBackError(
                f"Deployment failed and was rolled back safely: {exc}"
            ) from exc

    def _validate_approved_implementation(
        self,
        claim: ClaimedRun,
        source: DeploymentSource,
    ) -> ApprovedImplementation:
        metadata = source.implementation_result_metadata
        if metadata.get("phase") != RunPhase.IMPLEMENTATION.value:
            raise AgentDeploymentError(
                "Deployment source metadata is not an implementation result"
            )
        workspace = metadata.get("workspace")
        if not isinstance(workspace, dict):
            raise AgentDeploymentError(
                "Implementation result is missing workspace evidence"
            )

        base_branch = self._required_string(workspace, "base_branch")
        base_commit = self._required_commit(workspace, "base_commit")
        feature_branch = self._required_string(workspace, "branch")
        worktree_path = self._required_string(workspace, "worktree_path")
        head_commit = self._required_commit(workspace, "head_commit")
        status_porcelain_value = workspace.get("status_porcelain")
        if (
            not isinstance(status_porcelain_value, str)
            or not status_porcelain_value.rstrip("\n")
        ):
            raise AgentDeploymentError(
                "Implementation result is missing status_porcelain"
            )
        status_porcelain = status_porcelain_value.rstrip("\n")
        patch_path_value = self._required_string(workspace, "artifact_patch")
        patch_size_bytes = workspace.get("patch_size_bytes")
        if (
            not isinstance(patch_size_bytes, int)
            or patch_size_bytes < 1
            or patch_size_bytes > self.MAX_PATCH_BYTES
        ):
            raise AgentDeploymentError(
                "Implementation result has an invalid backend patch size"
            )
        changed_files_value = workspace.get("changed_files")
        if not isinstance(changed_files_value, list) or not changed_files_value:
            raise AgentDeploymentError(
                "Deployment requires at least one changed implementation file"
            )
        if not all(isinstance(item, str) for item in changed_files_value):
            raise AgentDeploymentError(
                "Implementation changed-file evidence is invalid"
            )
        changed_files = tuple(sorted(set(changed_files_value)))
        if len(changed_files) > self.MAX_CHANGED_FILES:
            raise AgentDeploymentError(
                f"Backend deployment permits at most {self.MAX_CHANGED_FILES} changed files"
            )
        if len(changed_files) != len(changed_files_value):
            raise AgentDeploymentError(
                "Implementation changed-file evidence contains duplicates"
            )

        implementation_tests_value = metadata.get("tests")
        if not isinstance(implementation_tests_value, list):
            raise AgentDeploymentError(
                "Implementation result is missing test evidence"
            )
        implementation_tests: list[dict] = []
        for test in implementation_tests_value:
            if not isinstance(test, dict):
                raise AgentDeploymentError("Implementation test evidence is invalid")
            command = test.get("command")
            status = test.get("status")
            details = test.get("details")
            if (
                not isinstance(command, str)
                or not command.strip()
                or status not in {"passed", "failed", "not_run"}
                or not isinstance(details, str)
            ):
                raise AgentDeploymentError(
                    "Implementation test evidence is invalid"
                )
            if status == "failed":
                raise AgentDeploymentError(
                    "Deployment approval contains failed implementation tests"
                )
            implementation_tests.append(
                {
                    "command": command.strip(),
                    "status": status,
                    "details": details.strip(),
                }
            )

        expected_branch = f"agent/card-{claim.card_id}"
        expected_worktree = self.source_worktree_root / f"card-{claim.card_id}"
        if feature_branch != expected_branch or claim.feature_branch != expected_branch:
            raise AgentDeploymentError(
                "Deployment feature branch does not match the card"
            )
        configured_worktree = Path(worktree_path).expanduser()
        if (
            not configured_worktree.is_absolute()
            or configured_worktree != expected_worktree
            or claim.worktree_path != str(expected_worktree)
        ):
            raise AgentDeploymentError(
                "Deployment worktree path does not match the card"
            )
        self._assert_path_within(
            expected_worktree,
            self.source_worktree_root,
            field="implementation worktree",
        )
        self._verify_worktree(
            expected_worktree,
            expected_branch=feature_branch,
            expected_common_directory=self.source_common_directory,
        )

        if base_branch != (claim.base_branch or "main"):
            raise AgentDeploymentError(
                "Implementation base branch does not match the card"
            )
        if head_commit != base_commit:
            raise AgentDeploymentError(
                "Implementation branch contains commits; deployment candidates "
                "must be created by RemiHub"
            )
        current_head = self._resolve_commit(expected_worktree, "HEAD")
        if current_head != head_commit:
            raise AgentDeploymentError(
                "Implementation worktree HEAD changed after review"
            )

        current_status = self._run_git(
            expected_worktree,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            error_context="Unable to inspect implementation worktree status",
        ).stdout.rstrip()
        if current_status != status_porcelain:
            raise AgentDeploymentError(
                "Implementation worktree status changed after review"
            )
        current_changed_files = self._changed_files(expected_worktree)
        if current_changed_files != changed_files:
            raise AgentDeploymentError(
                "Implementation changed files no longer match review evidence"
            )
        self._require_backend_paths(changed_files, expected_worktree)

        expected_patch_path = (
            self.source_artifact_root
            / claim.card_id
            / f"{source.implementation_run_id}.patch"
        )
        configured_patch_path = Path(patch_path_value).expanduser()
        if (
            not configured_patch_path.is_absolute()
            or configured_patch_path != expected_patch_path
        ):
            raise AgentDeploymentError(
                "Implementation patch path does not match the approved run"
            )
        self._assert_path_within(
            expected_patch_path,
            self.source_artifact_root,
            field="implementation patch",
        )
        if expected_patch_path.is_symlink() or not expected_patch_path.is_file():
            raise AgentDeploymentError("Approved implementation patch is missing")
        approved_patch = expected_patch_path.read_bytes()
        if len(approved_patch) != patch_size_bytes:
            raise AgentDeploymentError(
                "Approved implementation patch size changed after review"
            )
        current_patch = self._build_patch(expected_worktree)
        if current_patch != approved_patch:
            raise AgentDeploymentError(
                "Implementation patch changed after deployment approval"
            )
        self._reject_special_git_modes(approved_patch)
        patch_sha256 = hashlib.sha256(approved_patch).hexdigest()
        expected_tree = self._worktree_tree(expected_worktree, changed_files)

        return ApprovedImplementation(
            approval_id=source.approval_id,
            implementation_run_id=source.implementation_run_id,
            base_branch=base_branch,
            base_commit=base_commit,
            feature_branch=feature_branch,
            worktree_path=str(expected_worktree),
            head_commit=head_commit,
            changed_files=changed_files,
            status_porcelain=status_porcelain,
            patch_path=str(expected_patch_path),
            patch_size_bytes=patch_size_bytes,
            patch_sha256=patch_sha256,
            expected_tree=expected_tree,
            implementation_tests=tuple(implementation_tests),
        )

    def _materialize_candidate(
        self,
        claim: ClaimedRun,
        approved: ApprovedImplementation,
    ) -> tuple[str, str, Path]:
        candidate_branch = f"deployment/card-{claim.card_id}/r{claim.card_revision}"
        self._validate_branch_name(
            self.target_repository,
            candidate_branch,
            field="deployment candidate branch",
        )
        candidate_path = (
            self.candidate_worktree_root
            / f"card-{claim.card_id}-r{claim.card_revision}"
        )
        self._assert_path_within(
            candidate_path,
            self.candidate_worktree_root,
            field="deployment candidate worktree",
        )

        target_head = self._resolve_commit(self.target_repository, self.target_branch)
        existing_candidate = self._branch_commit(candidate_branch)
        if target_head not in {approved.base_commit, existing_candidate}:
            raise AgentDeploymentError(
                "Deployment target advanced before candidate creation"
            )

        if candidate_path.exists() or candidate_path.is_symlink():
            self._verify_worktree(
                candidate_path,
                expected_branch=candidate_branch,
                expected_common_directory=self.target_common_directory,
            )
        elif existing_candidate is not None:
            self._run_git(
                self.target_repository,
                "worktree",
                "add",
                str(candidate_path),
                candidate_branch,
                error_context="Unable to recover the deployment candidate",
            )
            self._verify_worktree(
                candidate_path,
                expected_branch=candidate_branch,
                expected_common_directory=self.target_common_directory,
            )
        else:
            if target_head != approved.base_commit:
                raise AgentDeploymentError(
                    "Deployment target advanced before candidate creation"
                )
            self._run_git(
                self.target_repository,
                "worktree",
                "add",
                "-b",
                candidate_branch,
                str(candidate_path),
                approved.base_commit,
                error_context="Unable to create the deployment candidate",
            )
            self._verify_worktree(
                candidate_path,
                expected_branch=candidate_branch,
                expected_common_directory=self.target_common_directory,
            )

        branch_head = self._resolve_commit(candidate_path, "HEAD")
        if branch_head == approved.base_commit:
            self._require_clean(candidate_path)
            self._run_git(
                candidate_path,
                "apply",
                "--index",
                "--binary",
                "--whitespace=error-all",
                "--",
                approved.patch_path,
                error_context="Unable to apply the approved implementation patch",
            )
            staged_files = self._staged_files(candidate_path)
            if staged_files != approved.changed_files:
                raise AgentDeploymentError(
                    "Deployment candidate changed files do not match approval evidence"
                )
            self._run_git(
                candidate_path,
                "-c",
                "user.name=RemiHub Deployment",
                "-c",
                "user.email=remihub-deployment@invalid.local",
                "commit",
                "--no-gpg-sign",
                "-m",
                f"Deploy agent card {claim.card_id} revision {claim.card_revision}",
                error_context="Unable to create the immutable deployment commit",
            )
            candidate_commit = self._resolve_commit(candidate_path, "HEAD")
        else:
            candidate_commit = branch_head

        self._validate_candidate_commit(candidate_commit, approved)
        self._require_clean(candidate_path)
        return candidate_branch, candidate_commit, candidate_path

    def _validate_candidate_commit(
        self,
        candidate_commit: str,
        approved: ApprovedImplementation,
    ) -> None:
        parent = self._resolve_commit(self.target_repository, f"{candidate_commit}^")
        if parent != approved.base_commit:
            raise AgentDeploymentError(
                "Deployment candidate has an unexpected parent commit"
            )
        changed_files = tuple(
            sorted(
                self._run_git(
                    self.target_repository,
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    candidate_commit,
                    error_context="Unable to inspect deployment commit",
                ).stdout.splitlines()
            )
        )
        if changed_files != approved.changed_files:
            raise AgentDeploymentError(
                "Deployment commit differs from approved changed files"
            )
        candidate_tree = self._run_git(
            self.target_repository,
            "rev-parse",
            "--verify",
            f"{candidate_commit}^{{tree}}",
            error_context="Unable to resolve deployment candidate tree",
        ).stdout.strip()
        if candidate_tree != approved.expected_tree:
            raise AgentDeploymentError(
                "Deployment commit tree differs from the approved worktree"
            )

    def _migration_plan(
        self,
        base_commit: str,
        candidate_commit: str,
        candidate_path: Path,
    ) -> MigrationPlan:
        migration_prefix = "backend/database/migrations/"
        status_lines = self._run_git(
            self.target_repository,
            "diff",
            "--name-status",
            "--no-renames",
            base_commit,
            candidate_commit,
            "--",
            migration_prefix,
            error_context="Unable to inspect candidate migrations",
        ).stdout.splitlines()
        if not status_lines:
            return MigrationPlan()

        added_files: list[str] = []
        grouped: dict[tuple[str, str], set[str]] = {}
        for line in status_lines:
            parts = line.split("\t")
            if len(parts) != 2 or parts[0] != "A":
                raise AgentDeploymentError(
                    "Historical migration files may not be modified, renamed, or deleted"
                )
            relative = parts[1]
            filename = PurePosixPath(relative).name
            match = migration_runner.MIGRATION_PATTERN.fullmatch(filename)
            if match is None:
                raise AgentDeploymentError(
                    f"Invalid migration filename: {relative}"
                )
            key = (match.group("version"), match.group("name"))
            grouped.setdefault(key, set()).add(match.group("direction"))
            added_files.append(relative)
            self._validate_migration_sql(
                candidate_path / relative,
                direction=match.group("direction"),
            )

        base_files = self._run_git(
            self.target_repository,
            "ls-tree",
            "-r",
            "--name-only",
            base_commit,
            "--",
            migration_prefix,
            error_context="Unable to inspect historical migration versions",
        ).stdout.splitlines()
        base_versions = [
            match.group("version")
            for path in base_files
            if (
                match := migration_runner.MIGRATION_PATTERN.fullmatch(
                    PurePosixPath(path).name
                )
            )
        ]
        maximum_base = max(base_versions, default="0000")

        ordered = sorted(grouped.items())
        for (version, name), directions in ordered:
            if directions != {"up", "down"}:
                raise AgentDeploymentError(
                    f"Migration {version}_{name} must add both up and down SQL files"
                )
            if version <= maximum_base:
                raise AgentDeploymentError(
                    f"New migration version {version} must be greater than {maximum_base}"
                )
            self._validate_migration_pair(
                candidate_path / migration_prefix / f"{version}_{name}.up.sql",
                candidate_path / migration_prefix / f"{version}_{name}.down.sql",
            )

        return MigrationPlan(
            versions=tuple(key[0] for key, _ in ordered),
            names=tuple(key[1] for key, _ in ordered),
            files=tuple(sorted(added_files)),
        )

    @staticmethod
    def _validate_migration_sql(path: Path, *, direction: str) -> None:
        if path.is_symlink() or not path.is_file():
            raise AgentDeploymentError("Migration must be a regular file")
        if path.stat().st_size > 500_000:
            raise AgentDeploymentError("Migration SQL file is too large")
        try:
            sql = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AgentDeploymentError("Migration SQL must be UTF-8") from exc
        if not sql.strip() or "\x00" in sql:
            raise AgentDeploymentError("Migration SQL is empty or binary")
        if re.search(r"(?m)^\s*\\", sql):
            raise AgentDeploymentError("Migration SQL may not contain psql meta-commands")

        normalized = re.sub(r"--[^\n]*", " ", sql)
        normalized = re.sub(r"/\*.*?\*/", " ", normalized, flags=re.DOTALL)
        privileged_patterns = (
            r"\bALTER\s+SYSTEM\b",
            r"\bCREATE\s+(?:USER|ROLE|DATABASE|EXTENSION|FUNCTION|PROCEDURE)\b",
            r"\bDROP\s+(?:USER|ROLE|DATABASE|EXTENSION)\b",
            r"\bGRANT\b",
            r"\bREVOKE\b",
            r"\bSET\s+ROLE\b",
            r"\bSECURITY\s+DEFINER\b",
            r"\bCOPY\b[\s\S]*\bPROGRAM\b",
            r"\bDO\s+\$",
            r"\bCALL\b",
            r"\bpg_(?:read|write)_file\b",
            r"\blo_import\b",
            r"\bdblink\b",
            r"\bCREATE\s+INDEX\s+CONCURRENTLY\b",
            r"\b(?:BEGIN|COMMIT|ROLLBACK)\s*;",
        )
        for pattern in privileged_patterns:
            if re.search(pattern, normalized, flags=re.IGNORECASE):
                raise AgentDeploymentError(
                    "Migration SQL contains a blocked privileged or nontransactional statement"
                )

        if direction == "up":
            destructive_patterns = (
                r"\bDROP\s+(?:TABLE|SCHEMA|TYPE|INDEX|SEQUENCE|VIEW|MATERIALIZED\s+VIEW)\b",
                r"\bTRUNCATE\b",
                r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO)\b",
                r"\bALTER\s+TABLE\b[\s\S]*?\bDROP\s+(?:COLUMN|CONSTRAINT)\b",
                r"\bALTER\s+TABLE\b[\s\S]*?\bALTER\s+COLUMN\b[\s\S]*?\bTYPE\b",
                r"\bALTER\s+TABLE\b[\s\S]*?\bALTER\s+COLUMN\b[\s\S]*?\bDROP\s+(?:DEFAULT|NOT\s+NULL)\b",
            )
            for pattern in destructive_patterns:
                if re.search(pattern, normalized, flags=re.IGNORECASE):
                    raise AgentDeploymentError(
                        "Destructive or irreversible up migrations are blocked"
                    )

    @classmethod
    def _validate_migration_pair(cls, up_path: Path, down_path: Path) -> None:
        up_sql = cls._normalized_migration_sql(up_path)
        down_sql = cls._normalized_migration_sql(down_path)
        if re.search(r"\bCASCADE\b", down_sql, flags=re.IGNORECASE):
            raise AgentDeploymentError(
                "Automatic down migrations may not use CASCADE"
            )
        if re.search(
            r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO|TRUNCATE)\b",
            down_sql,
            flags=re.IGNORECASE,
        ):
            raise AgentDeploymentError(
                "Automatic down migrations may not mutate application data"
            )

        identifier = r"[a-z_][a-z0-9_$]*(?:\.[a-z_][a-z0-9_$]*)?"
        object_pattern = re.compile(
            rf"\bDROP\s+(MATERIALIZED\s+VIEW|TABLE|SCHEMA|TYPE|INDEX|SEQUENCE|VIEW)"
            rf"\s+(?:IF\s+EXISTS\s+)?({identifier})",
            flags=re.IGNORECASE,
        )
        create_keywords = {
            "MATERIALIZED VIEW": r"MATERIALIZED\s+VIEW",
            "TABLE": r"TABLE",
            "SCHEMA": r"SCHEMA",
            "TYPE": r"TYPE",
            "INDEX": r"(?:UNIQUE\s+)?INDEX",
            "SEQUENCE": r"SEQUENCE",
            "VIEW": r"VIEW",
        }
        for match in object_pattern.finditer(down_sql):
            object_type = re.sub(r"\s+", " ", match.group(1).upper())
            object_name = re.escape(match.group(2))
            create_pattern = (
                rf"\bCREATE\s+{create_keywords[object_type]}\s+"
                rf"(?:IF\s+NOT\s+EXISTS\s+)?{object_name}\b"
            )
            if not re.search(create_pattern, up_sql, flags=re.IGNORECASE):
                raise AgentDeploymentError(
                    "Down migration drops an object not created by its up migration: "
                    f"{match.group(2)}"
                )

        alter_drop_pattern = re.compile(
            rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?({identifier})"
            rf"[^;]*?\bDROP\s+(COLUMN|CONSTRAINT)\s+"
            rf"(?:IF\s+EXISTS\s+)?({identifier})",
            flags=re.IGNORECASE,
        )
        for match in alter_drop_pattern.finditer(down_sql):
            table_name = re.escape(match.group(1))
            object_type = match.group(2).upper()
            object_name = re.escape(match.group(3))
            if object_type == "COLUMN":
                add_pattern = (
                    rf"\bALTER\s+TABLE\s+{table_name}[^;]*?"
                    rf"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
                    rf"{object_name}\b"
                )
            else:
                add_pattern = (
                    rf"\bALTER\s+TABLE\s+{table_name}[^;]*?"
                    rf"\bADD\s+CONSTRAINT\s+{object_name}\b"
                )
            if not re.search(add_pattern, up_sql, flags=re.IGNORECASE):
                raise AgentDeploymentError(
                    "Down migration removes a table member not added by its up migration: "
                    f"{match.group(1)}.{match.group(3)}"
                )

        trigger_pattern = re.compile(
            rf"\bDROP\s+TRIGGER\s+(?:IF\s+EXISTS\s+)?({identifier})"
            rf"\s+ON\s+({identifier})",
            flags=re.IGNORECASE,
        )
        for match in trigger_pattern.finditer(down_sql):
            trigger_name = re.escape(match.group(1))
            table_name = re.escape(match.group(2))
            if not re.search(
                rf"\bCREATE\s+TRIGGER\s+{trigger_name}[^;]*?\bON\s+{table_name}\b",
                up_sql,
                flags=re.IGNORECASE,
            ):
                raise AgentDeploymentError(
                    "Down migration drops a trigger not created by its up migration: "
                    f"{match.group(1)}"
                )

    @staticmethod
    def _normalized_migration_sql(path: Path) -> str:
        try:
            sql = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AgentDeploymentError("Migration SQL must be UTF-8") from exc
        normalized = re.sub(r"--[^\n]*", " ", sql)
        return re.sub(r"/\*.*?\*/", " ", normalized, flags=re.DOTALL)

    def _require_backend_paths(
        self,
        changed_files: tuple[str, ...],
        worktree: Path,
    ) -> None:
        for relative in changed_files:
            if any(ord(character) < 32 for character in relative):
                raise AgentDeploymentError("Changed path contains control characters")
            pure_path = PurePosixPath(relative)
            if pure_path.is_absolute() or ".." in pure_path.parts:
                raise AgentDeploymentError("Changed path escapes the worktree")
            if relative in self.PROTECTED_BACKEND_PATHS:
                raise AgentDeploymentError(
                    f"Automatic deployment blocks protected control-plane file: {relative}"
                )
            allowed = False
            if len(pure_path.parts) >= 2 and pure_path.parts[0] == "backend":
                allowed = pure_path.suffix == ".py"
                if pure_path.parts[:3] == (
                    "backend",
                    "database",
                    "migrations",
                ):
                    allowed = bool(
                        migration_runner.MIGRATION_PATTERN.fullmatch(pure_path.name)
                    )
            elif len(pure_path.parts) >= 2 and pure_path.parts[0] == "tests":
                allowed = pure_path.suffix == ".py"
            elif len(pure_path.parts) >= 2 and pure_path.parts[0] == "docs":
                allowed = pure_path.suffix.lower() == ".md"
            if not allowed:
                raise AgentDeploymentError(
                    "Backend deployment permits only backend Python, tests, docs, "
                    "and paired SQL migration files"
                )

            actual_path = worktree.joinpath(*pure_path.parts)
            if actual_path.is_symlink():
                raise AgentDeploymentError("Backend deployment rejects symbolic links")
            if actual_path.exists():
                resolved = actual_path.resolve()
                if worktree.resolve() not in resolved.parents:
                    raise AgentDeploymentError("Changed path escapes the worktree")
                if not actual_path.is_file():
                    raise AgentDeploymentError(
                        "Backend deployment permits only regular files"
                    )
                try:
                    content = actual_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    raise AgentDeploymentError(
                        "Backend deployment requires UTF-8 text files"
                    ) from exc
                if "\x00" in content:
                    raise AgentDeploymentError("Backend deployment rejects binary files")

    @staticmethod
    def _reject_special_git_modes(patch: bytes) -> None:
        forbidden = (
            b"new file mode 120000",
            b"old mode 120000",
            b"new file mode 160000",
            b"old mode 160000",
            b"new file mode 100755",
            b"old file mode 100755",
            b"old mode 100755",
            b"new mode 100755",
            b"Subproject commit ",
        )
        if any(marker in patch for marker in forbidden):
            raise AgentDeploymentError(
                "Backend deployment rejects executable files, symlinks, and submodules"
            )

    def _rollback_ref(self, claim: ClaimedRun) -> str:
        return f"rollback-before-agent-card-{claim.card_id}-r{claim.card_revision}"

    def _ensure_rollback_ref(self, rollback_ref: str, base_commit: str) -> None:
        existing = self._tag_commit(rollback_ref)
        if existing is None:
            self._run_git(
                self.target_repository,
                "-c",
                "user.name=RemiHub Deployment",
                "-c",
                "user.email=remihub-deployment@invalid.local",
                "tag",
                "-a",
                rollback_ref,
                base_commit,
                "-m",
                f"Rollback before {rollback_ref}",
                error_context="Unable to create the deployment rollback reference",
            )
        elif existing != base_commit:
            raise AgentDeploymentError(
                "Existing deployment rollback reference points to another commit"
            )

    def _tag_commit(self, tag: str) -> str | None:
        result = self._run_git(
            self.target_repository,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/tags/{tag}^{{commit}}",
            allowed_return_codes=(0, 1),
            error_context="Unable to inspect deployment rollback reference",
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def _manifest_path(self, claim: ClaimedRun) -> Path:
        card_root = self.deployment_artifact_root / claim.card_id
        card_root.mkdir(mode=0o750, exist_ok=True)
        if card_root.is_symlink():
            raise AgentDeploymentError(
                "Deployment artifact directory must not be a symlink"
            )
        self._assert_path_within(
            card_root,
            self.deployment_artifact_root,
            field="deployment artifact directory",
        )
        return card_root / f"{claim.id}.deployment.json"

    def _load_or_initialize_manifest(
        self,
        claim: ClaimedRun,
        approved: ApprovedImplementation,
        *,
        candidate_branch: str,
        candidate_commit: str,
        rollback_ref: str,
        migration_plan: MigrationPlan,
    ) -> dict:
        manifest_path = self._manifest_path(claim)
        identity = {
            "schema_version": 2,
            "environment": self.environment,
            "mode": "backend-qa-to-production",
            "card_id": claim.card_id,
            "card_revision": claim.card_revision,
            "deployment_run_id": claim.id,
            "approval_id": approved.approval_id,
            "implementation_run_id": approved.implementation_run_id,
            "base_branch": approved.base_branch,
            "base_commit": approved.base_commit,
            "feature_branch": approved.feature_branch,
            "candidate_branch": candidate_branch,
            "candidate_commit": candidate_commit,
            "target_branch": self.target_branch,
            "changed_files": list(approved.changed_files),
            "patch_sha256": approved.patch_sha256,
            "patch_size_bytes": approved.patch_size_bytes,
            "implementation_tests": list(approved.implementation_tests),
            "rollback_ref": rollback_ref,
            "migration_plan": asdict(migration_plan),
            "android_release_performed": False,
        }
        # Normalize tuples to the same JSON-native list representation used after reload.
        identity = json.loads(json.dumps(identity))
        if not manifest_path.exists():
            return {**identity, "attempts": []}
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise AgentDeploymentError(
                "Existing deployment manifest is not a regular file"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentDeploymentError("Existing deployment manifest is invalid") from exc
        for key, value in identity.items():
            if manifest.get(key) != value:
                raise AgentDeploymentError(
                    f"Existing deployment manifest conflicts on {key}"
                )
        if not isinstance(manifest.get("attempts"), list):
            raise AgentDeploymentError("Existing deployment manifest attempts are invalid")
        return manifest

    @staticmethod
    def _begin_attempt(manifest: dict, claim: ClaimedRun) -> dict:
        attempts = manifest["attempts"]
        attempt = {
            "attempt_index": len(attempts) + 1,
            "worker_attempt_count": claim.attempt_count,
            "started_at": _utc_now(),
            "finished_at": None,
            "status": "running",
            "stage": "candidate_created",
            "migrations_applied": [],
            "migrations_rolled_back": [],
            "rollback_performed": False,
            "rollback_errors": [],
        }
        attempts.append(attempt)
        return attempt

    @staticmethod
    def _successful_attempt(manifest: dict) -> dict | None:
        for attempt in reversed(manifest["attempts"]):
            if attempt.get("status") == "succeeded":
                return attempt
        return None

    def _candidate_from_success(
        self,
        approved: ApprovedImplementation,
        *,
        candidate_branch: str,
        candidate_commit: str,
        rollback_ref: str,
        manifest_path: Path,
        attempt: dict,
        health: RuntimeHealth,
    ) -> DeploymentCandidate:
        validation = attempt.get("validation") or {}
        backup = attempt.get("database_backup")
        return DeploymentCandidate(
            approval_id=approved.approval_id,
            implementation_run_id=approved.implementation_run_id,
            environment=self.environment,
            candidate_branch=candidate_branch,
            candidate_commit=candidate_commit,
            target_branch=self.target_branch,
            target_before=approved.base_commit,
            target_after=candidate_commit,
            base_commit=approved.base_commit,
            changed_files=approved.changed_files,
            patch_sha256=approved.patch_sha256,
            patch_size_bytes=approved.patch_size_bytes,
            manifest_path=str(manifest_path),
            rollback_ref=rollback_ref,
            validation={**validation, "idempotent_health": asdict(health)},
            service_restart_performed=True,
            migrations_applied=tuple(attempt.get("migrations_applied") or ()),
            database_backup=backup,
            rollback_performed=False,
        )

    @staticmethod
    def _write_manifest(path: Path, manifest: dict) -> None:
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.chmod(temporary_path, 0o640)
        temporary_path.replace(path)

    def _worktree_tree(
        self,
        worktree: Path,
        changed_files: tuple[str, ...],
    ) -> str:
        with tempfile.TemporaryDirectory(
            prefix="remihub-deployment-index-",
            dir=self.deployment_artifact_root,
        ) as temporary_directory:
            temporary_root = Path(temporary_directory)
            index_path = temporary_root / "index"
            object_directory = temporary_root / "objects"
            object_directory.mkdir(mode=0o750)
            environment_overrides = {
                "GIT_INDEX_FILE": str(index_path),
                "GIT_OBJECT_DIRECTORY": str(object_directory),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(
                    self.source_common_directory / "objects"
                ),
            }
            self._run_git(
                worktree,
                "read-tree",
                "HEAD",
                environment_overrides=environment_overrides,
                error_context="Unable to initialize deployment validation index",
            )
            self._run_git(
                worktree,
                "add",
                "-A",
                "--",
                *changed_files,
                environment_overrides=environment_overrides,
                error_context="Unable to stage deployment validation tree",
            )
            return self._run_git(
                worktree,
                "write-tree",
                environment_overrides=environment_overrides,
                error_context="Unable to create deployment validation tree",
            ).stdout.strip()

    def _build_patch(self, worktree: Path) -> bytes:
        tracked = self._run_git_bytes(
            worktree,
            "diff",
            "--binary",
            "--no-ext-diff",
            "HEAD",
            "--",
            error_context="Unable to recreate the implementation patch",
        ).stdout
        untracked_parts: list[bytes] = []
        for relative in self._untracked_files(worktree):
            result = self._run_git_bytes(
                worktree,
                "diff",
                "--no-index",
                "--binary",
                "--",
                "/dev/null",
                relative,
                allowed_return_codes=(0, 1),
                error_context="Unable to include an untracked implementation file",
            )
            untracked_parts.append(result.stdout)
        return tracked + b"".join(untracked_parts)

    def _changed_files(self, worktree: Path) -> tuple[str, ...]:
        tracked = self._run_git(
            worktree,
            "diff",
            "--name-only",
            "--no-ext-diff",
            "HEAD",
            "--",
            error_context="Unable to list implementation changes",
        ).stdout.splitlines()
        return tuple(sorted(set(tracked) | set(self._untracked_files(worktree))))

    def _untracked_files(self, worktree: Path) -> tuple[str, ...]:
        result = self._run_git_bytes(
            worktree,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            error_context="Unable to list untracked implementation files",
        )
        return tuple(
            sorted(
                item.decode("utf-8", errors="surrogateescape")
                for item in result.stdout.split(b"\0")
                if item
            )
        )

    def _staged_files(self, worktree: Path) -> tuple[str, ...]:
        return tuple(
            sorted(
                self._run_git(
                    worktree,
                    "diff",
                    "--cached",
                    "--name-only",
                    "--no-ext-diff",
                    "--",
                    error_context="Unable to inspect staged deployment changes",
                ).stdout.splitlines()
            )
        )

    def _require_clean(self, worktree: Path) -> None:
        status = self._run_git(
            worktree,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            error_context="Unable to inspect deployment candidate worktree",
        ).stdout.strip()
        if status:
            raise AgentDeploymentError(
                "Deployment candidate worktree contains unexpected changes"
            )

    def _branch_commit(self, branch: str) -> str | None:
        result = self._run_git(
            self.target_repository,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}^{{commit}}",
            allowed_return_codes=(0, 1),
            error_context="Unable to inspect deployment candidate branch",
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def _verify_worktree(
        self,
        path: Path,
        *,
        expected_branch: str,
        expected_common_directory: Path,
    ) -> None:
        if path.is_symlink() or not path.is_dir():
            raise AgentDeploymentError("Expected Git worktree is missing")
        top_level = Path(
            self._run_git(
                path,
                "rev-parse",
                "--show-toplevel",
                error_context="Deployment path is not a Git worktree",
            ).stdout.strip()
        ).resolve()
        if top_level != path.resolve():
            raise AgentDeploymentError("Deployment path is not a worktree root")
        if self._common_git_directory(path) != expected_common_directory:
            raise AgentDeploymentError(
                "Deployment worktree belongs to an unexpected Git repository"
            )
        branch = self._run_git(
            path,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            error_context="Deployment worktree must use a named branch",
        ).stdout.strip()
        if branch != expected_branch:
            raise AgentDeploymentError(
                "Deployment worktree is checked out on an unexpected branch"
            )

    @staticmethod
    def _required_string(mapping: dict, key: str) -> str:
        value = mapping.get(key)
        if not isinstance(value, str) or not value.strip():
            raise AgentDeploymentError(f"Implementation result is missing {key}")
        return value.strip()

    @classmethod
    def _required_commit(cls, mapping: dict, key: str) -> str:
        value = cls._required_string(mapping, key)
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise AgentDeploymentError(
                f"Implementation result has an invalid {key}"
            )
        return value

    def _resolve_commit(self, repository: Path, reference: str) -> str:
        return self._run_git(
            repository,
            "rev-parse",
            "--verify",
            f"{reference}^{{commit}}",
            error_context=f"Unable to resolve Git reference: {reference}",
        ).stdout.strip()

    def _validate_branch_name(
        self,
        repository: Path,
        value: str,
        *,
        field: str,
    ) -> str:
        normalized = value.strip()
        if not normalized:
            raise AgentWorkerConfigurationError(f"{field} must not be blank")
        self._run_git(
            repository,
            "check-ref-format",
            "--branch",
            normalized,
            error_context=f"Invalid {field}",
        )
        return normalized

    def _common_git_directory(self, repository: Path) -> Path:
        value = self._run_git(
            repository,
            "rev-parse",
            "--git-common-dir",
            error_context="Unable to resolve Git common directory",
        ).stdout.strip()
        path = Path(value)
        if not path.is_absolute():
            path = repository / path
        return path.resolve()

    @staticmethod
    def _assert_path_within(path: Path, root: Path, *, field: str) -> None:
        resolved_root = root.resolve()
        resolved_parent = path.parent.resolve()
        if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
            raise AgentDeploymentError(f"{field} escapes its configured root")

    def _git_environment(self) -> dict[str, str]:
        return {
            "GIT_CONFIG_GLOBAL": (
                "/opt/remihub-agent/deployment/config/"
                "git-safe-directory.ini"
            ),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": os.environ.get("HOME", "/nonexistent"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }

    def _run_git(
        self,
        repository: Path,
        *arguments: str,
        allowed_return_codes: Sequence[int] = (0,),
        environment_overrides: dict[str, str] | None = None,
        error_context: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = self._git_environment()
        if environment_overrides:
            environment.update(environment_overrides)
        try:
            result = subprocess.run(
                [self.git_binary, "-C", str(repository), *arguments],
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=self.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentDeploymentError(error_context) from exc
        if result.returncode not in allowed_return_codes:
            detail = [
                line.strip()
                for line in result.stderr.splitlines()
                if line.strip()
            ]
            suffix = f": {' | '.join(detail[-3:])}" if detail else ""
            raise AgentDeploymentError(f"{error_context}{suffix}")
        return result

    def _run_git_bytes(
        self,
        repository: Path,
        *arguments: str,
        allowed_return_codes: Sequence[int] = (0,),
        error_context: str,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                [self.git_binary, "-C", str(repository), *arguments],
                check=False,
                capture_output=True,
                env=self._git_environment(),
                timeout=self.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentDeploymentError(error_context) from exc
        if result.returncode not in allowed_return_codes:
            raise AgentDeploymentError(error_context)
        return result


class GitBackendDeploymentExecutor:
    allowed_phases = frozenset({RunPhase.DEPLOYMENT})
    allowed_repository_scopes = frozenset({RepositoryScope.BACKEND})

    def __init__(
        self,
        *,
        deployment_manager: GitBackendDeploymentManager,
        retry_after_seconds: int = 60,
    ):
        if retry_after_seconds < 1:
            raise ValueError("retry_after_seconds must be at least 1")
        self.deployment_manager = deployment_manager
        self.retry_after_seconds = retry_after_seconds

    def execute(self, claim: ClaimedRun) -> ExecutionResult:
        require_backend_repository_scope(
            claim.repository_scope,
            action="Deployment",
        )
        try:
            candidate = self.deployment_manager.deploy(claim)
        except DeploymentRolledBackError as exc:
            raise AgentTemporarilyBlockedError(
                str(exc),
                retry_after_seconds=self.retry_after_seconds,
            ) from exc
        changed_files = "\n".join(
            f"- `{path}`" for path in candidate.changed_files
        )
        migration_text = (
            ", ".join(candidate.migrations_applied)
            if candidate.migrations_applied
            else "none"
        )
        message = f"""
Deployed the approved backend candidate to {candidate.environment}.

- Candidate commit: `{candidate.candidate_commit}`
- Target branch: `{candidate.target_branch}`
- Rollback reference: `{candidate.rollback_ref}`
- Service restart performed: yes
- Migrations applied: {migration_text}
- Health check: `/openapi.json` passed

Changed files:
{changed_files}
""".strip()
        return ExecutionResult(
            message=message,
            card_status=CardStatus.COMPLETED,
            metadata={
                "executor": "git_backend_deployment",
                "phase": claim.phase.value,
                "environment": candidate.environment,
                "mode": "backend-qa-to-production",
                "candidate": asdict(candidate),
            },
        )


# Compatibility aliases for the promoted documentation-only foundation. New worker
# configuration uses git-backend-deployment and the generic classes above.
GitQaDeploymentManager = GitBackendDeploymentManager
GitQaDeploymentExecutor = GitBackendDeploymentExecutor


def _required_absolute_directory(value: str | Path, *, field: str) -> Path:
    configured = Path(value).expanduser()
    if not configured.is_absolute():
        raise AgentWorkerConfigurationError(f"{field} must be an absolute path")
    resolved = configured.resolve()
    if not resolved.is_dir():
        raise AgentWorkerConfigurationError(
            f"{field} does not exist or is not a directory: {resolved}"
        )
    return resolved


def _required_absolute_file(value: str | Path, *, field: str) -> Path:
    configured = Path(value).expanduser()
    if not configured.is_absolute():
        raise AgentWorkerConfigurationError(f"{field} must be an absolute path")
    if configured.is_symlink() or not configured.is_file():
        raise AgentWorkerConfigurationError(
            f"{field} does not exist or is not a regular file: {configured}"
        )
    return configured.resolve()


def _required_executable(value: str | Path, *, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise AgentWorkerConfigurationError(f"{field} must be an absolute path")
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AgentWorkerConfigurationError(f"{field} is not executable: {path}")
    return path.resolve()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail(value: str, maximum: int) -> str:
    normalized = value.strip()
    if len(normalized) <= maximum:
        return normalized
    return normalized[-maximum:]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

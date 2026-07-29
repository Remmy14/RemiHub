import importlib.machinery
import importlib.util
import json
import os
import sys
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.core.agent_deployment import (
    AgentDeploymentError,
    BackupEvidence,
    DeploymentRollbackError,
    DeploymentRolledBackError,
    DeploymentValidationError,
    GitBackendDeploymentExecutor,
    GitBackendDeploymentManager,
    PostgresDeploymentDatabase,
    RuntimeHealth,
    ValidationEvidence,
)
from backend.core.agent_state import CardStatus, RepositoryScope, RunPhase
from backend.core.agent_worker import (
    AgentTemporarilyBlockedError,
    AgentWorkerConfigurationError,
    ClaimedRun,
    DeploymentSource,
)
from backend.core.agent_workspace import GitImplementationWorkspaceManager


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


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class DeploymentImplementationTestEvidenceTests(unittest.TestCase):
    def test_backend_failed_implementation_test_remains_blocking(self):
        manager = object.__new__(GitBackendDeploymentManager)
        metadata = {
            "tests": [
                {
                    "command": "python -m unittest",
                    "status": "failed",
                    "details": "one test failed",
                }
            ]
        }

        with self.assertRaisesRegex(
            AgentDeploymentError,
            "contains failed implementation tests",
        ):
            manager._validate_implementation_tests(metadata)

    def test_backend_nonfailed_implementation_tests_are_preserved(self):
        manager = object.__new__(GitBackendDeploymentManager)
        metadata = {
            "tests": [
                {
                    "command": " git diff --check ",
                    "status": "passed",
                    "details": " clean ",
                },
                {
                    "command": "python -m unittest",
                    "status": "not_run",
                    "details": "not available",
                },
            ]
        }

        observed = manager._validate_implementation_tests(metadata)

        self.assertEqual(
            observed,
            (
                {
                    "command": "git diff --check",
                    "status": "passed",
                    "details": "clean",
                },
                {
                    "command": "python -m unittest",
                    "status": "not_run",
                    "details": "not available",
                },
            ),
        )


class FakeValidator:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = []

    def validate(self, candidate_worktree: Path) -> ValidationEvidence:
        self.calls.append(candidate_worktree)
        if self.fail:
            raise DeploymentValidationError("full backend tests failed")
        return ValidationEvidence(
            command="validator /workspace",
            duration_ms=25,
            stdout_sha256="a" * 64,
            stdout_tail="140 tests passed",
            stderr_tail="",
        )


class FakeDatabase:
    def __init__(
        self,
        *,
        pending=(),
        fail_upgrade=False,
        apply_before_failure=False,
        fail_downgrade=False,
    ):
        self.configured_pending = tuple(pending)
        self.applied = []
        self.fail_upgrade = fail_upgrade
        self.apply_before_failure = apply_before_failure
        self.fail_downgrade = fail_downgrade
        self.events = []

    def pending_versions(self, migrations_dir: Path):
        self.events.append(("pending", migrations_dir))
        return tuple(
            version
            for version in self.configured_pending
            if version not in self.applied
        )

    def backup(self, *, card_id: str, deployment_run_id: str):
        self.events.append(("backup", card_id, deployment_run_id))
        return BackupEvidence(
            path=f"/backups/{card_id}/{deployment_run_id}.dump",
            size_bytes=1234,
            sha256="b" * 64,
        )

    def upgrade(self, migrations_dir: Path, expected_versions):
        self.events.append(("upgrade", tuple(expected_versions)))
        if self.fail_upgrade:
            if self.apply_before_failure:
                self.applied.extend(expected_versions)
            raise RuntimeError("migration failed")
        self.applied.extend(expected_versions)
        return tuple(expected_versions)

    def downgrade(self, migrations_dir: Path, versions):
        self.events.append(("downgrade", tuple(versions)))
        if self.fail_downgrade:
            raise RuntimeError("down migration failed")
        for version in versions:
            if version in self.applied:
                self.applied.remove(version)
        return tuple(reversed(versions))


class FakeRuntime:
    def __init__(
        self,
        *,
        fail_verify_times=0,
        fail_restore=False,
        fail_start_after_apply=False,
        fail_promote_after_apply=False,
        fail_sync_after_apply=False,
    ):
        self.fail_verify_times = fail_verify_times
        self.fail_restore = fail_restore
        self.fail_start_after_apply = fail_start_after_apply
        self.fail_promote_after_apply = fail_promote_after_apply
        self.fail_sync_after_apply = fail_sync_after_apply
        self.events = []
        self.current_commit = None
        self.sources_commit = None
        self.active = True

    def stop(self):
        self.events.append("stop")
        self.active = False

    def start(self):
        self.events.append("start")
        self.active = True
        if self.fail_start_after_apply:
            self.fail_start_after_apply = False
            raise RuntimeError("start response lost")

    def promote(
        self,
        *,
        candidate_branch,
        candidate_commit,
        expected_before,
        rollback_ref,
    ):
        self.events.append(
            ("promote", candidate_branch, candidate_commit, expected_before, rollback_ref)
        )
        self.current_commit = candidate_commit
        if self.fail_promote_after_apply:
            self.fail_promote_after_apply = False
            raise RuntimeError("promote response lost")

    def restore(self, *, expected_current, rollback_commit):
        self.events.append(("restore", expected_current, rollback_commit))
        if self.fail_restore:
            raise RuntimeError("restore failed")
        self.current_commit = rollback_commit

    def synchronize_sources(
        self,
        *,
        candidate_branch,
        candidate_commit,
        expected_before,
        rollback_ref,
    ):
        self.events.append(("sync", candidate_commit, expected_before))
        self.sources_commit = candidate_commit
        if self.fail_sync_after_apply:
            self.fail_sync_after_apply = False
            raise RuntimeError("source sync response lost")

    def restore_sources(self, *, expected_current, rollback_commit):
        self.events.append(("restore_sources", expected_current, rollback_commit))
        if self.sources_commit in {None, expected_current, rollback_commit}:
            self.sources_commit = rollback_commit
            return
        raise RuntimeError("unexpected source state")

    def verify(self):
        self.events.append("verify")
        if self.fail_verify_times:
            self.fail_verify_times -= 1
            raise AgentDeploymentError("health check failed")
        if not self.active:
            raise AgentDeploymentError("service inactive")
        return RuntimeHealth(
            service_active=True,
            url="http://127.0.0.1:8001/openapi.json",
            status_code=200,
            response_sha256="c" * 64,
        )


class PostgresDeploymentDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config = self.root / "migrator.ini"
        self.config.write_text(
            "[Database]\nuser = migrator\npassword = secret\nhost = 127.0.0.1\nport = 5432\ndatabase = remihub_qa\n",
            encoding="utf-8",
        )
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.pg_dump = self.root / "pg_dump"
        self.pg_restore = self.root / "pg_restore"
        for executable in (self.pg_dump, self.pg_restore):
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)

    def _database(self):
        return PostgresDeploymentDatabase(
            config_path=self.config,
            backup_root=self.backups,
            owner_role="remihub_qa_owner",
            pg_dump_binary=str(self.pg_dump),
            pg_restore_binary=str(self.pg_restore),
        )

    def test_connection_explicitly_assumes_fixed_owner_role(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        with patch(
            "backend.core.agent_deployment.migration_runner._connect",
            return_value=connection,
        ):
            result = self._database()._connect()

        self.assertIs(result, connection)
        cursor.execute.assert_called_once_with("SET ROLE remihub_qa_owner")

    def test_backup_uses_same_fixed_owner_role(self):
        database = self._database()
        commands = []

        def fake_run(command, *, environment, context):
            commands.append((command, environment, context))
            if "--file" in command:
                output = Path(command[command.index("--file") + 1])
                output.write_bytes(b"verified test dump")

        database._run = fake_run
        with patch(
            "backend.core.agent_deployment.load_config",
            return_value={
                "Database": {
                    "user": "remihub_qa_migrator",
                    "password": "secret",
                    "host": "127.0.0.1",
                    "port": "5432",
                    "database": "remihub_qa",
                }
            },
        ):
            evidence = database.backup(
                card_id="card-id",
                deployment_run_id="run-id",
            )

        dump_command = commands[0][0]
        self.assertEqual(
            dump_command[dump_command.index("--role") + 1],
            "remihub_qa_owner",
        )
        self.assertGreater(evidence.size_bytes, 0)
        self.assertEqual(len(evidence.sha256), 64)

    def test_invalid_owner_role_is_rejected(self):
        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "DATABASE_OWNER_ROLE",
        ):
            PostgresDeploymentDatabase(
                config_path=self.config,
                backup_root=self.backups,
                owner_role="remihub; DROP ROLE",
            )


class GitBackendDeploymentManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.seed = self.root / "seed"
        self.source = self.root / "implementation.git"
        self.source_worktrees = self.root / "implementation-worktrees"
        self.source_artifacts = self.root / "implementation-artifacts"
        self.target = self.root / "qa-deployment.git"
        self.candidate_worktrees = self.root / "deployment-worktrees"
        self.deployment_artifacts = self.root / "deployment-artifacts"
        for path in (
            self.seed,
            self.source_worktrees,
            self.source_artifacts,
            self.candidate_worktrees,
            self.deployment_artifacts,
        ):
            path.mkdir()

        subprocess.run(
            ["git", "init", "-b", "main", str(self.seed)],
            check=True,
            capture_output=True,
        )
        _git(self.seed, "config", "user.name", "RemiHub Test")
        _git(self.seed, "config", "user.email", "remihub@example.invalid")
        for directory in (
            "backend/core",
            "backend/database/migrations",
            "tests",
            "docs",
        ):
            (self.seed / directory).mkdir(parents=True, exist_ok=True)
        (self.seed / "backend" / "example.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        (self.seed / "backend" / "core" / "agent_deployment.py").write_text(
            "CONTROL = True\n", encoding="utf-8"
        )
        (self.seed / "tests" / "test_example.py").write_text(
            "def test_placeholder():\n    assert True\n", encoding="utf-8"
        )
        (self.seed / "docs" / "existing.md").write_text(
            "# Existing\n", encoding="utf-8"
        )
        migrations = self.seed / "backend" / "database" / "migrations"
        (migrations / "0001_initial.up.sql").write_text(
            "CREATE TABLE example (id integer PRIMARY KEY);\n",
            encoding="utf-8",
        )
        (migrations / "0001_initial.down.sql").write_text(
            "DROP TABLE example;\n", encoding="utf-8"
        )
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-m", "Initial")
        self.base_commit = _git(self.seed, "rev-parse", "HEAD")
        subprocess.run(
            ["git", "clone", "--bare", str(self.seed), str(self.source)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "clone", "--bare", str(self.seed), str(self.target)],
            check=True,
            capture_output=True,
        )
        _git(self.target, "remote", "remove", "origin")
        _git(self.target, "update-ref", "refs/heads/qa-main", self.base_commit)

        self.implementation_claim = claimed_run(phase=RunPhase.IMPLEMENTATION)
        self.implementation_manager = GitImplementationWorkspaceManager(
            source_repository=self.source,
            worktree_root=self.source_worktrees,
            artifact_root=self.source_artifacts,
        )
        self.validator = FakeValidator()
        self.database = FakeDatabase()
        self.runtime = FakeRuntime()

    def _prepare_deployment(self, changes, *, tests=None):
        with self.implementation_manager.locked_workspace(
            self.implementation_claim,
            persist_workspace=lambda branch, path: None,
        ) as workspace:
            for relative, content in changes.items():
                path = workspace.path / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if content is None:
                    path.unlink()
                else:
                    path.write_text(content, encoding="utf-8")
            snapshot = self.implementation_manager.capture_snapshot(
                self.implementation_claim,
                workspace,
            )
            self.implementation_workspace = workspace

        metadata = {
            "executor": "codex_implementation",
            "phase": "implementation",
            "tests": tests
            if tests is not None
            else [
                {
                    "command": "python -m unittest tests.test_example",
                    "status": "passed",
                    "details": "passed",
                }
            ],
            "workspace": {
                "artifact_patch": str(snapshot.patch_path),
                "base_branch": workspace.base_branch,
                "base_commit": workspace.base_commit,
                "branch": snapshot.branch,
                "changed_files": list(snapshot.changed_files),
                "diff_stat": snapshot.diff_stat,
                "head_commit": snapshot.head_commit,
                "patch_size_bytes": snapshot.patch_size_bytes,
                "status_porcelain": snapshot.status_porcelain,
                "worktree_path": str(workspace.path),
            },
        }
        return replace(
            claimed_run(phase=RunPhase.DEPLOYMENT),
            id="7ce86bc5-59db-4c98-ac77-bd6038098e17",
            card_id=self.implementation_claim.card_id,
            feature_branch=snapshot.branch,
            worktree_path=str(workspace.path),
            deployment_source=DeploymentSource(
                approval_id="db62f682-713c-4516-a81f-c3c884c97bdc",
                implementation_run_id=self.implementation_claim.id,
                implementation_result_metadata=metadata,
            ),
        )

    def _manager(
        self,
        *,
        validator=None,
        database=None,
        runtime=None,
        environment="qa",
    ):
        target_branch = "qa-main" if environment == "qa" else "production-main"
        if environment == "production":
            _git(self.target, "update-ref", "refs/heads/production-main", self.base_commit)
        return GitBackendDeploymentManager(
            environment=environment,
            source_repository=self.source,
            source_worktree_root=self.source_worktrees,
            source_artifact_root=self.source_artifacts,
            target_repository=self.target,
            candidate_worktree_root=self.candidate_worktrees,
            deployment_artifact_root=self.deployment_artifacts,
            target_branch=target_branch,
            validator=validator or self.validator,
            database=database or self.database,
            runtime=runtime or self.runtime,
        )

    def test_git_commands_use_protected_fixed_safe_directory_config(self):
        environment = self._manager()._git_environment()

        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(
            environment["GIT_CONFIG_GLOBAL"],
            "/opt/remihub-agent/deployment/config/git-safe-directory.ini",
        )

    def test_backend_candidate_runs_validation_and_promotes_exact_commit(self):
        claim = self._prepare_deployment(
            {"backend/example.py": "VALUE = 2\n"}
        )
        candidate = self._manager().deploy(claim)

        self.assertEqual(candidate.target_before, self.base_commit)
        self.assertEqual(
            _git(self.target, "rev-parse", "qa-main"), candidate.candidate_commit
        )
        self.assertEqual(candidate.changed_files, ("backend/example.py",))
        self.assertTrue(candidate.service_restart_performed)
        self.assertEqual(candidate.migrations_applied, ())
        self.assertEqual(self.runtime.events[0], "stop")
        self.assertIn("start", self.runtime.events)
        manifest = json.loads(Path(candidate.manifest_path).read_text())
        self.assertEqual(manifest["attempts"][-1]["status"], "succeeded")
        self.assertEqual(
            manifest["attempts"][-1]["validation"]["stdout_tail"],
            "140 tests passed",
        )

    def test_new_paired_reversible_migration_is_backed_up_and_applied(self):
        claim = self._prepare_deployment(
            {
                "backend/database/migrations/0002_widget.up.sql": (
                    "CREATE TABLE widgets (id integer PRIMARY KEY);\n"
                ),
                "backend/database/migrations/0002_widget.down.sql": (
                    "DROP TABLE widgets;\n"
                ),
            }
        )
        database = FakeDatabase(pending=("0002",))
        candidate = self._manager(database=database).deploy(claim)

        self.assertEqual(candidate.migrations_applied, ("0002",))
        self.assertIsNotNone(candidate.database_backup)
        self.assertIn(("upgrade", ("0002",)), database.events)
        backup_index = next(
            index for index, event in enumerate(database.events) if event[0] == "backup"
        )
        upgrade_index = database.events.index(("upgrade", ("0002",)))
        self.assertLess(backup_index, upgrade_index)

    def test_historical_migration_modification_is_rejected(self):
        claim = self._prepare_deployment(
            {
                "backend/database/migrations/0001_initial.up.sql": (
                    "CREATE TABLE example (id bigint PRIMARY KEY);\n"
                )
            }
        )
        with self.assertRaisesRegex(AgentDeploymentError, "Historical migration"):
            self._manager().deploy(claim)
        self.assertEqual(_git(self.target, "rev-parse", "qa-main"), self.base_commit)

    def test_migration_requires_up_and_down_pair(self):
        claim = self._prepare_deployment(
            {
                "backend/database/migrations/0002_widget.up.sql": (
                    "CREATE TABLE widgets (id integer PRIMARY KEY);\n"
                )
            }
        )
        with self.assertRaisesRegex(AgentDeploymentError, "both up and down"):
            self._manager().deploy(claim)

    def test_destructive_up_migration_is_rejected(self):
        claim = self._prepare_deployment(
            {
                "backend/database/migrations/0002_widget.up.sql": "DROP TABLE example;\n",
                "backend/database/migrations/0002_widget.down.sql": (
                    "CREATE TABLE example (id integer PRIMARY KEY);\n"
                ),
            }
        )
        with self.assertRaisesRegex(AgentDeploymentError, "Destructive"):
            self._manager().deploy(claim)


    def test_down_migration_cannot_drop_unrelated_object(self):
        claim = self._prepare_deployment(
            {
                "backend/database/migrations/0002_widget.up.sql": (
                    "CREATE TABLE widgets (id integer PRIMARY KEY);\n"
                ),
                "backend/database/migrations/0002_widget.down.sql": (
                    "DROP TABLE users;\n"
                ),
            }
        )
        with self.assertRaisesRegex(AgentDeploymentError, "not created"):
            self._manager().deploy(claim)

    def test_down_migration_cannot_use_cascade(self):
        claim = self._prepare_deployment(
            {
                "backend/database/migrations/0002_widget.up.sql": (
                    "CREATE TABLE widgets (id integer PRIMARY KEY);\n"
                ),
                "backend/database/migrations/0002_widget.down.sql": (
                    "DROP TABLE widgets CASCADE;\n"
                ),
            }
        )
        with self.assertRaisesRegex(AgentDeploymentError, "CASCADE"):
            self._manager().deploy(claim)

    def test_down_migration_cannot_mutate_application_data(self):
        claim = self._prepare_deployment(
            {
                "backend/database/migrations/0002_widget.up.sql": (
                    "CREATE TABLE widgets (id integer PRIMARY KEY);\n"
                ),
                "backend/database/migrations/0002_widget.down.sql": (
                    "DROP TABLE widgets; UPDATE users SET active = false;\n"
                ),
            }
        )
        with self.assertRaisesRegex(AgentDeploymentError, "mutate application data"):
            self._manager().deploy(claim)

    def test_requirements_file_is_blocked(self):
        claim = self._prepare_deployment({"requirements.txt": "evil==1\n"})
        with self.assertRaisesRegex(AgentDeploymentError, "permits only"):
            self._manager().deploy(claim)

    def test_control_plane_file_is_blocked(self):
        claim = self._prepare_deployment(
            {"backend/core/agent_deployment.py": "CONTROL = False\n"}
        )
        with self.assertRaisesRegex(AgentDeploymentError, "protected control-plane"):
            self._manager().deploy(claim)

    def test_failed_implementation_test_evidence_is_rejected(self):
        claim = self._prepare_deployment(
            {"backend/example.py": "VALUE = 2\n"},
            tests=[
                {
                    "command": "python -m unittest",
                    "status": "failed",
                    "details": "one failed",
                }
            ],
        )
        with self.assertRaisesRegex(AgentDeploymentError, "failed implementation"):
            self._manager().deploy(claim)

    def test_validation_failure_does_not_stop_service_or_advance_target(self):
        claim = self._prepare_deployment(
            {"backend/example.py": "VALUE = 2\n"}
        )
        validator = FakeValidator(fail=True)
        with self.assertRaises(DeploymentValidationError):
            self._manager(validator=validator).deploy(claim)
        self.assertEqual(self.runtime.events, [])
        self.assertEqual(_git(self.target, "rev-parse", "qa-main"), self.base_commit)

    def test_failed_health_check_rolls_back_code_and_migration(self):
        claim = self._prepare_deployment(
            {
                "backend/database/migrations/0002_widget.up.sql": (
                    "CREATE TABLE widgets (id integer PRIMARY KEY);\n"
                ),
                "backend/database/migrations/0002_widget.down.sql": (
                    "DROP TABLE widgets;\n"
                ),
            }
        )
        database = FakeDatabase(pending=("0002",))
        runtime = FakeRuntime(fail_verify_times=1)
        manager = self._manager(database=database, runtime=runtime)

        with self.assertRaises(DeploymentRolledBackError):
            manager.deploy(claim)

        self.assertEqual(_git(self.target, "rev-parse", "qa-main"), self.base_commit)
        self.assertEqual(database.applied, [])
        self.assertTrue(any(event[0] == "restore" for event in runtime.events if isinstance(event, tuple)))
        manifest_path = self.deployment_artifacts / claim.card_id / f"{claim.id}.deployment.json"
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["attempts"][-1]["status"], "rolled_back")
        self.assertEqual(
            manifest["attempts"][-1]["migrations_rolled_back"], ["0002"]
        )

    def test_partial_migration_failure_is_detected_and_downgraded(self):
        claim = self._prepare_deployment(
            {
                "backend/database/migrations/0002_widget.up.sql": (
                    "CREATE TABLE widgets (id integer PRIMARY KEY);\n"
                ),
                "backend/database/migrations/0002_widget.down.sql": (
                    "DROP TABLE widgets;\n"
                ),
            }
        )
        database = FakeDatabase(
            pending=("0002",),
            fail_upgrade=True,
            apply_before_failure=True,
        )
        with self.assertRaises(DeploymentRolledBackError):
            self._manager(database=database).deploy(claim)
        self.assertEqual(database.applied, [])
        self.assertIn(("downgrade", ("0002",)), database.events)

    def test_incomplete_rollback_fails_closed(self):
        claim = self._prepare_deployment(
            {
                "backend/database/migrations/0002_widget.up.sql": (
                    "CREATE TABLE widgets (id integer PRIMARY KEY);\n"
                ),
                "backend/database/migrations/0002_widget.down.sql": (
                    "DROP TABLE widgets;\n"
                ),
            }
        )
        database = FakeDatabase(pending=("0002",), fail_downgrade=True)
        runtime = FakeRuntime(fail_verify_times=1)
        with self.assertRaises(DeploymentRollbackError):
            self._manager(database=database, runtime=runtime).deploy(claim)
        manifest_path = self.deployment_artifacts / claim.card_id / f"{claim.id}.deployment.json"
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["attempts"][-1]["status"], "rollback_failed")


    def test_ambiguous_start_is_forced_stopped_and_rolled_back(self):
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})
        runtime = FakeRuntime(fail_start_after_apply=True)

        with self.assertRaises(DeploymentRolledBackError):
            self._manager(runtime=runtime).deploy(claim)

        self.assertEqual(runtime.current_commit, self.base_commit)
        self.assertTrue(runtime.active)
        self.assertGreaterEqual(runtime.events.count("stop"), 2)
        self.assertEqual(_git(self.target, "rev-parse", "qa-main"), self.base_commit)

    def test_ambiguous_promotion_is_reconciled_and_rolled_back(self):
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})
        runtime = FakeRuntime(fail_promote_after_apply=True)

        with self.assertRaises(DeploymentRolledBackError):
            self._manager(runtime=runtime).deploy(claim)

        self.assertEqual(runtime.current_commit, self.base_commit)
        self.assertTrue(runtime.active)
        self.assertEqual(_git(self.target, "rev-parse", "qa-main"), self.base_commit)

    def test_ambiguous_source_sync_is_reconciled_in_production(self):
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})
        runtime = FakeRuntime(fail_sync_after_apply=True)

        with self.assertRaises(DeploymentRolledBackError):
            self._manager(runtime=runtime, environment="production").deploy(claim)

        self.assertEqual(runtime.sources_commit, self.base_commit)
        self.assertEqual(runtime.current_commit, self.base_commit)
        self.assertEqual(
            _git(self.target, "rev-parse", "production-main"),
            self.base_commit,
        )

    def test_retry_reuses_candidate_and_preserves_attempt_history(self):
        claim = self._prepare_deployment(
            {"backend/example.py": "VALUE = 2\n"}
        )
        runtime = FakeRuntime(fail_verify_times=1)
        manager = self._manager(runtime=runtime)
        with self.assertRaises(DeploymentRolledBackError):
            manager.deploy(claim)
        candidate = manager.deploy(claim)

        manifest = json.loads(Path(candidate.manifest_path).read_text())
        self.assertEqual(
            [attempt["status"] for attempt in manifest["attempts"]],
            ["rolled_back", "succeeded"],
        )
        self.assertEqual(_git(self.target, "rev-parse", "qa-main"), candidate.candidate_commit)

    def test_success_retry_is_idempotent(self):
        claim = self._prepare_deployment(
            {"backend/example.py": "VALUE = 2\n"}
        )
        manager = self._manager()
        first = manager.deploy(claim)
        events_after_first = list(self.runtime.events)
        second = manager.deploy(claim)

        self.assertEqual(first.candidate_commit, second.candidate_commit)
        self.assertEqual(self.runtime.events[:-1], events_after_first)
        self.assertEqual(self.runtime.events[-1], "verify")
        manifest = json.loads(Path(second.manifest_path).read_text())
        self.assertEqual(len(manifest["attempts"]), 1)

    def test_executor_turns_safe_rollback_into_retryable_block(self):
        claim = self._prepare_deployment(
            {"backend/example.py": "VALUE = 2\n"}
        )
        runtime = FakeRuntime(fail_verify_times=1)
        executor = GitBackendDeploymentExecutor(
            deployment_manager=self._manager(runtime=runtime),
            retry_after_seconds=90,
        )
        with self.assertRaises(AgentTemporarilyBlockedError) as raised:
            executor.execute(claim)
        self.assertEqual(raised.exception.retry_after_seconds, 90)

    def test_executor_rejects_non_backend_repository_scope(self):
        claim = self._prepare_deployment(
            {"backend/example.py": "VALUE = 2\n"}
        )
        executor = GitBackendDeploymentExecutor(
            deployment_manager=self._manager(),
            retry_after_seconds=90,
        )

        with self.assertRaisesRegex(ValueError, "backend-scoped"):
            executor.execute(
                replace(
                    claim,
                    repository_scope=RepositoryScope.BACKEND_AND_ANDROID,
                )
            )

    def test_production_requires_production_target_branch(self):
        claim = self._prepare_deployment(
            {"backend/example.py": "VALUE = 2\n"}
        )
        del claim
        with self.assertRaisesRegex(AgentWorkerConfigurationError, "production-main"):
            GitBackendDeploymentManager(
                environment="production",
                source_repository=self.source,
                source_worktree_root=self.source_worktrees,
                source_artifact_root=self.source_artifacts,
                target_repository=self.target,
                candidate_worktree_root=self.candidate_worktrees,
                deployment_artifact_root=self.deployment_artifacts,
                target_branch="qa-main",
                validator=self.validator,
                database=self.database,
                runtime=self.runtime,
            )


class DeploymentControlSynchronizationTests(unittest.TestCase):
    def test_planning_fetch_uses_verified_production_runtime(self):
        helper_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "libexec"
            / "remihub-backend-deployment-control"
        )
        loader = importlib.machinery.SourceFileLoader(
            "remihub_backend_deployment_control_test",
            str(helper_path),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = module
        try:
            loader.exec_module(module)
        finally:
            sys.modules.pop(loader.name, None)

        environment = module.ENVIRONMENTS["production"]
        candidate_branch = (
            "deployment/card-3d8549c4-a965-4d2e-aacf-9df7e6ccdbb4/r1"
        )
        candidate = "a" * 40
        base = "b" * 40
        rollback_ref = (
            "rollback-before-agent-card-"
            "3d8549c4-a965-4d2e-aacf-9df7e6ccdbb4-r1"
        )
        temporary_ref = f"refs/remihub-deployment/{candidate_branch}"

        def fake_resolve(repository, reference, *, user=None):
            del user
            if repository == environment.target_repo and reference in {
                f"refs/heads/{candidate_branch}",
                environment.target_branch,
            }:
                return candidate
            if repository == module.SOURCE_REPO and reference == temporary_ref:
                return candidate
            raise AssertionError((repository, reference))

        commands = []

        def fake_run(command, *, user=None, check=True):
            commands.append((command, user, check))
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(module, "resolve", side_effect=fake_resolve),
            patch.object(module, "run", side_effect=fake_run),
            patch.object(module, "require_source_state") as require_source_state,
            patch.object(module, "require_runtime_state") as require_runtime_state,
            patch.object(module, "harden_planning_checkout") as harden_planning,
        ):
            module.synchronize_sources(
                environment,
                [candidate_branch, candidate, base, rollback_ref],
            )

        require_source_state.assert_any_call(base)
        require_source_state.assert_any_call(candidate)
        require_runtime_state.assert_called_once_with(environment, candidate)
        harden_planning.assert_called_once_with(candidate)

        planning_fetches = [
            command
            for command, user, _ in commands
            if user == "alex"
            and command[:4]
            == [module.GIT, "-C", str(module.PLANNING_REPO), "fetch"]
        ]
        self.assertEqual(len(planning_fetches), 1)
        planning_fetch = planning_fetches[0]
        self.assertIn(str(environment.runtime), planning_fetch)
        self.assertIn(environment.runtime_branch, planning_fetch)
        self.assertNotIn(str(module.SOURCE_REPO), planning_fetch)

    def test_planning_hardening_restores_worker_readability_and_git_modes(self):
        helper_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "libexec"
            / "remihub-backend-deployment-control"
        )
        loader = importlib.machinery.SourceFileLoader(
            "remihub_backend_deployment_control_planning_modes_test",
            str(helper_path),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = module
        try:
            loader.exec_module(module)
        finally:
            sys.modules.pop(loader.name, None)

        with tempfile.TemporaryDirectory() as temporary_directory:
            planning = Path(temporary_directory) / "planning"
            planning.mkdir()
            _git(planning, "init", "-b", "main")
            _git(planning, "config", "user.name", "RemiHub Test")
            _git(planning, "config", "user.email", "remihub-test@invalid.local")
            (planning / "backend").mkdir()
            worker = planning / "backend" / "agent_worker.py"
            worker.write_text("VALUE = 1\n", encoding="utf-8")
            executable = planning / "deploy.sh"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            _git(planning, "add", ".")
            _git(planning, "commit", "-m", "Base")
            expected = _git(planning, "rev-parse", "HEAD")

            worker.chmod(0o600)
            executable.chmod(0o700)

            def local_run(command, *, user=None, check=True):
                del user
                result = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if check and result.returncode != 0:
                    raise subprocess.CalledProcessError(
                        result.returncode,
                        command,
                        result.stdout,
                        result.stderr,
                    )
                return result

            owner = SimpleNamespace(pw_uid=os.getuid())
            group = SimpleNamespace(gr_gid=os.getgid())
            with (
                patch.object(module, "PLANNING_REPO", planning),
                patch.object(module.pwd, "getpwnam", return_value=owner),
                patch.object(module.grp, "getgrnam", return_value=group),
                patch.object(module, "run", side_effect=local_run),
            ):
                module.harden_planning_checkout(expected)

            self.assertEqual(planning.stat().st_mode & 0o7777, 0o2750)
            self.assertEqual(worker.stat().st_mode & 0o7777, 0o640)
            self.assertEqual(executable.stat().st_mode & 0o7777, 0o750)
            self.assertEqual(
                (planning / ".git" / "config").stat().st_mode & 0o7777,
                0o640,
            )
            self.assertEqual(
                _git(planning, "status", "--porcelain=v1", "--untracked-files=no"),
                "",
            )

    def test_qa_runtime_hardening_preserves_git_modes_across_promote_and_restore(self):
        helper_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "libexec"
            / "remihub-backend-deployment-control"
        )
        loader = importlib.machinery.SourceFileLoader(
            "remihub_backend_deployment_control_modes_test",
            str(helper_path),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = module
        try:
            loader.exec_module(module)
        finally:
            sys.modules.pop(loader.name, None)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            target = root / "target.git"
            runtime = root / "runtime"
            source.mkdir()
            _git(source, "init")
            _git(source, "config", "user.name", "RemiHub Test")
            _git(source, "config", "user.email", "remihub-test@invalid.local")
            (source / "backend").mkdir()
            (source / "backend" / "regular.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            executable = source / "deploy.sh"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            _git(source, "add", ".")
            _git(source, "commit", "-m", "Base")
            base = _git(source, "rev-parse", "HEAD")

            subprocess.run(
                ["git", "clone", "--bare", str(source), str(target)],
                check=True,
                capture_output=True,
                text=True,
            )
            _git(target, "update-ref", "refs/heads/qa-main", base)
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--no-hardlinks",
                    "--no-checkout",
                    str(target),
                    str(runtime),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            _git(runtime, "checkout", "-b", "qa-runtime", base)

            environment = module.Environment(
                service="qa.service",
                target_repo=target,
                target_branch="qa-main",
                runtime=runtime,
                runtime_branch="qa-runtime",
                runtime_user="root",
            )
            group = SimpleNamespace(gr_gid=os.getgid())
            with (
                patch.object(module.grp, "getgrnam", return_value=group),
                patch.object(module.os, "chown"),
            ):
                module.harden_qa_runtime(environment, base)

                self.assertEqual(
                    (runtime / "backend" / "regular.py").stat().st_mode & 0o7777,
                    0o640,
                )
                self.assertEqual(
                    (runtime / "deploy.sh").stat().st_mode & 0o7777,
                    0o750,
                )
                self.assertEqual(
                    (runtime / "backend").stat().st_mode & 0o7777,
                    0o2750,
                )
                self.assertEqual(
                    _git(runtime, "status", "--porcelain=v1", "--untracked-files=no"),
                    "",
                )

                _git(source, "checkout", "-b", "candidate")
                (source / "backend" / "regular.py").write_text(
                    "VALUE = 2\n",
                    encoding="utf-8",
                )
                (source / "backend" / "new.py").write_text(
                    "NEW = True\n",
                    encoding="utf-8",
                )
                _git(source, "add", ".")
                _git(source, "commit", "-m", "Candidate")
                candidate = _git(source, "rev-parse", "HEAD")
                branch = (
                    "deployment/card-"
                    "22222222-2222-4222-8222-222222222222/r1"
                )
                _git(
                    source,
                    "push",
                    str(target),
                    f"HEAD:refs/heads/{branch}",
                )

                module.promote(
                    environment,
                    [
                        branch,
                        candidate,
                        base,
                        (
                            "rollback-before-agent-card-"
                            "22222222-2222-4222-8222-222222222222-r1"
                        ),
                    ],
                )
                self.assertEqual(
                    _git(runtime, "status", "--porcelain=v1", "--untracked-files=no"),
                    "",
                )
                self.assertEqual(
                    (runtime / "backend" / "regular.py").stat().st_mode & 0o7777,
                    0o640,
                )
                self.assertEqual(
                    (runtime / "backend" / "new.py").stat().st_mode & 0o7777,
                    0o640,
                )
                self.assertEqual(
                    (runtime / "deploy.sh").stat().st_mode & 0o7777,
                    0o750,
                )

                module.restore(environment, [candidate, base])
                self.assertEqual(
                    _git(runtime, "status", "--porcelain=v1", "--untracked-files=no"),
                    "",
                )
                self.assertFalse((runtime / "backend" / "new.py").exists())

    def test_installer_uses_git_mode_preserving_runtime_hardening(self):
        installer_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "install-package.sh"
        )
        installer = installer_path.read_text(encoding="utf-8")
        self.assertIn(
            'harden-runtime qa "$NEW_COMMIT"',
            installer,
        )
        self.assertNotIn(
            "find /opt/remihub-agent/deployment/qa/application -type f -exec chmod 0640",
            installer,
        )

    def test_qa_verifier_checks_runtime_cleanliness_at_each_transition(self):
        verifier_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "qa-verify.sh"
        )
        verifier = verifier_path.read_text(encoding="utf-8")
        self.assertGreaterEqual(verifier.count('"$HELPER" verify-runtime qa'), 5)
        self.assertIn("candidate-runtime-state.txt", verifier)
        self.assertIn("retry-runtime-state.txt", verifier)
        self.assertIn("restored-health-runtime-state.txt", verifier)

    def test_installer_finishes_with_owner_scoped_git_checks_and_qa_gate(self):
        installer_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "install-package.sh"
        )
        installer = installer_path.read_text(encoding="utf-8")
        self.assertNotIn('$(git -C "$PROD"', installer)
        self.assertNotIn('\n  git -C "$PROD"', installer)
        qa_verification = installer.index(
            'verify-runtime qa "$NEW_COMMIT"'
        )
        production_promotion = installer.index(
            'echo "[8/10] Promote the tested foundation commit and synchronize agent sources"'
        )
        self.assertLess(qa_verification, production_promotion)
        self.assertIn(
            '! systemctl is-active --quiet remihub-backend-qa.service',
            installer,
        )
        self.assertIn(
            'wait_for_service_stable remihub-agent-worker.service',
            installer,
        )
        self.assertIn(
            'wait_for_service_stable remihub-agent-implementation.service',
            installer,
        )

    def test_installer_qa_runtime_clone_cannot_reown_target_objects(self):
        installer_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "install-package.sh"
        )
        installer = installer_path.read_text(encoding="utf-8")
        self.assertIn(
            "git clone --no-hardlinks --no-checkout",
            installer,
        )
        self.assertIn(
            "git --git-dir=/opt/remihub-agent/deployment/qa/repository.git",
            installer,
        )
        self.assertIn(
            "fsck --full --strict",
            installer,
        )
        self.assertIn(
            "rev-parse 'qa-main^{commit}'",
            installer,
        )

    def test_no_hardlinks_clone_keeps_runtime_permissions_isolated(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            target = root / "target.git"
            runtime = root / "runtime"
            source.mkdir()
            _git(source, "init")
            _git(source, "config", "user.name", "RemiHub Test")
            _git(source, "config", "user.email", "remihub-test@invalid.local")
            (source / "backend.py").write_text("VALUE = 1\n", encoding="utf-8")
            _git(source, "add", "backend.py")
            _git(source, "commit", "-m", "Initial")
            subprocess.run(
                ["git", "clone", "--bare", str(source), str(target)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--no-hardlinks",
                    "--no-checkout",
                    str(target),
                    str(runtime),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            target_objects = target / "objects"
            runtime_objects = runtime / ".git" / "objects"
            common_objects = sorted(
                path.relative_to(target_objects)
                for path in target_objects.glob("[0-9a-f][0-9a-f]/*")
                if (runtime_objects / path.relative_to(target_objects)).is_file()
            )
            self.assertTrue(common_objects)
            relative_object = common_objects[0]
            target_object = target_objects / relative_object
            runtime_object = runtime_objects / relative_object
            self.assertNotEqual(
                target_object.stat().st_ino,
                runtime_object.stat().st_ino,
            )
            target_mode = target_object.stat().st_mode & 0o777
            runtime_object.chmod(0o600 if target_mode != 0o600 else 0o400)
            self.assertEqual(target_object.stat().st_mode & 0o777, target_mode)

    def test_installer_grants_only_execute_traversal_to_qa_runtime(self):
        installer_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "install-package.sh"
        )
        installer = installer_path.read_text(encoding="utf-8")
        for path in (
            "/opt/remihub-agent/deployment",
            "/opt/remihub-agent/deployment/config",
            "/opt/remihub-agent/deployment/qa",
        ):
            self.assertIn(
                f"install -d -o root -g root -m 0711 {path}",
                installer,
            )
        for path in (
            "/opt/remihub-agent/deployment",
            "/opt/remihub-agent/deployment/config",
            "/opt/remihub-agent/deployment/qa",
        ):
            self.assertIn(
                f"require_account_path remihub-qa-app -x {path}",
                installer,
            )
        self.assertIn(
            "require_account_path remihub-qa-app -r /opt/remihub-agent/deployment/qa/application/backend/main.py",
            installer,
        )
        self.assertIn(
            "Isolation probe passed: remihub-qa-app cannot read repository.git",
            installer,
        )
        self.assertNotIn(
            "usermod -a -G remihub-agent remihub-qa-app",
            installer,
        )

    def test_installer_checks_full_traversal_chain_before_repository_seeding(self):
        installer_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "install-package.sh"
        )
        installer = installer_path.read_text(encoding="utf-8")
        probe_index = installer.index(
            'echo "Validating deployment account traversal before repository seeding"'
        )
        seed_index = installer.index(
            'echo "[5/10] Seed isolated QA and production deployment repositories"'
        )
        self.assertLess(probe_index, seed_index)
        self.assertIn("Permission probe FAILED:", installer)
        self.assertIn('print_path_chain "$path" >&2', installer)
        self.assertIn(
            "require_account_path remihub-deployer -x /opt/remihub-agent/deployment",
            installer,
        )
        self.assertIn(
            "require_account_path remihub-qa-app -x /opt/remihub-agent/deployment",
            installer,
        )

    def test_installer_restores_deployment_qa_and_config_parent_metadata(self):
        installer_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "install-package.sh"
        )
        installer = installer_path.read_text(encoding="utf-8")
        self.assertIn("DEPLOYMENT_PARENT_OWNER", installer)
        self.assertIn("DEPLOYMENT_PARENT_MODE", installer)
        self.assertIn("QA_PARENT_EXISTED", installer)
        self.assertIn("CONFIG_PARENT_EXISTED", installer)
        self.assertIn(
            '>"$BACKUP/deployment-parent-before.txt"',
            installer,
        )
        self.assertIn(
            "/opt/remihub-agent/deployment/qa \\",
            installer,
        )
        self.assertIn(
            "/opt/remihub-agent/deployment/config \\",
            installer,
        )
        self.assertIn(
            'chmod "$DEPLOYMENT_PARENT_MODE" /opt/remihub-agent/deployment',
            installer,
        )

    def test_qa_runtime_unit_checks_paths_as_service_account(self):
        unit_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "systemd"
            / "remihub-backend-qa.service"
        )
        unit = unit_path.read_text(encoding="utf-8")
        self.assertIn(
            "ExecStartPre=/usr/bin/test -x /opt/remihub-agent/deployment",
            unit,
        )
        self.assertIn(
            "ExecStartPre=/usr/bin/test -x /opt/remihub-agent/deployment/qa",
            unit,
        )
        self.assertIn(
            "ExecStartPre=/usr/bin/test -x /opt/remihub-agent/deployment/config",
            unit,
        )
        self.assertIn(
            "ExecStartPre=/usr/bin/test -r /opt/remihub-agent/deployment/qa/application/backend/main.py",
            unit,
        )
        self.assertIn("User=remihub-qa-app", unit)

    def test_installer_completes_qa_proof_before_production_promotion(self):
        installer_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "install-package.sh"
        )
        installer = installer_path.read_text(encoding="utf-8")
        qa_index = installer.index(
            'echo "[7/10] Run complete QA validation before production promotion"'
        )
        promotion_index = installer.index(
            'echo "[8/10] Promote the tested foundation commit and synchronize agent sources"'
        )
        production_reset_index = installer.index(
            'runuser -u alex -- git -C "$PROD" reset --hard "$NEW_COMMIT"'
        )
        self.assertLess(qa_index, promotion_index)
        self.assertLess(promotion_index, production_reset_index)

    def test_qa_verifier_preserves_service_status_and_journal(self):
        verifier_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "qa-verify.sh"
        )
        verifier = verifier_path.read_text(encoding="utf-8")
        self.assertIn("capture_qa_diagnostics", verifier)
        self.assertIn("journalctl -u remihub-backend-qa.service", verifier)
        self.assertIn('>"$RECORD/$label-service-journal.log"', verifier)
        self.assertIn(
            'wait_for_qa_health "$RECORD/qa-openapi.json" "candidate-health"',
            verifier,
        )

    def test_installer_binds_versioned_postgresql_clients(self):
        installer_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "install-package.sh"
        )
        installer = installer_path.read_text(encoding="utf-8")
        self.assertIn(
            'candidate="/usr/lib/postgresql/$major/bin/$tool"',
            installer,
        )
        self.assertIn(
            'runuser -u remihub-deployer -- "$PG_DUMP_BINARY" --version',
            installer,
        )
        self.assertIn(
            'runuser -u remihub-deployer -- "$PG_RESTORE_BINARY" --version',
            installer,
        )
        self.assertNotIn(
            "REMIHUB_AGENT_DEPLOYMENT_PG_DUMP_BINARY=/usr/bin/pg_dump",
            installer,
        )

    def test_qa_verifier_uses_explicit_postgresql_clients_and_surfaces_log(self):
        verifier_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "qa-verify.sh"
        )
        verifier = verifier_path.read_text(encoding="utf-8")
        self.assertIn('PG_DUMP="${3:', verifier)
        self.assertIn('PG_RESTORE="${4:', verifier)
        self.assertIn("pg_dump_binary=pg_dump", verifier)
        self.assertIn("pg_restore_binary=pg_restore", verifier)
        self.assertIn(
            'cat "$RECORD/database-verification.log" >&2',
            verifier,
        )

    def test_installer_planning_fetch_uses_production_checkout(self):
        installer_path = (
            Path(__file__).resolve().parents[1]
            / "deployments"
            / "agent_backend"
            / "install-package.sh"
        )
        installer = installer_path.read_text(encoding="utf-8")
        self.assertIn(
            'git -C "$PLANNING" fetch --no-tags "$PROD" refs/heads/main',
            installer,
        )
        self.assertNotIn(
            'git -C "$PLANNING" fetch --no-tags "$SOURCE" refs/heads/main',
            installer,
        )
        self.assertIn(
            'harden-planning production "$NEW_COMMIT"',
            installer,
        )
        self.assertIn(
            'runuser -u remihub-agent -- /usr/bin/test -r "$PLANNING/backend/agent_worker.py"',
            installer,
        )
        self.assertIn(
            'wait_for_service_stable remihub-agent-worker.service',
            installer,
        )
        self.assertNotIn(
            'systemctl start remihub-agent-worker.service\n    systemctl is-active --quiet remihub-agent-worker.service',
            installer,
        )


if __name__ == "__main__":
    unittest.main()

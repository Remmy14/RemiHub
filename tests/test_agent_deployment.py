import importlib.machinery
import importlib.util
import json
import os
import sys
import subprocess
import tarfile
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.core.agent_deployment import (
    AgentDeploymentError,
    BackupEvidence,
    DeploymentRollbackError,
    DeploymentRolledBackError,
    DeploymentValidationError,
    FrontendArtifactEvidence,
    GitBackendDeploymentExecutor,
    GitBackendDeploymentManager,
    PostgresMigrationHistoryReader,
    PostgresDeploymentDatabase,
    RuntimeHealth,
    ValidationEvidence,
    _frontend_artifact_manifest,
    _write_deterministic_frontend_archive,
    verify_frontend_archive,
    verify_distinct_database_identities,
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
    def test_backend_failed_codex_test_is_preserved_as_advisory_evidence(self):
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

        observed = manager._validate_implementation_tests(metadata)

        self.assertEqual(
            observed,
            (
                {
                    "command": "python -m unittest",
                    "status": "failed",
                    "details": "one test failed",
                },
            ),
        )

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
        history=(),
        history_sequence=None,
        fail_upgrade=False,
        apply_before_failure=False,
        fail_downgrade=False,
    ):
        self.configured_pending = tuple(pending)
        self.configured_history = tuple(history)
        self.history_sequence = (
            [tuple(item) for item in history_sequence]
            if history_sequence is not None
            else None
        )
        self.applied = []
        self.fail_upgrade = fail_upgrade
        self.apply_before_failure = apply_before_failure
        self.fail_downgrade = fail_downgrade
        self.events = []

    def migration_history(self):
        self.events.append("history")
        if self.history_sequence is not None:
            if len(self.history_sequence) > 1:
                return self.history_sequence.pop(0)
            return self.history_sequence[0]
        return self.configured_history

    def database_identity(self):
        return ("database", "session_user", "current_user", "127.0.0.1", "5432")

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


class FakeHistoryReader:
    def __init__(self, history=(), *, identity=None):
        self.history = tuple(history)
        self.identity = identity or (
            "qa",
            "qa_reader",
            "qa_reader",
            "127.0.0.1",
            "5432",
        )
        self.events = []

    def migration_history(self):
        self.events.append("history")
        return self.history

    def database_identity(self):
        return self.identity


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
        self.frontend_installed = None

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

    def frontend_install(
        self,
        *,
        artifact_manifest,
        artifact_archive,
        artifact_identity,
        candidate_commit,
        card_id,
        deployment_run_id,
    ):
        self.events.append(("frontend_install", artifact_identity, candidate_commit))
        self.frontend_installed = artifact_identity
        return {
            "status": "installed",
            "artifact_identity": artifact_identity,
            "candidate_commit": candidate_commit,
        }

    def frontend_restore(self):
        self.events.append("frontend_restore")
        self.frontend_installed = None
        return {"status": "restored"}

    def frontend_verify(self, *, artifact_manifest, artifact_identity):
        self.events.append(("frontend_verify", artifact_identity))
        if self.frontend_installed != artifact_identity:
            raise AgentDeploymentError("frontend artifact mismatch")
        return {"status": "verified", "artifact_identity": artifact_identity}

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


class FakeGitHubSynchronizer:
    def __init__(
        self,
        *,
        fail_times: int = 0,
        failure_message: str = "GitHub unavailable",
        already_current: bool = False,
    ):
        self.fail_times = fail_times
        self.failure_message = failure_message
        self.already_current = already_current
        self.calls = []

    def synchronize(
        self,
        *,
        candidate_commit,
        base_commit,
        card_id,
        deployment_run_id,
    ):
        self.calls.append(
            {
                "candidate_commit": candidate_commit,
                "base_commit": base_commit,
                "card_id": card_id,
                "deployment_run_id": deployment_run_id,
            }
        )
        if self.fail_times:
            self.fail_times -= 1
            raise AgentDeploymentError(self.failure_message)
        return {
            "status": "verified",
            "candidate_commit": candidate_commit,
            "remote_before": candidate_commit if self.already_current else base_commit,
            "remote_after": candidate_commit,
            "push_return_code": "not_needed" if self.already_current else 0,
        }


class FakeFrontendBuilder:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def build(
        self,
        *,
        candidate_worktree,
        artifact_root,
        card_id,
        card_revision,
        deployment_run_id,
        approval_id,
        implementation_run_id,
        candidate_commit,
        changed_files,
    ):
        self.calls.append(tuple(changed_files))
        if not any(path.startswith("frontend-web/") for path in changed_files):
            return FrontendArtifactEvidence(changed=False)
        if self.fail:
            raise DeploymentValidationError("frontend build failed")
        artifact_dir = (
            artifact_root
            / card_id
            / deployment_run_id
            / "frontend-web"
            / candidate_commit
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = artifact_dir / "manifest.json"
        archive_path = artifact_dir / "dist.tar"
        manifest_path.write_text("{}", encoding="utf-8")
        archive_path.write_bytes(b"tar")
        return FrontendArtifactEvidence(
            changed=True,
            artifact_directory=str(artifact_dir),
            archive_path=str(archive_path),
            archive_sha256="d" * 64,
            manifest_path=str(manifest_path),
            manifest_sha256="e" * 64,
            artifact_identity="f" * 64,
            lockfile_sha256="a" * 64,
            node_version="v22.22.2",
            npm_version="10.9.7",
            commands=(
                {"command": "npm ci --ignore-scripts", "return_code": 0},
                {"command": "npm run lint", "return_code": 0},
                {"command": "npm run build", "return_code": 0},
            ),
            reproducibility={"matched": True},
        )


class FrontendArtifactDeterminismTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.dist = self.root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text("<script src=/assets/app.js></script>\n", encoding="utf-8")
        (self.dist / "assets" / "app.js").write_text("console.log('ok')\n", encoding="utf-8")

    def test_manifest_identity_is_sorted_and_content_based(self):
        manifest = _frontend_artifact_manifest(
            self.dist,
            candidate_commit="a" * 40,
            lockfile_sha256="b" * 64,
        )

        paths = [entry["path"] for entry in manifest["entries"]]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(manifest["artifact_identity"]), 64)
        self.assertNotIn("mtime", json.dumps(manifest))
        self.assertNotIn("uid", json.dumps(manifest))

    def test_deterministic_archive_normalizes_metadata_and_verifies_identity(self):
        manifest = _frontend_artifact_manifest(
            self.dist,
            candidate_commit="a" * 40,
            lockfile_sha256="b" * 64,
        )
        manifest_path = self.root / "manifest.json"
        archive_path = self.root / "dist.tar"
        manifest_path.write_bytes(
            (
                json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                + "\n"
            ).encode("utf-8")
        )

        _write_deterministic_frontend_archive(self.dist, archive_path, manifest)
        verified = verify_frontend_archive(
            archive_path=archive_path,
            manifest_path=manifest_path,
        )

        self.assertEqual(verified["artifact_identity"], manifest["artifact_identity"])
        with tarfile.open(archive_path) as archive:
            members = archive.getmembers()
        self.assertEqual([member.name for member in members], [entry["path"] for entry in manifest["entries"]])
        self.assertTrue(all(member.mtime == 0 for member in members))
        self.assertTrue(all(member.uid == 0 and member.gid == 0 for member in members))
        self.assertTrue(all(member.uname == "root" and member.gname == "root" for member in members))

    def test_archive_metadata_must_be_normalized_even_when_content_matches(self):
        manifest = _frontend_artifact_manifest(
            self.dist,
            candidate_commit="a" * 40,
            lockfile_sha256="b" * 64,
        )
        manifest_path = self.root / "manifest.json"
        archive_path = self.root / "bad.tar"
        manifest_path.write_bytes(
            (
                json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                + "\n"
            ).encode("utf-8")
        )
        with tarfile.open(archive_path, "w") as archive:
            for entry in manifest["entries"]:
                source = self.dist / entry["path"]
                info = archive.gettarinfo(str(source), arcname=entry["path"])
                info.mtime = 123
                if entry["type"] == "directory":
                    archive.addfile(info)
                else:
                    with source.open("rb") as file_object:
                        archive.addfile(info, file_object)

        with self.assertRaisesRegex(
            DeploymentValidationError,
            "non-deterministic timestamps",
        ):
            verify_frontend_archive(
                archive_path=archive_path,
                manifest_path=manifest_path,
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

    def test_read_only_history_reader_rejects_invalid_role(self):
        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "QA_PARITY_DATABASE_ROLE",
        ):
            PostgresMigrationHistoryReader(
                config_path=self.config,
                role="reader; DROP",
            )

    def test_history_reader_uses_read_only_transaction_and_sanitized_rows(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (True,)
        cursor.fetchall.return_value = [
            ("0001", "initial", "a" * 64, datetime.now(timezone.utc)),
        ]

        with patch(
            "backend.core.agent_deployment._connect_explicit_database_config",
            return_value=connection,
        ):
            history = PostgresMigrationHistoryReader(
                config_path=self.config,
                role="remihub_qa_migration_reader",
            ).migration_history()

        self.assertEqual(
            history,
            ({"version": "0001", "name": "initial", "checksum": "a" * 64},),
        )
        executed = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("SET ROLE remihub_qa_migration_reader", executed)
        self.assertIn("SET TRANSACTION READ ONLY", executed)

    def test_same_database_identity_is_rejected(self):
        first = FakeHistoryReader(identity=("remihub", "u", "u", "127.0.0.1", "5432"))
        second = FakeHistoryReader(identity=("remihub", "u", "u", "127.0.0.1", "5432"))

        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "must be distinct",
        ):
            verify_distinct_database_identities(first, second)


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
        self.database = FakeDatabase(history=self._expected_history())
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
        frontend_builder=None,
        qa_history_reader=None,
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
            frontend_builder=frontend_builder or FakeFrontendBuilder(),
            qa_history_reader=(
                qa_history_reader
                if qa_history_reader is not None
                else (
                    FakeHistoryReader(self._expected_history())
                    if environment == "production"
                    else None
                )
            ),
        )

    def _expected_history(self, migrations_dir=None):
        if migrations_dir is None:
            migrations_dir = self.seed / "backend" / "database" / "migrations"
        from backend.core.agent_deployment import _expected_migration_history

        return _expected_migration_history(Path(migrations_dir))

    def test_git_commands_use_protected_fixed_safe_directory_config(self):
        environment = self._manager()._git_environment()

        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(
            environment["GIT_CONFIG_GLOBAL"],
            "/opt/remihub-agent/deployment/config/git-safe-directory.ini",
        )

    def test_git_commands_add_exact_dynamic_repository_safe_directory(self):
        from backend.core.agent_deployment import _exact_git_command

        repository = self.source_worktrees / "card-dynamic-worktree"
        repository.mkdir()
        resolved = repository.resolve()
        command = _exact_git_command(
            "git", repository, ("status", "--porcelain=v1")
        )

        self.assertEqual(
            command[:5],
            ["git", "-c", f"safe.directory={resolved}", "-C", str(resolved)],
        )
        self.assertNotIn("safe.directory=*", command)

    def test_git_byte_commands_add_exact_dynamic_repository_safe_directory(self):
        from backend.core.agent_deployment import _exact_git_command

        repository = self.source_worktrees / "card-dynamic-byte-worktree"
        repository.mkdir()
        resolved = repository.resolve()
        command = _exact_git_command(
            "git", repository, ("show", "HEAD:file.bin")
        )

        self.assertEqual(
            command[:5],
            ["git", "-c", f"safe.directory={resolved}", "-C", str(resolved)],
        )
        self.assertNotIn("safe.directory=*", command)

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
        database = FakeDatabase(
            pending=("0002",),
            history=self._expected_history(
                self.implementation_workspace.path
                / "backend"
                / "database"
                / "migrations"
            ),
        )
        candidate = self._manager(database=database).deploy(claim)

        self.assertEqual(candidate.migrations_applied, ("0002",))
        self.assertIsNotNone(candidate.database_backup)
        self.assertIn(("upgrade", ("0002",)), database.events)
        backup_index = next(
            index for index, event in enumerate(database.events) if event[0] == "backup"
        )
        upgrade_index = database.events.index(("upgrade", ("0002",)))
        self.assertLess(backup_index, upgrade_index)

    def test_production_refuses_when_qa_missing_candidate_migration_before_mutation(self):
        changes = {}
        for version in range(2, 7):
            changes[f"backend/database/migrations/{version:04d}_step_{version}.up.sql"] = (
                f"CREATE TABLE step_{version} (id integer PRIMARY KEY);\n"
            )
            changes[
                f"backend/database/migrations/{version:04d}_step_{version}.down.sql"
            ] = f"DROP TABLE step_{version};\n"
        claim = self._prepare_deployment(changes)
        expected = self._expected_history(
            self.implementation_workspace.path / "backend" / "database" / "migrations"
        )
        database = FakeDatabase(pending=("0002", "0003", "0004", "0005", "0006"))
        runtime = FakeRuntime()
        qa_reader = FakeHistoryReader(expected[:-1])

        with self.assertRaisesRegex(AgentDeploymentError, "missing=\\('0006'"):
            self._manager(
                database=database,
                runtime=runtime,
                qa_history_reader=qa_reader,
                environment="production",
            ).deploy(claim)

        self.assertEqual(runtime.events, [])
        self.assertEqual(database.events, [])
        self.assertEqual(
            _git(self.target, "rev-parse", "production-main"),
            self.base_commit,
        )
        manifest_path = self.deployment_artifacts / claim.card_id / f"{claim.id}.deployment.json"
        manifest = json.loads(manifest_path.read_text())
        attempt = manifest["attempts"][-1]
        self.assertEqual(attempt["status"], "failed_migration_parity")
        self.assertEqual(
            [item["version"] for item in manifest["migration_plan"]["expected_history"]],
            ["0001", "0002", "0003", "0004", "0005", "0006"],
        )

    def test_production_aligned_qa_history_permits_continued_execution(self):
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})
        expected = self._expected_history()
        qa_reader = FakeHistoryReader(expected)

        candidate = self._manager(
            qa_history_reader=qa_reader,
            environment="production",
        ).deploy(claim)

        self.assertEqual(
            _git(self.target, "rev-parse", "production-main"),
            candidate.candidate_commit,
        )
        self.assertIn("history", qa_reader.events)

    def test_production_refuses_qa_name_mismatch(self):
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})
        expected = self._expected_history()
        observed = ({**expected[0], "name": "different_name"},)

        with self.assertRaisesRegex(AgentDeploymentError, "name_mismatch=\\('0001'"):
            self._manager(
                qa_history_reader=FakeHistoryReader(observed),
                environment="production",
            ).deploy(claim)

    def test_production_refuses_qa_checksum_mismatch(self):
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})
        expected = self._expected_history()
        observed = ({**expected[0], "checksum": "0" * 64},)

        with self.assertRaisesRegex(AgentDeploymentError, "checksum_mismatch=\\('0001'"):
            self._manager(
                qa_history_reader=FakeHistoryReader(observed),
                environment="production",
            ).deploy(claim)

    def test_production_refuses_unexpected_qa_migration(self):
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})
        expected = self._expected_history()
        observed = expected + (
            {"version": "9999", "name": "future", "checksum": "f" * 64},
        )

        with self.assertRaisesRegex(AgentDeploymentError, "unexpected=\\('9999'"):
            self._manager(
                qa_history_reader=FakeHistoryReader(observed),
                environment="production",
            ).deploy(claim)

    def test_no_new_migration_candidate_still_enforces_complete_qa_parity(self):
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})

        with self.assertRaisesRegex(AgentDeploymentError, "missing=\\('0001'"):
            self._manager(
                qa_history_reader=FakeHistoryReader(()),
                environment="production",
            ).deploy(claim)

    def test_qa_history_is_rechecked_after_candidate_validation(self):
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})
        expected = self._expected_history()
        database = FakeDatabase(
            pending=(),
            history=(),
        )

        with self.assertRaises(DeploymentRolledBackError):
            self._manager(database=database).deploy(claim)

        self.assertIn("verify", self.runtime.events)
        manifest_path = self.deployment_artifacts / claim.card_id / f"{claim.id}.deployment.json"
        manifest = json.loads(manifest_path.read_text())
        attempt = manifest["attempts"][-1]
        self.assertEqual(attempt["failure_stage"], "qa_history_after_validation")
        self.assertEqual(attempt["status"], "rolled_back")

    def test_production_history_is_rechecked_after_production_validation(self):
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})
        expected = self._expected_history()
        database = FakeDatabase(history=expected)

        candidate = self._manager(
            database=database,
            qa_history_reader=FakeHistoryReader(expected),
            environment="production",
        ).deploy(claim)

        self.assertEqual(candidate.target_after, candidate.candidate_commit)
        self.assertIn("history", database.events)

    def test_post_deployment_production_history_mismatch_rolls_back(self):
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})
        expected = self._expected_history()
        database = FakeDatabase(history=())
        runtime = FakeRuntime()

        with self.assertRaises(DeploymentRolledBackError):
            self._manager(
                database=database,
                runtime=runtime,
                qa_history_reader=FakeHistoryReader(expected),
                environment="production",
            ).deploy(claim)

        self.assertEqual(runtime.current_commit, self.base_commit)
        self.assertEqual(
            _git(self.target, "rev-parse", "production-main"),
            self.base_commit,
        )
        manifest_path = self.deployment_artifacts / claim.card_id / f"{claim.id}.deployment.json"
        manifest = json.loads(manifest_path.read_text())
        attempt = manifest["attempts"][-1]
        self.assertEqual(attempt["status"], "rolled_back")
        self.assertEqual(
            attempt["failure_stage"],
            "production_history_after_validation",
        )

    def test_migration_parity_evidence_omits_protected_values(self):
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})
        expected = self._expected_history()
        candidate = self._manager(
            qa_history_reader=FakeHistoryReader(expected),
            environment="production",
        ).deploy(claim)

        manifest = json.loads(Path(candidate.manifest_path).read_text())
        serialized = json.dumps(manifest)
        self.assertIn("qa_history_before_production_mutation", serialized)
        self.assertIn("production_history_after_validation", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("migrator.ini", serialized)
        self.assertNotIn("password", serialized.lower())

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

    def test_frontend_safe_source_file_is_accepted_and_installed(self):
        frontend_builder = FakeFrontendBuilder()
        claim = self._prepare_deployment(
            {"frontend-web/src/NewScreen.tsx": "export const value = 1;\n"}
        )

        candidate = self._manager(frontend_builder=frontend_builder).deploy(claim)

        self.assertEqual(frontend_builder.calls, [("frontend-web/src/NewScreen.tsx",)])
        self.assertIsNotNone(candidate.frontend_artifact)
        self.assertIn(("frontend_install", "f" * 64, candidate.candidate_commit), self.runtime.events)
        self.assertIn(("frontend_verify", "f" * 64), self.runtime.events)
        manifest = json.loads(Path(candidate.manifest_path).read_text())
        self.assertTrue(manifest["frontend_artifact"]["changed"])
        self.assertEqual(
            manifest["attempts"][-1]["frontend_install"]["artifact_identity"],
            "f" * 64,
        )

    def test_frontend_dist_and_node_modules_are_rejected(self):
        for path in (
            "frontend-web/dist/index.html",
            "frontend-web/node_modules/react/index.js",
        ):
            with self.subTest(path=path):
                claim = self._prepare_deployment({path: "generated\n"})
                with self.assertRaisesRegex(
                    AgentDeploymentError,
                    "generated dependency or dist",
                ):
                    self._manager().deploy(claim)

    def test_frontend_env_and_secret_like_files_are_rejected(self):
        for path in (
            "frontend-web/.env",
            "frontend-web/src/firebase-token.txt",
            "frontend-web/public/private-key.pem",
        ):
            with self.subTest(path=path):
                claim = self._prepare_deployment({path: "secret\n"})
                with self.assertRaisesRegex(
                    AgentDeploymentError,
                    "environment files|secret-like",
                ):
                    self._manager().deploy(claim)

    def test_backend_only_candidate_does_not_require_frontend_artifact(self):
        frontend_builder = FakeFrontendBuilder()
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})

        candidate = self._manager(frontend_builder=frontend_builder).deploy(claim)

        self.assertIsNone(candidate.frontend_artifact)
        self.assertEqual(frontend_builder.calls, [("backend/example.py",)])
        self.assertNotIn("frontend_install", [event for event in self.runtime.events if isinstance(event, str)])
        manifest = json.loads(Path(candidate.manifest_path).read_text())
        self.assertFalse(manifest["frontend_artifact"]["changed"])

    def test_frontend_build_failure_occurs_before_runtime_mutation(self):
        claim = self._prepare_deployment(
            {"frontend-web/src/Broken.tsx": "export const broken = true;\n"}
        )

        with self.assertRaisesRegex(DeploymentValidationError, "frontend build failed"):
            self._manager(frontend_builder=FakeFrontendBuilder(fail=True)).deploy(claim)

        self.assertEqual(self.runtime.events, [])
        self.assertEqual(_git(self.target, "rev-parse", "qa-main"), self.base_commit)

    def test_frontend_failure_after_install_restores_backend_and_frontend(self):
        claim = self._prepare_deployment(
            {"frontend-web/src/NewScreen.tsx": "export const value = 1;\n"}
        )
        runtime = FakeRuntime(fail_verify_times=1)

        with self.assertRaises(DeploymentRolledBackError):
            self._manager(runtime=runtime).deploy(claim)

        self.assertIn("frontend_restore", runtime.events)
        self.assertEqual(runtime.current_commit, self.base_commit)
        self.assertEqual(_git(self.target, "rev-parse", "qa-main"), self.base_commit)

    def test_failed_codex_test_evidence_runs_authoritative_validation(self):
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

        candidate = self._manager().deploy(claim)

        self.assertEqual(len(self.validator.calls), 1)
        self.assertEqual(
            _git(self.target, "rev-parse", "qa-main"),
            candidate.candidate_commit,
        )
        manifest = json.loads(Path(candidate.manifest_path).read_text())
        self.assertEqual(
            manifest["implementation_tests"],
            [
                {
                    "command": "python -m unittest",
                    "status": "failed",
                    "details": "one failed",
                }
            ],
        )
        self.assertEqual(manifest["attempts"][-1]["status"], "succeeded")

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

    def test_production_executor_records_verified_github_sync(self):
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})
        synchronizer = FakeGitHubSynchronizer()
        executor = GitBackendDeploymentExecutor(
            deployment_manager=self._manager(environment="production"),
            github_synchronizer=synchronizer,
            retry_after_seconds=90,
        )

        result = executor.execute(claim)

        self.assertEqual(result.card_status, CardStatus.COMPLETED)
        self.assertEqual(result.metadata["github_sync"]["status"], "verified")
        self.assertEqual(len(synchronizer.calls), 1)
        manifest_path = Path(result.metadata["candidate"]["manifest_path"])
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["attempts"][-1]["status"], "succeeded")
        self.assertEqual(manifest["attempts"][-1]["stage"], "github_synchronized")
        self.assertEqual(
            manifest["github_sync"]["remote_after"],
            result.metadata["candidate"]["candidate_commit"],
        )

    def test_github_failure_blocks_without_redeploy_and_retry_only_resynchronizes(self):
        claim = self._prepare_deployment({"backend/example.py": "VALUE = 2\n"})
        runtime = FakeRuntime()
        synchronizer = FakeGitHubSynchronizer(fail_times=1)
        executor = GitBackendDeploymentExecutor(
            deployment_manager=self._manager(
                environment="production",
                runtime=runtime,
            ),
            github_synchronizer=synchronizer,
            retry_after_seconds=90,
        )

        with self.assertRaises(AgentTemporarilyBlockedError) as raised:
            executor.execute(claim)
        self.assertEqual(raised.exception.retry_after_seconds, 90)
        events_after_local_success = list(runtime.events)
        self.assertIn("stop", events_after_local_success)
        self.assertTrue(
            any(
                isinstance(event, tuple) and event[0] == "promote"
                for event in events_after_local_success
            )
        )
        self.assertTrue(
            any(
                isinstance(event, tuple) and event[0] == "sync"
                for event in events_after_local_success
            )
        )

        manifest_path = self.deployment_artifacts / claim.card_id / f"{claim.id}.deployment.json"
        pending_manifest = json.loads(manifest_path.read_text())
        self.assertEqual(pending_manifest["attempts"][-1]["status"], "succeeded")
        self.assertEqual(pending_manifest["attempts"][-1]["stage"], "github_sync_pending")
        self.assertEqual(pending_manifest["github_sync"]["status"], "pending")

        result = executor.execute(claim)

        self.assertEqual(result.card_status, CardStatus.COMPLETED)
        self.assertEqual(len(synchronizer.calls), 2)
        self.assertEqual(runtime.events[:-1], events_after_local_success)
        self.assertEqual(runtime.events[-1], "verify")
        completed_manifest = json.loads(manifest_path.read_text())
        self.assertEqual(len(completed_manifest["attempts"]), 1)
        self.assertEqual(completed_manifest["attempts"][-1]["stage"], "github_synchronized")
        self.assertEqual(completed_manifest["github_sync"]["status"], "verified")






    def test_production_executor_requires_github_synchronizer(self):
        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "requires a GitHub synchronizer",
        ):
            GitBackendDeploymentExecutor(
                deployment_manager=self._manager(environment="production"),
                retry_after_seconds=90,
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
                frontend_backup_root=root / "frontend-backups",
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

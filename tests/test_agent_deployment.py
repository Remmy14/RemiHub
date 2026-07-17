import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

from backend.core.agent_deployment import (
    AgentDeploymentError,
    GitQaDeploymentExecutor,
    GitQaDeploymentManager,
)
from backend.core.agent_state import CardStatus, RunPhase
from backend.core.agent_worker import (
    AgentWorkerConfigurationError,
    DeploymentSource,
)
from backend.core.agent_workspace import GitImplementationWorkspaceManager
from backend.services.agent_service import (
    AgentStateConflictError,
    _deployment_implementation_result,
)
from tests.test_agent_worker import claimed_run


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class GitQaDeploymentManagerTests(unittest.TestCase):
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
        (self.seed / "docs").mkdir()
        (self.seed / "docs" / "existing.md").write_text(
            "# Existing\n",
            encoding="utf-8",
        )
        _git(self.seed, "add", "docs/existing.md")
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
        _git(
            self.target,
            "update-ref",
            "refs/heads/qa-main",
            self.base_commit,
        )

        self.implementation_claim = claimed_run(phase=RunPhase.IMPLEMENTATION)
        self.implementation_manager = GitImplementationWorkspaceManager(
            source_repository=self.source,
            worktree_root=self.source_worktrees,
            artifact_root=self.source_artifacts,
        )
        self.persisted = []
        with self.implementation_manager.locked_workspace(
            self.implementation_claim,
            persist_workspace=lambda branch, path: self.persisted.append(
                (branch, path)
            ),
        ) as workspace:
            self.implementation_workspace = workspace
            (workspace.path / "docs" / "deployment-smoke.md").write_text(
                "# QA deployment smoke\n",
                encoding="utf-8",
            )
            self.snapshot = self.implementation_manager.capture_snapshot(
                self.implementation_claim,
                workspace,
            )

        metadata = {
            "executor": "codex_implementation",
            "phase": "implementation",
            "workspace": {
                "artifact_patch": str(self.snapshot.patch_path),
                "base_branch": self.implementation_workspace.base_branch,
                "base_commit": self.implementation_workspace.base_commit,
                "branch": self.snapshot.branch,
                "changed_files": list(self.snapshot.changed_files),
                "diff_stat": self.snapshot.diff_stat,
                "head_commit": self.snapshot.head_commit,
                "patch_size_bytes": self.snapshot.patch_size_bytes,
                "status_porcelain": self.snapshot.status_porcelain,
                "worktree_path": str(self.implementation_workspace.path),
            },
        }
        self.deployment_claim = replace(
            claimed_run(phase=RunPhase.DEPLOYMENT),
            id="7ce86bc5-59db-4c98-ac77-bd6038098e17",
            card_id=self.implementation_claim.card_id,
            feature_branch=self.snapshot.branch,
            worktree_path=str(self.implementation_workspace.path),
            deployment_source=DeploymentSource(
                approval_id="db62f682-713c-4516-a81f-c3c884c97bdc",
                implementation_run_id=self.implementation_claim.id,
                implementation_result_metadata=metadata,
            ),
        )
        self.manager = GitQaDeploymentManager(
            source_repository=self.source,
            source_worktree_root=self.source_worktrees,
            source_artifact_root=self.source_artifacts,
            target_repository=self.target,
            candidate_worktree_root=self.candidate_worktrees,
            deployment_artifact_root=self.deployment_artifacts,
            target_branch="qa-main",
        )

    def test_requires_exact_qa_target_branch(self):
        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "must be qa-main",
        ):
            GitQaDeploymentManager(
                source_repository=self.source,
                source_worktree_root=self.source_worktrees,
                source_artifact_root=self.source_artifacts,
                target_repository=self.target,
                candidate_worktree_root=self.candidate_worktrees,
                deployment_artifact_root=self.deployment_artifacts,
                target_branch="main",
            )

    def test_rejects_target_repository_with_remote(self):
        _git(
            self.target,
            "remote",
            "add",
            "origin",
            "ssh://example.invalid/remihub.git",
        )

        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "must not have Git remotes",
        ):
            GitQaDeploymentManager(
                source_repository=self.source,
                source_worktree_root=self.source_worktrees,
                source_artifact_root=self.source_artifacts,
                target_repository=self.target,
                candidate_worktree_root=self.candidate_worktrees,
                deployment_artifact_root=self.deployment_artifacts,
            )

    def test_rejects_implementation_repository_as_target(self):
        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "separate from implementation",
        ):
            GitQaDeploymentManager(
                source_repository=self.source,
                source_worktree_root=self.source_worktrees,
                source_artifact_root=self.source_artifacts,
                target_repository=self.source,
                candidate_worktree_root=self.candidate_worktrees,
                deployment_artifact_root=self.deployment_artifacts,
            )

    def test_creates_immutable_candidate_and_fast_forwards_qa_target(self):
        candidate = self.manager.deploy(self.deployment_claim)

        self.assertEqual(candidate.base_commit, self.base_commit)
        self.assertEqual(candidate.target_before, self.base_commit)
        self.assertEqual(candidate.target_after, candidate.candidate_commit)
        self.assertEqual(
            _git(self.target, "rev-parse", "qa-main"),
            candidate.candidate_commit,
        )
        self.assertEqual(
            _git(self.target, "rev-parse", f"{candidate.candidate_commit}^"),
            self.base_commit,
        )
        self.assertEqual(
            candidate.changed_files,
            ("docs/deployment-smoke.md",),
        )
        self.assertTrue(Path(candidate.manifest_path).is_file())
        manifest = json.loads(Path(candidate.manifest_path).read_text())
        self.assertEqual(manifest["candidate_commit"], candidate.candidate_commit)
        self.assertFalse(manifest["service_restart_performed"])
        self.assertEqual(manifest["migrations_applied"], [])
        self.assertIn(
            "?? docs/deployment-smoke.md",
            _git(self.implementation_workspace.path, "status", "--short"),
        )

    def test_retry_reuses_same_candidate_commit(self):
        first = self.manager.deploy(self.deployment_claim)
        second = self.manager.deploy(self.deployment_claim)

        self.assertEqual(second.candidate_commit, first.candidate_commit)
        self.assertEqual(second.target_before, self.base_commit)
        self.assertEqual(second.target_after, first.candidate_commit)

    def test_tracked_document_change_preserves_porcelain_leading_space(self):
        (self.implementation_workspace.path / "docs" / "existing.md").write_text(
            "# Updated existing\n",
            encoding="utf-8",
        )
        refreshed = self.implementation_manager.capture_snapshot(
            self.implementation_claim,
            self.implementation_workspace,
        )
        metadata = dict(
            self.deployment_claim.deployment_source.implementation_result_metadata
        )
        metadata["workspace"] = {
            **metadata["workspace"],
            "artifact_patch": str(refreshed.patch_path),
            "changed_files": list(refreshed.changed_files),
            "diff_stat": refreshed.diff_stat,
            "patch_size_bytes": refreshed.patch_size_bytes,
            "status_porcelain": refreshed.status_porcelain,
        }
        claim = replace(
            self.deployment_claim,
            deployment_source=replace(
                self.deployment_claim.deployment_source,
                implementation_result_metadata=metadata,
            ),
        )

        candidate = self.manager.deploy(claim)

        self.assertEqual(
            candidate.changed_files,
            ("docs/deployment-smoke.md", "docs/existing.md"),
        )

    def test_changed_implementation_after_approval_fails_closed(self):
        smoke_path = (
            self.implementation_workspace.path / "docs" / "deployment-smoke.md"
        )
        smoke_path.write_text(
            "changed after approval\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            AgentDeploymentError,
            "patch changed|status changed",
        ):
            self.manager.deploy(self.deployment_claim)

        self.assertEqual(_git(self.target, "rev-parse", "qa-main"), self.base_commit)

    def test_non_documentation_change_is_rejected(self):
        backend_file = self.implementation_workspace.path / "backend.py"
        backend_file.write_text("unsafe = True\n", encoding="utf-8")
        refreshed = self.implementation_manager.capture_snapshot(
            self.implementation_claim,
            self.implementation_workspace,
        )
        metadata = dict(
            self.deployment_claim.deployment_source.implementation_result_metadata
        )
        metadata["workspace"] = {
            **metadata["workspace"],
            "artifact_patch": str(refreshed.patch_path),
            "changed_files": list(refreshed.changed_files),
            "diff_stat": refreshed.diff_stat,
            "patch_size_bytes": refreshed.patch_size_bytes,
            "status_porcelain": refreshed.status_porcelain,
        }
        claim = replace(
            self.deployment_claim,
            deployment_source=replace(
                self.deployment_claim.deployment_source,
                implementation_result_metadata=metadata,
            ),
        )

        with self.assertRaisesRegex(AgentDeploymentError, "docs/\\*\\.md"):
            self.manager.deploy(claim)

    def test_executor_stops_at_completed_without_restart_or_migration(self):
        result = GitQaDeploymentExecutor(
            deployment_manager=self.manager
        ).execute(self.deployment_claim)

        self.assertEqual(result.card_status, CardStatus.COMPLETED)
        self.assertEqual(result.metadata["environment"], "qa")
        candidate = result.metadata["candidate"]
        self.assertFalse(candidate["service_restart_performed"])
        self.assertEqual(candidate["migrations_applied"], ())


class DeploymentApprovalEvidenceTests(unittest.TestCase):
    def cursor(self, row):
        cursor = MagicMock()
        cursor.description = [("id",), ("result_metadata",)]
        cursor.fetchone.return_value = row
        return cursor

    def test_requires_successful_implementation_result(self):
        with self.assertRaisesRegex(AgentStateConflictError, "successful"):
            _deployment_implementation_result(
                self.cursor(None),
                card_id="card",
                card_revision=1,
            )

    def test_requires_complete_workspace_evidence(self):
        with self.assertRaisesRegex(AgentStateConflictError, "incomplete"):
            _deployment_implementation_result(
                self.cursor(("run", {"phase": "implementation"})),
                card_id="card",
                card_revision=1,
            )

    def test_accepts_implementation_workspace_evidence(self):
        workspace = {
            "artifact_patch": "/tmp/run.patch",
            "base_branch": "main",
            "base_commit": "a" * 40,
            "branch": "agent/card-card",
            "changed_files": ["docs/example.md"],
            "head_commit": "a" * 40,
            "patch_size_bytes": 10,
            "status_porcelain": "?? docs/example.md",
            "worktree_path": "/tmp/card",
        }
        result = _deployment_implementation_result(
            self.cursor(
                (
                    "run",
                    {"phase": "implementation", "workspace": workspace},
                )
            ),
            card_id="card",
            card_revision=1,
        )

        self.assertEqual(result["id"], "run")


if __name__ == "__main__":
    unittest.main()

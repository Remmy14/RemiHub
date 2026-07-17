import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from backend.core.agent_state import RunPhase
from backend.core.agent_workspace import (
    AgentWorkspaceError,
    GitImplementationWorkspaceManager,
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


class GitImplementationWorkspaceManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.seed = self.root / "seed"
        self.source = self.root / "implementation.git"
        self.worktrees = self.root / "worktrees"
        self.artifacts = self.root / "artifacts"
        self.seed.mkdir()
        self.worktrees.mkdir()
        self.artifacts.mkdir()

        subprocess.run(
            ["git", "init", "-b", "main", str(self.seed)],
            check=True,
            capture_output=True,
        )
        _git(self.seed, "config", "user.name", "RemiHub Test")
        _git(self.seed, "config", "user.email", "remihub@example.invalid")
        (self.seed / "tracked.txt").write_text("before\n", encoding="utf-8")
        _git(self.seed, "add", "tracked.txt")
        _git(self.seed, "commit", "-m", "Initial")
        subprocess.run(
            ["git", "clone", "--bare", str(self.seed), str(self.source)],
            check=True,
            capture_output=True,
        )

        self.manager = GitImplementationWorkspaceManager(
            source_repository=self.source,
            worktree_root=self.worktrees,
            artifact_root=self.artifacts,
        )
        self.claim = claimed_run(phase=RunPhase.IMPLEMENTATION)

    def test_creates_deterministic_workspace_and_persists_metadata(self):
        persisted = []

        with self.manager.locked_workspace(
            self.claim,
            persist_workspace=lambda branch, path: persisted.append((branch, path)),
        ) as workspace:
            self.assertEqual(
                workspace.feature_branch,
                f"agent/card-{self.claim.card_id}",
            )
            self.assertEqual(
                workspace.path,
                self.worktrees / f"card-{self.claim.card_id}",
            )
            self.assertEqual(
                _git(workspace.path, "branch", "--show-current"),
                workspace.feature_branch,
            )

        self.assertEqual(
            persisted,
            [(workspace.feature_branch, str(workspace.path))],
        )

    def test_recovery_reuses_worktree_created_before_metadata_persistence(self):
        def fail_persistence(_branch, _path):
            raise RuntimeError("lease lost")

        with self.assertRaisesRegex(RuntimeError, "lease lost"):
            with self.manager.locked_workspace(
                self.claim,
                persist_workspace=fail_persistence,
            ):
                self.fail("workspace must not be yielded before persistence")

        expected_path = self.worktrees / f"card-{self.claim.card_id}"
        self.assertTrue(expected_path.is_dir())
        persisted = []
        with self.manager.locked_workspace(
            self.claim,
            persist_workspace=lambda branch, path: persisted.append((branch, path)),
        ) as recovered:
            self.assertEqual(recovered.path, expected_path)

        self.assertEqual(len(persisted), 1)

    def test_existing_workspace_keeps_original_base_when_main_advances(self):
        persisted = []
        with self.manager.locked_workspace(
            self.claim,
            persist_workspace=lambda branch, path: persisted.append((branch, path)),
        ) as workspace:
            original_base = workspace.base_commit

        clone = self.root / "advance"
        subprocess.run(
            ["git", "clone", str(self.source), str(clone)],
            check=True,
            capture_output=True,
        )
        _git(clone, "config", "user.name", "RemiHub Test")
        _git(clone, "config", "user.email", "remihub@example.invalid")
        (clone / "later.txt").write_text("later\n", encoding="utf-8")
        _git(clone, "add", "later.txt")
        _git(clone, "commit", "-m", "Advance main")
        _git(clone, "push", "origin", "main")

        stored_claim = replace(
            self.claim,
            feature_branch=persisted[0][0],
            worktree_path=persisted[0][1],
        )
        with self.manager.locked_workspace(
            stored_claim,
            persist_workspace=lambda _branch, _path: None,
        ) as recovered:
            self.assertEqual(recovered.base_commit, original_base)
            self.assertNotEqual(
                recovered.base_commit,
                _git(self.source, "rev-parse", "main"),
            )

    def test_existing_workspace_rejects_agent_created_commit(self):
        persisted = []
        with self.manager.locked_workspace(
            self.claim,
            persist_workspace=lambda branch, path: persisted.append((branch, path)),
        ) as workspace:
            _git(workspace.path, "config", "user.name", "RemiHub Test")
            _git(workspace.path, "config", "user.email", "remihub@example.invalid")
            (workspace.path / "tracked.txt").write_text("committed\n", encoding="utf-8")
            _git(workspace.path, "add", "tracked.txt")
            _git(workspace.path, "commit", "-m", "Unexpected commit")

        stored_claim = replace(
            self.claim,
            feature_branch=persisted[0][0],
            worktree_path=persisted[0][1],
        )
        with self.assertRaisesRegex(AgentWorkspaceError, "contains commits"):
            with self.manager.locked_workspace(
                stored_claim,
                persist_workspace=lambda _branch, _path: None,
            ):
                self.fail("committed implementation work must fail closed")

    def test_existing_metadata_must_match_deterministic_workspace(self):
        conflicting = replace(
            self.claim,
            feature_branch="agent/unexpected",
            worktree_path=str(self.worktrees / "unexpected"),
        )

        with self.assertRaisesRegex(AgentWorkspaceError, "deterministic branch"):
            with self.manager.locked_workspace(
                conflicting,
                persist_workspace=lambda _branch, _path: None,
            ):
                self.fail("conflicting metadata must fail closed")

    def test_capture_snapshot_includes_tracked_and_untracked_files(self):
        with self.manager.locked_workspace(
            self.claim,
            persist_workspace=lambda _branch, _path: None,
        ) as workspace:
            (workspace.path / "tracked.txt").write_text("after\n", encoding="utf-8")
            (workspace.path / "new.txt").write_text("new\n", encoding="utf-8")
            snapshot = self.manager.capture_snapshot(self.claim, workspace)

        self.assertEqual(snapshot.changed_files, ("new.txt", "tracked.txt"))
        self.assertIn("M tracked.txt", snapshot.status_porcelain)
        self.assertIn("?? new.txt", snapshot.status_porcelain)
        patch_text = snapshot.patch_path.read_text(encoding="utf-8")
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", patch_text)
        self.assertIn("diff --git a/new.txt b/new.txt", patch_text)
        self.assertEqual(snapshot.patch_size_bytes, snapshot.patch_path.stat().st_size)

    def test_planning_run_cannot_receive_writable_workspace(self):
        with self.assertRaisesRegex(AgentWorkspaceError, "implementation runs"):
            with self.manager.locked_workspace(
                claimed_run(phase=RunPhase.PLANNING),
                persist_workspace=lambda _branch, _path: None,
            ):
                self.fail("planning must not receive a writable workspace")


if __name__ == "__main__":
    unittest.main()

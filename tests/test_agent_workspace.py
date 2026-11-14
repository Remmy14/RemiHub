import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

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

    def test_base_branch_override_validates_after_source_repository_initialization(self):
        manager = GitImplementationWorkspaceManager(
            source_repository=self.source,
            worktree_root=self.worktrees,
            artifact_root=self.artifacts,
            base_branch_override="main",
        )

        self.assertEqual(manager.source_repository, self.source.resolve())
        self.assertEqual(manager.base_branch_override, "main")

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

    def _advanced_correction(self, *, conflict):
        persisted = []
        with self.manager.locked_workspace(self.claim,
                persist_workspace=lambda branch,path:persisted.append((branch,str(path)))) as workspace:
            original_base = workspace.base_commit
            (workspace.path / "tracked.txt").write_text("candidate change\n")
            (workspace.path / "new.txt").write_text("preserve new source\n")
            original_snapshot = self.manager.capture_snapshot(self.claim, workspace)
            original_bytes = original_snapshot.patch_path.read_bytes()
        clone = self.root / "approved-advance"
        subprocess.run(["git","clone",str(self.source),str(clone)],check=True,capture_output=True)
        _git(clone,"config","user.name","Approved Source Fixture")
        _git(clone,"config","user.email","fixture@example.invalid")
        (clone / "later.txt").write_text("new approved infrastructure\n")
        if conflict:(clone / "tracked.txt").write_text("approved baseline change\n")
        _git(clone,"add",".");_git(clone,"commit","-m","Approved source advance");_git(clone,"push","origin","main")
        correction = replace(self.claim,id=str(uuid4()),feature_branch=persisted[0][0],
            worktree_path=persisted[0][1],result_metadata={"refresh_base":True,"review_attempt":2})
        return correction, original_base, original_snapshot.patch_path, original_bytes

    def test_authorized_correction_advances_base_and_preserves_candidate_and_history(self):
        claim, old_base, original_patch, original_bytes = self._advanced_correction(conflict=False)
        with self.manager.locked_workspace(claim,persist_workspace=lambda *_args:None) as workspace:
            self.assertEqual(workspace.base_commit,_git(self.source,"rev-parse","main"))
            self.assertNotEqual(workspace.base_commit,old_base)
            self.assertEqual((workspace.path / "tracked.txt").read_text(),"candidate change\n")
            self.assertEqual((workspace.path / "new.txt").read_text(),"preserve new source\n")
            self.assertEqual((workspace.path / "later.txt").read_text(),"new approved infrastructure\n")
            stash=workspace.base_refresh["preserved_stash"]
            self.assertEqual(_git(workspace.path,"show",stash+":tracked.txt"),"candidate change")
            self.manager.prepare_validation(workspace)
            snapshot=self.manager.capture_snapshot(claim,workspace)
            self.assertEqual(snapshot.changed_files,("new.txt","tracked.txt"))
        self.assertEqual(original_patch.read_bytes(),original_bytes)

    def test_overlapping_baseline_changes_require_source_resolution_before_validation(self):
        claim, _old, original_patch, original_bytes = self._advanced_correction(conflict=True)
        with self.manager.locked_workspace(claim,persist_workspace=lambda *_args:None) as workspace:
            self.assertIn("tracked.txt",workspace.base_refresh["conflicted_files"])
            self.assertIn("<<<<<<<",(workspace.path / "tracked.txt").read_text())
            with self.assertRaisesRegex(AgentWorkspaceError,"Resolve baseline merge"):
                self.manager.prepare_validation(workspace)
            (workspace.path / "tracked.txt").write_text("approved baseline change\ncandidate correction\n")
            self.manager.prepare_validation(workspace)
            self.assertEqual(self.manager._unmerged_paths(workspace.path),[])
            snapshot=self.manager.capture_snapshot(claim,workspace)
            self.assertEqual(snapshot.head_commit,workspace.base_commit)
            self.assertNotIn("later.txt",snapshot.changed_files)
        self.assertEqual(original_patch.read_bytes(),original_bytes)

    def test_staged_unresolved_conflicts_remain_fenced_across_workspace_retries(self):
        claim, _old, patch, original_bytes = self._advanced_correction(conflict=True)
        with self.manager.locked_workspace(claim, persist_workspace=lambda *_:None) as workspace:
            self.assertIn("<<<<<<<", (workspace.path / "tracked.txt").read_text())
            _git(workspace.path, "add", "tracked.txt")
            self.assertEqual(self.manager._unmerged_paths(workspace.path), [])
            with self.assertRaisesRegex(AgentWorkspaceError, "Resolve baseline merge"):
                self.manager.prepare_validation(workspace)
        with self.manager.locked_workspace(claim, persist_workspace=lambda *_:None) as recovered:
            self.assertIsNone(recovered.base_refresh)
            with self.assertRaisesRegex(AgentWorkspaceError, "Resolve baseline merge"):
                self.manager.prepare_validation(recovered)
            (recovered.path / "tracked.txt").write_text("approved baseline change\ncandidate correction\n")
            _git(recovered.path, "add", "tracked.txt")
            self.manager.prepare_validation(recovered)
            self.manager.prepare_validation(recovered)
        self.assertEqual(patch.read_bytes(), original_bytes)

    def test_untracked_baseline_collision_fences_retries_until_explicit_source_recovery(self):
        claim, _old, patch, original_bytes = self._advanced_correction(conflict=False)
        clone = self.root / "approved-advance"
        (clone / "new.txt").write_text("approved new source\n")
        _git(clone, "add", ".");_git(clone, "commit", "-m", "Add colliding source");_git(clone, "push", "origin", "main")
        path = Path(claim.worktree_path)
        with self.assertRaisesRegex(AgentWorkspaceError, "needs explicit recovery"):
            with self.manager.locked_workspace(claim, persist_workspace=lambda *_:None):
                self.fail("partially restored candidate must not reach implementation")
        self.assertEqual(_git(path, "rev-parse", "HEAD"), _git(self.source, "rev-parse", "main"))
        pending = self.manager._pending_refresh_ref(path)
        stash = _git(path, "rev-parse", pending)
        self.assertEqual(_git(path, "show", stash + "^3:new.txt"), "preserve new source")
        self.assertEqual(_git(path, "rev-parse", "refs/remihub-preserved/" + stash), stash)
        with self.assertRaisesRegex(AgentWorkspaceError, "recovery is incomplete"):
            with self.manager.locked_workspace(claim, persist_workspace=lambda *_:None):
                self.fail("HEAD matching latest must not bypass pending recovery")
        # Explicit operator recovery restores old source before clearing the host marker.
        preserved = subprocess.run(["git", "-C", str(path), "show", stash + "^3:new.txt"],
                                   check=True, capture_output=True).stdout
        (path / "new.txt").write_bytes(preserved)
        _git(path, "update-ref", "-d", pending, stash)
        with self.manager.locked_workspace(claim, persist_workspace=lambda *_:None) as workspace:
            self.manager.prepare_validation(workspace)
            self.assertEqual((workspace.path / "new.txt").read_text(), "preserve new source\n")
            self.assertEqual((workspace.path / "tracked.txt").read_text(), "candidate change\n")
        self.assertEqual(patch.read_bytes(), original_bytes)

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

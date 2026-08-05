import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKER = (
    ROOT
    / "deployments"
    / "agent_backend"
    / "libexec"
    / "remihub-backend-github-sync"
)
CONTROL = (
    ROOT
    / "deployments"
    / "agent_backend"
    / "libexec"
    / "remihub-backend-github-sync-control"
)


class BackendGithubSyncAssetTests(unittest.TestCase):
    def test_network_worker_never_opens_canonical_worktree_index(self):
        source = WORKER.read_text(encoding="utf-8")
        self.assertIn('GIT_DIR = REPO / ".git"', source)
        self.assertIn('f"--git-dir={GIT_DIR}"', source)
        self.assertNotIn('"-C", str(REPO)', source)
        self.assertNotIn('git("status"', source)
        self.assertNotIn("release_sha256", source)
        self.assertNotIn("ALLOWED_DIRTY", source)

    def test_root_control_runs_every_canonical_worktree_git_as_alex(self):
        source = CONTROL.read_text(encoding="utf-8")
        self.assertNotIn("ALLOWED_DIRTY", source)
        self.assertNotIn("RELEASE_FILE", source)
        self.assertIn('fail("canonical worktree is not clean")', source)
        tree = ast.parse(source)
        canonical_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "git_worktree":
                continue
            if not node.args or not isinstance(node.args[0], ast.Name):
                continue
            if node.args[0].id != "CANONICAL_REPO":
                continue
            canonical_calls.append(node)
        self.assertGreaterEqual(len(canonical_calls), 7)
        for call in canonical_calls:
            user_values = [
                keyword.value.value
                for keyword in call.keywords
                if keyword.arg == "user"
                and isinstance(keyword.value, ast.Constant)
            ]
            self.assertEqual(user_values, ["alex"])

    def test_control_rechecks_clean_worktree_after_network_operation(self):
        source = CONTROL.read_text(encoding="utf-8")
        self.assertIn("status_after = git_worktree(", source)
        self.assertIn('fail("canonical worktree changed during GitHub synchronization")', source)


if __name__ == "__main__":
    unittest.main()

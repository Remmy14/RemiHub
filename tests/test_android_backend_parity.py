"""Regression coverage for Android's backend-only QA parity inheritance."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from backend.core.agent_deployment import AgentDeploymentError, GitBackendDeploymentManager
from backend.core.agent_state import RepositoryScope, RunPhase
from backend.core.agent_worker import AgentWorkerConfigurationError
from backend.core.android_deployment import GitAndroidDeploymentManager


class DeploymentManagerParityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        seed = root / "seed"
        source = root / "source.git"
        target = root / "production.git"
        self._git("init", "-b", "main", str(seed))
        self._git("-C", str(seed), "-c", "user.name=RemiHub Test",
                  "-c", "user.email=test@example.invalid", "commit",
                  "--allow-empty", "-m", "seed")
        commit = self._git("-C", str(seed), "rev-parse", "HEAD").strip()
        self._git("clone", "--bare", str(seed), str(source))
        self._git("clone", "--bare", str(seed), str(target))
        self._git("-C", str(target), "remote", "remove", "origin")
        self._git("-C", str(target), "update-ref",
                  "refs/heads/production-main", commit)
        paths = {
            key: root / key for key in (
                "source_worktrees", "source_artifacts", "candidate_worktrees",
                "deployment_artifacts"
            )
        }
        for path in paths.values():
            path.mkdir()
        self.kwargs = {
            "source_repository": source,
            "source_worktree_root": paths["source_worktrees"],
            "source_artifact_root": paths["source_artifacts"],
            "target_repository": target,
            "candidate_worktree_root": paths["candidate_worktrees"],
            "deployment_artifact_root": paths["deployment_artifacts"],
            "target_branch": "production-main",
        }

    @staticmethod
    def _git(*args):
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True,
        ).stdout

    def test_backend_still_requires_qa_migration_reader(self):
        self.assertIs(GitBackendDeploymentManager.BACKEND_QA_PARITY_REQUIRED, True)
        with self.assertRaisesRegex(
            AgentWorkerConfigurationError, "QA migration parity reader"
        ):
            GitBackendDeploymentManager(
                environment="production", **self.kwargs,
                validator=MagicMock(), database=MagicMock(),
                runtime=MagicMock(),
            )

    def test_backend_still_requires_qa_candidate_repository(self):
        with self.assertRaisesRegex(
            AgentWorkerConfigurationError, "QA candidate repository"
        ):
            GitBackendDeploymentManager(
                environment="production", **self.kwargs,
                validator=MagicMock(), database=MagicMock(),
                runtime=MagicMock(), qa_history_reader=MagicMock(),
            )

    def test_android_constructor_does_not_require_backend_qa_parity(self):
        self.assertIs(GitAndroidDeploymentManager.BACKEND_QA_PARITY_REQUIRED, False)
        manager = GitAndroidDeploymentManager(
            **self.kwargs, validator=MagicMock(), runtime=MagicMock()
        )
        self.assertEqual(manager.environment, "production")
        self.assertIsNone(manager.qa_history_reader)
        self.assertIsNone(manager.qa_candidate_repository)

    def test_android_deploy_still_rejects_missing_approved_implementation(self):
        manager = GitAndroidDeploymentManager(
            **self.kwargs, validator=MagicMock(), runtime=MagicMock()
        )
        claim = SimpleNamespace(
            phase=RunPhase.DEPLOYMENT,
            repository_scope=RepositoryScope.ANDROID,
            deployment_source=None,
        )
        with self.assertRaisesRegex(
            AgentDeploymentError, "approved implementation result"
        ):
            manager.deploy(claim)


if __name__ == "__main__":
    unittest.main()

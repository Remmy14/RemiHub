import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from backend.core.android_deployment import (
    EXPECTED_CERTIFICATE_SHA256,
    EXPECTED_PACKAGE_NAME,
    AndroidReleaseVersion,
    GitAndroidDeploymentExecutor,
    GitAndroidDeploymentManager,
)
from backend.core.agent_deployment import AgentDeploymentError
from backend.core.agent_state import CardStatus, RepositoryScope, RunPhase
from backend.core.agent_worker import ClaimedRun


class AndroidDeploymentBoundaryTests(unittest.TestCase):
    def manager_without_init(self):
        manager = object.__new__(GitAndroidDeploymentManager)
        manager.target_branch = "production-master"
        return manager

    def test_android_path_boundary_allows_app_source_and_docs(self):
        manager = self.manager_without_init()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "app/src/main/java/com/alex/remihub/Example.kt"
            source.parent.mkdir(parents=True)
            source.write_text("class Example\n")
            docs = root / "AGENTS.md"
            docs.write_text("# Instructions\n")
            manager._require_backend_paths(
                (
                    "AGENTS.md",
                    "app/src/main/java/com/alex/remihub/Example.kt",
                ),
                root,
            )

    def test_android_path_boundary_blocks_build_logic(self):
        manager = self.manager_without_init()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_file = root / "app/build.gradle.kts"
            build_file.parent.mkdir(parents=True)
            build_file.write_text("plugins {}\n")
            with self.assertRaisesRegex(AgentDeploymentError, "protected build"):
                manager._require_backend_paths(("app/build.gradle.kts",), root)

    def test_executor_is_android_only(self):
        manager = MagicMock()
        executor = GitAndroidDeploymentExecutor(deployment_manager=manager)
        self.assertEqual(
            executor.allowed_repository_scopes,
            frozenset({RepositoryScope.ANDROID}),
        )

        claim = ClaimedRun(
            id="22222222-2222-4222-8222-222222222222",
            card_id="11111111-1111-4111-8111-111111111111",
            phase=RunPhase.DEPLOYMENT,
            card_status=CardStatus.DEPLOYING,
            card_revision=1,
            attempt_count=1,
            lease_token="33333333-3333-4333-8333-333333333333",
            worker_id="worker",
            title="Android feature",
            description="description",
            repository_scope=RepositoryScope.BACKEND,
        )
        with self.assertRaisesRegex(Exception, "repository_scope=android"):
            executor.execute(claim)
        manager.deploy.assert_not_called()

    def test_release_request_contains_exact_identity(self):
        manager = self.manager_without_init()
        approved = MagicMock(
            approval_id="approval",
            implementation_run_id="implementation",
            base_branch="master",
            base_commit="a" * 40,
            feature_branch="agent/card-id",
            changed_files=("AGENTS.md",),
            patch_sha256="b" * 64,
            patch_size_bytes=10,
            expected_tree="e" * 40,
        )
        claim = MagicMock(
            card_id="11111111-1111-4111-8111-111111111111",
            card_revision=1,
            id="22222222-2222-4222-8222-222222222222",
        )
        version = AndroidReleaseVersion(64, 0, 8, 10, "0.8.10")
        validation = MagicMock(
            unsigned_apk_path="/tmp/app.apk",
            unsigned_apk_sha256="c" * 64,
            unsigned_apk_size_bytes=123,
            manifest_path="/tmp/validation.json",
        )
        request = manager._release_request(
            claim=claim,
            approved=approved,
            candidate_branch="deployment/card-id/r1",
            candidate_commit="d" * 40,
            candidate_path=Path("/tmp/candidate"),
            version=version,
            validation=validation,
            manifest_path=Path("/tmp/deployment.json"),
        )
        self.assertEqual(request["package_name"], EXPECTED_PACKAGE_NAME)
        self.assertEqual(
            request["certificate_sha256"],
            EXPECTED_CERTIFICATE_SHA256,
        )
        self.assertEqual(request["version"]["version_code"], 64)
        self.assertEqual(request["candidate_commit"], "d" * 40)
        self.assertEqual(request["expected_tree"], "e" * 40)


if __name__ == "__main__":
    unittest.main()

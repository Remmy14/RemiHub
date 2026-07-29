import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.core.android_deployment import (
    EXPECTED_CERTIFICATE_SHA256,
    EXPECTED_PACKAGE_NAME,
    AndroidReleaseVersion,
    AndroidValidationEvidence,
    CommandAndroidReleaseValidator,
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


    def validation_evidence(self):
        raw = {
            "success": True,
            "validator": "trusted_android_release_offline_gradle",
            "tasks": [
                ":app:testDebugUnitTest",
                ":app:lintDebug",
                ":app:assembleDebug",
                ":app:assembleRelease",
            ],
            "network": "denied",
            "gradle_offline": True,
            "protected_build_files_unchanged": True,
            "workspace": "/tmp/candidate",
            "gradle_log": "/tmp/gradle.log",
            "manifest_path": "/tmp/validation.json",
            "release_apk": {
                "path": "/tmp/app-release-unsigned.apk",
                "sha256": "c" * 64,
                "size_bytes": 123,
                "package_name": EXPECTED_PACKAGE_NAME,
                "version_code": 64,
                "version_name": "0.8.10",
                "signed": False,
            },
        }
        return AndroidValidationEvidence(
            manifest_path=raw["manifest_path"],
            unsigned_apk_path=raw["release_apk"]["path"],
            unsigned_apk_sha256=raw["release_apk"]["sha256"],
            unsigned_apk_size_bytes=raw["release_apk"]["size_bytes"],
            package_name=EXPECTED_PACKAGE_NAME,
            version_code=64,
            version_name="0.8.10",
            gradle_log=raw["gradle_log"],
            raw=raw,
        )

    def trusted_implementation_metadata(self, *, success=True):
        return {
            "tests": [
                {
                    "command": "./scripts/verify-android-baseline.sh",
                    "status": "failed",
                    "details": "Codex sandbox Java security path was unavailable.",
                }
            ],
            "trusted_validation": {
                "success": success,
                "network": "denied",
                "gradle_offline": True,
                "protected_build_files_unchanged": True,
                "release_apk": {
                    "signed": False,
                    "package_name": EXPECTED_PACKAGE_NAME,
                },
            },
        }

    def test_trusted_android_validation_supersedes_failed_codex_test_report(self):
        manager = self.manager_without_init()
        metadata = self.trusted_implementation_metadata()

        observed = manager._validate_implementation_tests(metadata)

        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["status"], "failed")
        self.assertIn("Codex sandbox", observed[0]["details"])

    def test_failed_codex_test_still_blocks_without_trusted_android_validation(self):
        manager = self.manager_without_init()
        metadata = self.trusted_implementation_metadata(success=False)

        with self.assertRaisesRegex(
            AgentDeploymentError,
            "contains failed implementation tests",
        ):
            manager._validate_implementation_tests(metadata)

    def test_trusted_android_validation_requires_offline_network_denied_evidence(self):
        manager = self.manager_without_init()
        metadata = self.trusted_implementation_metadata()
        metadata["trusted_validation"]["network"] = "allowed"

        with self.assertRaisesRegex(
            AgentDeploymentError,
            "contains failed implementation tests",
        ):
            manager._validate_implementation_tests(metadata)

    def test_first_attempt_builds_validation(self):
        manager = self.manager_without_init()
        manager.release_validator = MagicMock()
        evidence = self.validation_evidence()
        manager.release_validator.validate.return_value = evidence
        current = {"attempt_index": 1, "status": "running"}
        manifest = {"attempts": [current]}
        claim = MagicMock()
        version = AndroidReleaseVersion(64, 0, 8, 10, "0.8.10")

        observed, metadata = manager._validation_for_attempt(
            manifest=manifest,
            current_attempt=current,
            candidate_worktree=Path("/tmp/candidate"),
            claim=claim,
            version=version,
        )

        self.assertEqual(observed, evidence)
        self.assertEqual(metadata, {"validation_mode": "built"})
        manager.release_validator.validate.assert_called_once()
        manager.release_validator.verify_existing.assert_not_called()

    def test_retry_reuses_rolled_back_validation_without_gradle(self):
        manager = self.manager_without_init()
        manager.release_validator = MagicMock()
        evidence = self.validation_evidence()
        prior = {
            "attempt_index": 1,
            "status": "rolled_back",
            "validation": evidence.__dict__,
        }
        current = {"attempt_index": 2, "status": "running"}
        manifest = {"attempts": [prior, current]}
        claim = MagicMock()
        version = AndroidReleaseVersion(64, 0, 8, 10, "0.8.10")
        manager.release_validator.verify_existing.return_value = evidence

        observed, metadata = manager._validation_for_attempt(
            manifest=manifest,
            current_attempt=current,
            candidate_worktree=Path("/tmp/candidate"),
            claim=claim,
            version=version,
        )

        self.assertEqual(observed, evidence)
        self.assertEqual(metadata["validation_mode"], "reused")
        self.assertEqual(metadata["reused_validation_attempt_index"], 1)
        self.assertEqual(
            metadata["reused_unsigned_apk_sha256"],
            evidence.unsigned_apk_sha256,
        )
        manager.release_validator.verify_existing.assert_called_once_with(
            candidate_worktree=Path("/tmp/candidate"),
            claim=claim,
            version=version,
            expected=evidence.__dict__,
        )
        manager.release_validator.validate.assert_not_called()

    def test_retry_fails_when_rolled_back_validation_is_missing(self):
        manager = self.manager_without_init()
        manager.release_validator = MagicMock()
        prior = {"attempt_index": 1, "status": "rolled_back"}
        current = {"attempt_index": 2, "status": "running"}
        with self.assertRaisesRegex(AgentDeploymentError, "omitted reusable"):
            manager._validation_for_attempt(
                manifest={"attempts": [prior, current]},
                current_attempt=current,
                candidate_worktree=Path("/tmp/candidate"),
                claim=MagicMock(),
                version=AndroidReleaseVersion(64, 0, 8, 10, "0.8.10"),
            )
        manager.release_validator.validate.assert_not_called()
        manager.release_validator.verify_existing.assert_not_called()

    def test_verify_existing_invokes_non_build_validator_mode(self):
        version = AndroidReleaseVersion(64, 0, 8, 10, "0.8.10")
        evidence = self.validation_evidence()
        completed = MagicMock(
            returncode=0,
            stdout=json.dumps(evidence.raw),
            stderr="",
        )
        validator = object.__new__(CommandAndroidReleaseValidator)
        validator.validation_command = Path("/usr/local/libexec/validator")
        validator.timeout_seconds = 30
        claim = MagicMock(
            card_id="11111111-1111-4111-8111-111111111111",
            id="22222222-2222-4222-8222-222222222222",
        )

        with patch(
            "backend.core.android_deployment.subprocess.run",
            return_value=completed,
        ) as run:
            observed = validator.verify_existing(
                candidate_worktree=Path("/tmp/candidate"),
                claim=claim,
                version=version,
                expected=evidence.__dict__,
            )

        self.assertEqual(observed, evidence)
        command = run.call_args.args[0]
        self.assertEqual(command[1], "--verify-existing")
        self.assertEqual(command[2], "/tmp/candidate")

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

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from backend.core.agent_deployment import (
    AgentDeploymentError,
    ApprovedImplementation,
    DeploymentRolledBackError,
    GitBackendDeploymentManager,
    _required_executable,
)
from backend.core.agent_state import (
    CardStatus,
    RepositoryScope,
    RunPhase,
    require_exact_repository_scope,
)
from backend.core.agent_worker import (
    AgentTemporarilyBlockedError,
    AgentWorkerConfigurationError,
    ClaimedRun,
    ExecutionResult,
)


EXPECTED_PACKAGE_NAME = "com.alex.remihub"
EXPECTED_CERTIFICATE_SHA256 = (
    "029cc5d06bd10e1d07a56834dd45326c9762f6263c5835244bcaf4a6a6a6e03d"
)


@dataclass(frozen=True)
class AndroidReleaseVersion:
    version_code: int
    version_major: int
    version_minor: int
    version_patch: int
    version_name: str


@dataclass(frozen=True)
class AndroidValidationEvidence:
    manifest_path: str
    unsigned_apk_path: str
    unsigned_apk_sha256: str
    unsigned_apk_size_bytes: int
    package_name: str
    version_code: int
    version_name: str
    gradle_log: str
    raw: dict


@dataclass(frozen=True)
class AndroidPublicationEvidence:
    status: str
    release_id: int
    apk_filename: str
    apk_relative_path: str
    apk_sha256: str
    file_size_bytes: int
    package_name: str
    version_code: int
    version_name: str
    certificate_sha256: str
    candidate_commit: str
    previous_commit: str
    journal_path: str
    raw: dict


@dataclass(frozen=True)
class AndroidDeploymentCandidate:
    approval_id: str
    implementation_run_id: str
    candidate_branch: str
    candidate_commit: str
    base_commit: str
    changed_files: tuple[str, ...]
    patch_sha256: str
    patch_size_bytes: int
    manifest_path: str
    request_path: str
    version: dict
    validation: dict
    publication: dict


class CommandAndroidReleaseValidator:
    def __init__(self, *, validation_command: str | Path, timeout_seconds: int):
        self.validation_command = _required_executable(
            validation_command,
            field="REMIHUB_AGENT_DEPLOYMENT_VALIDATOR",
        )
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")
        self.timeout_seconds = timeout_seconds

    def validate(
        self,
        *,
        candidate_worktree: Path,
        claim: ClaimedRun,
        version: AndroidReleaseVersion,
    ) -> AndroidValidationEvidence:
        return self._execute(
            [
                str(candidate_worktree),
                claim.card_id,
                claim.id,
                str(version.version_code),
                str(version.version_major),
                str(version.version_minor),
                str(version.version_patch),
            ],
            version=version,
            context="Trusted Android release validation",
        )

    def verify_existing(
        self,
        *,
        candidate_worktree: Path,
        claim: ClaimedRun,
        version: AndroidReleaseVersion,
        expected: dict,
    ) -> AndroidValidationEvidence:
        expected_evidence = self._evidence_from_mapping(expected, version=version)
        observed = self._execute(
            [
                "--verify-existing",
                str(candidate_worktree),
                claim.card_id,
                claim.id,
                str(version.version_code),
                str(version.version_major),
                str(version.version_minor),
                str(version.version_patch),
            ],
            version=version,
            context="Trusted Android release artifact reuse verification",
        )
        if observed != expected_evidence:
            raise AgentDeploymentError(
                "Reused Android release validation evidence changed after rollback"
            )
        return observed

    def _execute(
        self,
        arguments: list[str],
        *,
        version: AndroidReleaseVersion,
        context: str,
    ) -> AndroidValidationEvidence:
        result = subprocess.run(
            [str(self.validation_command), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout)[-4000:].strip()
            raise AgentDeploymentError(
                context + " failed" + (f": {tail}" if tail else "")
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AgentDeploymentError(
                f"{context} returned invalid JSON"
            ) from exc
        return self._evidence_from_payload(payload, version=version)

    @classmethod
    def _evidence_from_mapping(
        cls,
        value: dict,
        *,
        version: AndroidReleaseVersion,
    ) -> AndroidValidationEvidence:
        if not isinstance(value, dict):
            raise AgentDeploymentError(
                "Rolled-back Android deployment omitted reusable validation evidence"
            )
        raw = value.get("raw")
        if not isinstance(raw, dict):
            raise AgentDeploymentError(
                "Rolled-back Android validation raw evidence is invalid"
            )
        evidence = cls._evidence_from_payload(raw, version=version)
        expected = AndroidValidationEvidence(
            manifest_path=_required_string(value, "manifest_path"),
            unsigned_apk_path=_required_string(value, "unsigned_apk_path"),
            unsigned_apk_sha256=_required_sha256(value, "unsigned_apk_sha256"),
            unsigned_apk_size_bytes=_required_positive_int(
                value,
                "unsigned_apk_size_bytes",
            ),
            package_name=_required_string(value, "package_name"),
            version_code=_required_positive_int(value, "version_code"),
            version_name=_required_string(value, "version_name"),
            gradle_log=_required_string(value, "gradle_log"),
            raw=raw,
        )
        if evidence != expected:
            raise AgentDeploymentError(
                "Rolled-back Android validation evidence is internally inconsistent"
            )
        return expected

    @staticmethod
    def _evidence_from_payload(
        payload: dict,
        *,
        version: AndroidReleaseVersion,
    ) -> AndroidValidationEvidence:
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise AgentDeploymentError(
                "Trusted Android release validator did not report success"
            )
        release_apk = payload.get("release_apk")
        if not isinstance(release_apk, dict):
            raise AgentDeploymentError(
                "Trusted Android release validator omitted release APK evidence"
            )
        if release_apk.get("signed") is not False:
            raise AgentDeploymentError(
                "Trusted Android release candidate must remain unsigned"
            )
        if release_apk.get("package_name") != EXPECTED_PACKAGE_NAME:
            raise AgentDeploymentError("Trusted Android release package is unexpected")
        if release_apk.get("version_code") != version.version_code:
            raise AgentDeploymentError("Trusted Android release versionCode is unexpected")
        if release_apk.get("version_name") != version.version_name:
            raise AgentDeploymentError("Trusted Android release versionName is unexpected")
        if payload.get("network") != "denied" or payload.get("gradle_offline") is not True:
            raise AgentDeploymentError(
                "Trusted Android release validation was not offline"
            )
        if payload.get("protected_build_files_unchanged") is not True:
            raise AgentDeploymentError(
                "Protected Android build files changed during release validation"
            )
        return AndroidValidationEvidence(
            manifest_path=_required_string(payload, "manifest_path"),
            unsigned_apk_path=_required_string(release_apk, "path"),
            unsigned_apk_sha256=_required_sha256(release_apk, "sha256"),
            unsigned_apk_size_bytes=_required_positive_int(
                release_apk,
                "size_bytes",
            ),
            package_name=release_apk["package_name"],
            version_code=release_apk["version_code"],
            version_name=release_apk["version_name"],
            gradle_log=_required_string(payload, "gradle_log"),
            raw=payload,
        )


class PrivilegedAndroidReleaseRuntime:
    def __init__(
        self,
        *,
        helper_path: str | Path,
        command_timeout_seconds: int,
    ):
        self.helper_path = _required_executable(
            helper_path,
            field="REMIHUB_AGENT_DEPLOYMENT_RUNTIME_HELPER",
        )
        if command_timeout_seconds < 1:
            raise ValueError("command_timeout_seconds must be at least 1")
        self.command_timeout_seconds = command_timeout_seconds

    def reserve(
        self,
        *,
        claim: ClaimedRun,
        approved: ApprovedImplementation,
        candidate_commit: str,
    ) -> AndroidReleaseVersion:
        payload = self._run(
            "reserve",
            claim.card_id,
            claim.id,
            candidate_commit,
            approved.base_commit,
            approved.patch_sha256,
        )
        return AndroidReleaseVersion(
            version_code=_required_positive_int(payload, "version_code"),
            version_major=_required_nonnegative_int(payload, "version_major"),
            version_minor=_required_nonnegative_int(payload, "version_minor"),
            version_patch=_required_nonnegative_int(payload, "version_patch"),
            version_name=_required_string(payload, "version_name"),
        )

    def publish(self, request_path: Path) -> AndroidPublicationEvidence:
        payload = self._run("publish", str(request_path))
        return self._publication_evidence(payload)

    def verify(self, request_path: Path) -> AndroidPublicationEvidence:
        payload = self._run("verify", str(request_path))
        return self._publication_evidence(payload)

    def _publication_evidence(self, payload: dict) -> AndroidPublicationEvidence:
        if payload.get("status") != "succeeded":
            raise AgentDeploymentError("Android release helper did not report success")
        certificate = _required_sha256(payload, "certificate_sha256")
        if certificate != EXPECTED_CERTIFICATE_SHA256:
            raise AgentDeploymentError("Published Android certificate is unexpected")
        if payload.get("package_name") != EXPECTED_PACKAGE_NAME:
            raise AgentDeploymentError("Published Android package is unexpected")
        return AndroidPublicationEvidence(
            status="succeeded",
            release_id=_required_positive_int(payload, "release_id"),
            apk_filename=_required_string(payload, "apk_filename"),
            apk_relative_path=_required_string(payload, "apk_relative_path"),
            apk_sha256=_required_sha256(payload, "apk_sha256"),
            file_size_bytes=_required_positive_int(payload, "file_size_bytes"),
            package_name=payload["package_name"],
            version_code=_required_positive_int(payload, "version_code"),
            version_name=_required_string(payload, "version_name"),
            certificate_sha256=certificate,
            candidate_commit=_required_commit(payload, "candidate_commit"),
            previous_commit=_required_commit(payload, "previous_commit"),
            journal_path=_required_string(payload, "journal_path"),
            raw=payload,
        )

    def _run(self, operation: str, *arguments: str) -> dict:
        result = subprocess.run(
            [
                "sudo",
                "--non-interactive",
                str(self.helper_path),
                operation,
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.command_timeout_seconds,
        )
        payload = None
        for line in reversed(result.stdout.splitlines()):
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if result.returncode != 0:
            if isinstance(payload, dict) and payload.get("status") == "rolled_back":
                raise DeploymentRolledBackError(
                    _required_string(payload, "message")
                )
            detail = (result.stderr or result.stdout)[-4000:].strip()
            raise AgentDeploymentError(
                f"Protected Android release helper failed during {operation}"
                + (f": {detail}" if detail else "")
            )
        if not isinstance(payload, dict):
            raise AgentDeploymentError(
                "Protected Android release helper returned invalid JSON"
            )
        return payload


class _UnusedBackendComponent:
    def __getattr__(self, name):
        raise AssertionError(f"Unused backend deployment component invoked: {name}")


class GitAndroidDeploymentManager(GitBackendDeploymentManager):
    """Materialize one reviewed Android patch and publish a protected APK release."""

    PROTECTED_ANDROID_PATHS = frozenset(
        {
            "gradlew",
            "gradlew.bat",
            "gradle.properties",
            "settings.gradle",
            "settings.gradle.kts",
            "build.gradle",
            "build.gradle.kts",
            "app/build.gradle",
            "app/build.gradle.kts",
            "app/google-services.json",
            "local.properties",
        }
    )

    def __init__(
        self,
        *,
        source_repository: str | Path,
        source_worktree_root: str | Path,
        source_artifact_root: str | Path,
        target_repository: str | Path,
        candidate_worktree_root: str | Path,
        deployment_artifact_root: str | Path,
        target_branch: str,
        validator: CommandAndroidReleaseValidator,
        runtime: PrivilegedAndroidReleaseRuntime,
        command_timeout_seconds: int = 120,
    ):
        super().__init__(
            environment="production",
            source_repository=source_repository,
            source_worktree_root=source_worktree_root,
            source_artifact_root=source_artifact_root,
            target_repository=target_repository,
            candidate_worktree_root=candidate_worktree_root,
            deployment_artifact_root=deployment_artifact_root,
            target_branch=target_branch,
            validator=_UnusedBackendComponent(),
            database=_UnusedBackendComponent(),
            runtime=_UnusedBackendComponent(),
            command_timeout_seconds=command_timeout_seconds,
        )
        self.release_validator = validator
        self.release_runtime = runtime

    def deploy(self, claim: ClaimedRun) -> AndroidDeploymentCandidate:
        if claim.phase is not RunPhase.DEPLOYMENT:
            raise AgentDeploymentError(
                "The Android deployment manager accepts only deployment runs"
            )
        require_exact_repository_scope(
            claim.repository_scope,
            expected=RepositoryScope.ANDROID,
            action="Android deployment",
        )
        if claim.deployment_source is None:
            raise AgentDeploymentError(
                "Android deployment requires an approved implementation result"
            )

        lock_path = self.lock_root / f"{claim.card_id}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            os.chmod(lock_path, 0o640)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                self._validate_android_review_evidence(claim)
                approved = self._validate_approved_implementation(
                    replace(claim, base_branch="master"),
                    claim.deployment_source,
                )
                candidate_branch, candidate_commit, candidate_path = (
                    self._materialize_candidate(claim, approved)
                )
                version = self.release_runtime.reserve(
                    claim=claim,
                    approved=approved,
                    candidate_commit=candidate_commit,
                )
                manifest_path = self._manifest_path(claim)
                manifest = self._load_or_initialize_android_manifest(
                    manifest_path,
                    claim=claim,
                    approved=approved,
                    candidate_branch=candidate_branch,
                    candidate_commit=candidate_commit,
                    version=version,
                )
                successful = self._successful_attempt(manifest)
                request_path = manifest_path.with_suffix(".release-request.json")
                if successful is not None:
                    publication = self.release_runtime.verify(request_path)
                    return self._candidate(
                        approved,
                        candidate_branch=candidate_branch,
                        candidate_commit=candidate_commit,
                        manifest_path=manifest_path,
                        request_path=request_path,
                        version=version,
                        validation=successful["validation"],
                        publication=asdict(publication),
                    )

                attempt = self._begin_attempt(manifest, claim)
                self._write_manifest(manifest_path, manifest)
                try:
                    validation, validation_metadata = self._validation_for_attempt(
                        manifest=manifest,
                        current_attempt=attempt,
                        candidate_worktree=candidate_path,
                        claim=claim,
                        version=version,
                    )
                    attempt.update(validation_metadata)
                    attempt["stage"] = "validated"
                    attempt["validation"] = asdict(validation)
                    self._write_manifest(manifest_path, manifest)

                    request = self._release_request(
                        claim=claim,
                        approved=approved,
                        candidate_branch=candidate_branch,
                        candidate_commit=candidate_commit,
                        candidate_path=candidate_path,
                        version=version,
                        validation=validation,
                        manifest_path=manifest_path,
                    )
                    self._write_json(request_path, request)
                    attempt["stage"] = "publication_requested"
                    attempt["request_path"] = str(request_path)
                    self._write_manifest(manifest_path, manifest)

                    publication = self.release_runtime.publish(request_path)
                    attempt["stage"] = "published"
                    attempt["status"] = "succeeded"
                    attempt["finished_at"] = _utc_now()
                    attempt["publication"] = asdict(publication)
                    self._write_manifest(manifest_path, manifest)
                    return self._candidate(
                        approved,
                        candidate_branch=candidate_branch,
                        candidate_commit=candidate_commit,
                        manifest_path=manifest_path,
                        request_path=request_path,
                        version=version,
                        validation=asdict(validation),
                        publication=asdict(publication),
                    )
                except Exception as exc:
                    attempt["status"] = (
                        "rolled_back"
                        if isinstance(exc, DeploymentRolledBackError)
                        else "failed"
                    )
                    attempt["finished_at"] = _utc_now()
                    attempt["error_type"] = type(exc).__name__
                    attempt["error_message"] = str(exc)[:2000]
                    self._write_manifest(manifest_path, manifest)
                    raise
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


    def _validation_for_attempt(
        self,
        *,
        manifest: dict,
        current_attempt: dict,
        candidate_worktree: Path,
        claim: ClaimedRun,
        version: AndroidReleaseVersion,
    ) -> tuple[AndroidValidationEvidence, dict]:
        reusable_attempt = self._latest_rolled_back_attempt(
            manifest,
            exclude=current_attempt,
        )
        if reusable_attempt is None:
            validation = self.release_validator.validate(
                candidate_worktree=candidate_worktree,
                claim=claim,
                version=version,
            )
            return validation, {"validation_mode": "built"}

        prior_validation = reusable_attempt.get("validation")
        if not isinstance(prior_validation, dict):
            raise AgentDeploymentError(
                "Rolled-back Android deployment omitted reusable validation evidence"
            )
        prior_index = reusable_attempt.get("attempt_index")
        if not isinstance(prior_index, int) or isinstance(prior_index, bool) or prior_index < 1:
            raise AgentDeploymentError(
                "Rolled-back Android deployment has an invalid attempt index"
            )
        validation = self.release_validator.verify_existing(
            candidate_worktree=candidate_worktree,
            claim=claim,
            version=version,
            expected=prior_validation,
        )
        return validation, {
            "validation_mode": "reused",
            "reused_validation_attempt_index": prior_index,
            "reused_unsigned_apk_sha256": validation.unsigned_apk_sha256,
            "reused_unsigned_apk_size_bytes": validation.unsigned_apk_size_bytes,
        }

    @staticmethod
    def _latest_rolled_back_attempt(
        manifest: dict,
        *,
        exclude: dict,
    ) -> dict | None:
        attempts = manifest.get("attempts")
        if not isinstance(attempts, list):
            raise AgentDeploymentError("Existing Android deployment attempts are invalid")
        for attempt in reversed(attempts):
            if attempt is exclude:
                continue
            if isinstance(attempt, dict) and attempt.get("status") == "rolled_back":
                return attempt
        return None

    @staticmethod
    def _validate_trusted_implementation_validation(metadata: dict) -> dict:
        validation = metadata.get("trusted_validation")
        if not isinstance(validation, dict) or validation.get("success") is not True:
            raise AgentDeploymentError(
                "Android deployment requires successful trusted implementation validation"
            )
        required = {
            "gradle_offline": True,
            "network": "denied",
            "protected_build_files_unchanged": True,
        }
        for field, expected in required.items():
            if validation.get(field) != expected:
                raise AgentDeploymentError(
                    f"Android implementation validation has unexpected {field}"
                )
        release_apk = validation.get("release_apk")
        if not isinstance(release_apk, dict) or release_apk.get("signed") is not False:
            raise AgentDeploymentError(
                "Android implementation validation must produce an unsigned release APK"
            )
        if release_apk.get("package_name") != EXPECTED_PACKAGE_NAME:
            raise AgentDeploymentError(
                "Android implementation validation package is unexpected"
            )
        return validation

    def _validate_android_review_evidence(self, claim: ClaimedRun) -> None:
        assert claim.deployment_source is not None
        metadata = claim.deployment_source.implementation_result_metadata
        if metadata.get("repository_scope") != RepositoryScope.ANDROID.value:
            raise AgentDeploymentError(
                "Android deployment source has the wrong repository scope"
            )
        self._validate_trusted_implementation_validation(metadata)

    def _failed_implementation_test_is_blocking(
        self,
        metadata: dict,
        test: dict,
    ) -> bool:
        try:
            self._validate_trusted_implementation_validation(metadata)
        except AgentDeploymentError:
            return True
        return False

    def _validate_approved_implementation(
        self,
        claim: ClaimedRun,
        source,
    ) -> ApprovedImplementation:
        approved = super()._validate_approved_implementation(claim, source)
        if approved.base_branch != "master":
            raise AgentDeploymentError("Android implementation base branch must be master")
        return approved

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
            if relative in self.PROTECTED_ANDROID_PATHS:
                raise AgentDeploymentError(
                    f"Android deployment blocks protected build file: {relative}"
                )
            if pure_path.parts and pure_path.parts[0] in {
                "gradle",
                "buildSrc",
                "build-logic",
            }:
                raise AgentDeploymentError(
                    f"Android deployment blocks build logic: {relative}"
                )
            allowed = (
                relative == "AGENTS.md"
                or (
                    len(pure_path.parts) >= 2
                    and pure_path.parts[0] == "docs"
                    and pure_path.suffix.lower() == ".md"
                )
                or (
                    len(pure_path.parts) >= 3
                    and pure_path.parts[:2] == ("app", "src")
                )
            )
            if not allowed:
                raise AgentDeploymentError(
                    "Android deployment permits only app/src content, docs, and AGENTS.md"
                )
            actual_path = worktree.joinpath(*pure_path.parts)
            if actual_path.is_symlink():
                raise AgentDeploymentError("Android deployment rejects symbolic links")
            if actual_path.exists():
                resolved = actual_path.resolve()
                if worktree.resolve() not in resolved.parents:
                    raise AgentDeploymentError("Changed path escapes the worktree")
                if not actual_path.is_file():
                    raise AgentDeploymentError(
                        "Android deployment permits only regular files"
                    )

    def _load_or_initialize_android_manifest(
        self,
        manifest_path: Path,
        *,
        claim: ClaimedRun,
        approved: ApprovedImplementation,
        candidate_branch: str,
        candidate_commit: str,
        version: AndroidReleaseVersion,
    ) -> dict:
        identity = {
            "schema_version": 1,
            "mode": "protected-android-release",
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
            "expected_tree": approved.expected_tree,
            "version": asdict(version),
            "package_name": EXPECTED_PACKAGE_NAME,
            "certificate_sha256": EXPECTED_CERTIFICATE_SHA256,
        }
        if not manifest_path.exists():
            return {**identity, "attempts": []}
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise AgentDeploymentError(
                "Existing Android deployment manifest is not a regular file"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentDeploymentError(
                "Existing Android deployment manifest is invalid"
            ) from exc
        for key, value in identity.items():
            if manifest.get(key) != value:
                raise AgentDeploymentError(
                    f"Existing Android deployment manifest conflicts on {key}"
                )
        if not isinstance(manifest.get("attempts"), list):
            raise AgentDeploymentError(
                "Existing Android deployment attempts are invalid"
            )
        return manifest

    def _release_request(
        self,
        *,
        claim: ClaimedRun,
        approved: ApprovedImplementation,
        candidate_branch: str,
        candidate_commit: str,
        candidate_path: Path,
        version: AndroidReleaseVersion,
        validation: AndroidValidationEvidence,
        manifest_path: Path,
    ) -> dict:
        return {
            "schema_version": 1,
            "mode": "protected-android-release",
            "card_id": claim.card_id,
            "card_revision": claim.card_revision,
            "deployment_run_id": claim.id,
            "approval_id": approved.approval_id,
            "implementation_run_id": approved.implementation_run_id,
            "base_branch": approved.base_branch,
            "base_commit": approved.base_commit,
            "candidate_branch": candidate_branch,
            "candidate_commit": candidate_commit,
            "candidate_worktree": str(candidate_path),
            "target_branch": self.target_branch,
            "changed_files": list(approved.changed_files),
            "patch_sha256": approved.patch_sha256,
            "patch_size_bytes": approved.patch_size_bytes,
            "expected_tree": approved.expected_tree,
            "version": asdict(version),
            "package_name": EXPECTED_PACKAGE_NAME,
            "certificate_sha256": EXPECTED_CERTIFICATE_SHA256,
            "unsigned_apk": {
                "path": validation.unsigned_apk_path,
                "sha256": validation.unsigned_apk_sha256,
                "size_bytes": validation.unsigned_apk_size_bytes,
            },
            "validation_manifest": validation.manifest_path,
            "deployment_manifest": str(manifest_path),
        }

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.chmod(temporary, 0o640)
        temporary.replace(path)

    @staticmethod
    def _candidate(
        approved: ApprovedImplementation,
        *,
        candidate_branch: str,
        candidate_commit: str,
        manifest_path: Path,
        request_path: Path,
        version: AndroidReleaseVersion,
        validation: dict,
        publication: dict,
    ) -> AndroidDeploymentCandidate:
        return AndroidDeploymentCandidate(
            approval_id=approved.approval_id,
            implementation_run_id=approved.implementation_run_id,
            candidate_branch=candidate_branch,
            candidate_commit=candidate_commit,
            base_commit=approved.base_commit,
            changed_files=approved.changed_files,
            patch_sha256=approved.patch_sha256,
            patch_size_bytes=approved.patch_size_bytes,
            manifest_path=str(manifest_path),
            request_path=str(request_path),
            version=asdict(version),
            validation=validation,
            publication=publication,
        )


class GitAndroidDeploymentExecutor:
    allowed_phases = frozenset({RunPhase.DEPLOYMENT})
    allowed_repository_scopes = frozenset({RepositoryScope.ANDROID})

    def __init__(
        self,
        *,
        deployment_manager: GitAndroidDeploymentManager,
        retry_after_seconds: int = 60,
    ):
        if retry_after_seconds < 1:
            raise ValueError("retry_after_seconds must be at least 1")
        self.deployment_manager = deployment_manager
        self.retry_after_seconds = retry_after_seconds

    def execute(self, claim: ClaimedRun) -> ExecutionResult:
        require_exact_repository_scope(
            claim.repository_scope,
            expected=RepositoryScope.ANDROID,
            action="Android deployment",
        )
        try:
            candidate = self.deployment_manager.deploy(claim)
        except DeploymentRolledBackError as exc:
            raise AgentTemporarilyBlockedError(
                str(exc),
                retry_after_seconds=self.retry_after_seconds,
            ) from exc
        publication = candidate.publication
        message = f"""
Published the approved Android candidate as a protected RemiHub release.

- Candidate commit: `{candidate.candidate_commit}`
- Version: `{publication['version_name']}` (`{publication['version_code']}`)
- APK: `{publication['apk_filename']}`
- APK SHA-256: `{publication['apk_sha256']}`
- Signing certificate SHA-256: `{publication['certificate_sha256']}`
- Update endpoint verification: passed
- Rollback journal: `{publication['journal_path']}`
""".strip()
        return ExecutionResult(
            message=message,
            card_status=CardStatus.COMPLETED,
            metadata={
                "executor": "git_android_deployment",
                "phase": claim.phase.value,
                "environment": "production",
                "mode": "protected-android-release",
                "candidate": asdict(candidate),
            },
        )


def _required_string(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AgentDeploymentError(f"Android release evidence is missing {field}")
    return value.strip()


def _required_positive_int(payload: dict, field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AgentDeploymentError(f"Android release evidence has invalid {field}")
    return value


def _required_nonnegative_int(payload: dict, field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AgentDeploymentError(f"Android release evidence has invalid {field}")
    return value


def _required_sha256(payload: dict, field: str) -> str:
    value = _required_string(payload, field).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise AgentDeploymentError(f"Android release evidence has invalid {field}")
    return value


def _required_commit(payload: dict, field: str) -> str:
    value = _required_string(payload, field).lower()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise AgentDeploymentError(f"Android release evidence has invalid {field}")
    return value


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()

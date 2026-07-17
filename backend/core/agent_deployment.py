from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

from backend.core.agent_state import CardStatus, RunPhase
from backend.core.agent_worker import (
    AgentWorkerConfigurationError,
    ClaimedRun,
    DeploymentSource,
    ExecutionResult,
)


class AgentDeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovedImplementation:
    approval_id: str
    implementation_run_id: str
    base_branch: str
    base_commit: str
    feature_branch: str
    worktree_path: str
    head_commit: str
    changed_files: tuple[str, ...]
    status_porcelain: str
    patch_path: str
    patch_size_bytes: int
    patch_sha256: str
    expected_tree: str


@dataclass(frozen=True)
class DeploymentCandidate:
    approval_id: str
    implementation_run_id: str
    candidate_branch: str
    candidate_commit: str
    target_branch: str
    target_before: str
    target_after: str
    base_commit: str
    changed_files: tuple[str, ...]
    patch_sha256: str
    patch_size_bytes: int
    manifest_path: str
    service_restart_performed: bool = False
    migrations_applied: tuple[str, ...] = ()


class GitQaDeploymentManager:
    """Create and fast-forward a documentation-only local QA candidate."""

    def __init__(
        self,
        *,
        source_repository: str | Path,
        source_worktree_root: str | Path,
        source_artifact_root: str | Path,
        target_repository: str | Path,
        candidate_worktree_root: str | Path,
        deployment_artifact_root: str | Path,
        target_branch: str = "qa-main",
        git_binary: str = "git",
        command_timeout_seconds: int = 120,
    ):
        self.git_binary = git_binary.strip()
        if not self.git_binary:
            raise AgentWorkerConfigurationError("git_binary must not be blank")
        if command_timeout_seconds < 1:
            raise ValueError("command_timeout_seconds must be at least 1")
        self.command_timeout_seconds = command_timeout_seconds

        self.source_repository = self._existing_absolute_directory(
            source_repository,
            field="REMIHUB_AGENT_REPOSITORY",
        )
        self.source_worktree_root = self._existing_absolute_directory(
            source_worktree_root,
            field="REMIHUB_AGENT_WORKTREE_ROOT",
        )
        self.source_artifact_root = self._existing_absolute_directory(
            source_artifact_root,
            field="REMIHUB_AGENT_ARTIFACT_ROOT",
        )
        self.target_repository = self._existing_absolute_directory(
            target_repository,
            field="REMIHUB_AGENT_DEPLOYMENT_TARGET_REPOSITORY",
        )
        self.candidate_worktree_root = self._existing_absolute_directory(
            candidate_worktree_root,
            field="REMIHUB_AGENT_DEPLOYMENT_WORKTREE_ROOT",
        )
        self.deployment_artifact_root = self._existing_absolute_directory(
            deployment_artifact_root,
            field="REMIHUB_AGENT_DEPLOYMENT_ARTIFACT_ROOT",
        )
        self.target_branch = self._validate_branch_name(
            self.target_repository,
            target_branch,
            field="deployment target branch",
        )
        if self.target_branch != "qa-main":
            raise AgentWorkerConfigurationError(
                "Phase-one deployment target branch must be qa-main"
            )

        self._run_git(
            self.source_repository,
            "rev-parse",
            "--git-dir",
            error_context="The implementation source is not a Git repository",
        )
        self._run_git(
            self.target_repository,
            "rev-parse",
            "--git-dir",
            error_context="The QA deployment target is not a Git repository",
        )
        self.source_common_directory = self._common_git_directory(
            self.source_repository
        )
        self.target_common_directory = self._common_git_directory(
            self.target_repository
        )
        if self.source_common_directory == self.target_common_directory:
            raise AgentWorkerConfigurationError(
                "QA deployment target must be separate from implementation source"
            )
        if self._run_git(
            self.target_repository,
            "rev-parse",
            "--is-bare-repository",
            error_context="Unable to inspect the QA deployment target",
        ).stdout.strip() != "true":
            raise AgentWorkerConfigurationError(
                "QA deployment target must be a bare Git repository"
            )
        if self._run_git(
            self.target_repository,
            "remote",
            error_context="Unable to inspect QA deployment remotes",
        ).stdout.strip():
            raise AgentWorkerConfigurationError(
                "Phase-one QA deployment target must not have Git remotes"
            )
        self._resolve_commit(self.target_repository, self.target_branch)

        self.lock_root = self.candidate_worktree_root / ".locks"
        self.lock_root.mkdir(mode=0o750, exist_ok=True)
        if self.lock_root.is_symlink():
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_DEPLOYMENT_WORKTREE_ROOT/.locks must not be a symlink"
            )

    def deploy(self, claim: ClaimedRun) -> DeploymentCandidate:
        if claim.phase is not RunPhase.DEPLOYMENT:
            raise AgentDeploymentError(
                "The QA deployment manager accepts only deployment runs"
            )
        if claim.deployment_source is None:
            raise AgentDeploymentError(
                "Deployment requires an approved implementation result"
            )

        lock_path = self.lock_root / f"{claim.card_id}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            os.chmod(lock_path, 0o640)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                approved = self._validate_approved_implementation(
                    claim,
                    claim.deployment_source,
                )
                return self._materialize_candidate(claim, approved)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _validate_approved_implementation(
        self,
        claim: ClaimedRun,
        source: DeploymentSource,
    ) -> ApprovedImplementation:
        metadata = source.implementation_result_metadata
        if metadata.get("phase") != RunPhase.IMPLEMENTATION.value:
            raise AgentDeploymentError(
                "Deployment source metadata is not an implementation result"
            )
        workspace = metadata.get("workspace")
        if not isinstance(workspace, dict):
            raise AgentDeploymentError(
                "Implementation result is missing workspace evidence"
            )

        base_branch = self._required_string(workspace, "base_branch")
        base_commit = self._required_commit(workspace, "base_commit")
        feature_branch = self._required_string(workspace, "branch")
        worktree_path = self._required_string(workspace, "worktree_path")
        head_commit = self._required_commit(workspace, "head_commit")
        status_porcelain_value = workspace.get("status_porcelain")
        if (
            not isinstance(status_porcelain_value, str)
            or not status_porcelain_value.rstrip("\n")
        ):
            raise AgentDeploymentError(
                "Implementation result is missing status_porcelain"
            )
        status_porcelain = status_porcelain_value.rstrip("\n")
        patch_path_value = self._required_string(workspace, "artifact_patch")
        patch_size_bytes = workspace.get("patch_size_bytes")
        if (
            not isinstance(patch_size_bytes, int)
            or patch_size_bytes < 1
            or patch_size_bytes > 1_000_000
        ):
            raise AgentDeploymentError(
                "Implementation result has an invalid phase-one patch size"
            )
        changed_files_value = workspace.get("changed_files")
        if not isinstance(changed_files_value, list) or not changed_files_value:
            raise AgentDeploymentError(
                "Deployment requires at least one changed implementation file"
            )
        if not all(isinstance(item, str) for item in changed_files_value):
            raise AgentDeploymentError(
                "Implementation changed-file evidence is invalid"
            )
        changed_files = tuple(sorted(set(changed_files_value)))
        if len(changed_files) > 50:
            raise AgentDeploymentError(
                "Phase-one QA deployment permits at most 50 changed files"
            )
        if len(changed_files) != len(changed_files_value):
            raise AgentDeploymentError(
                "Implementation changed-file evidence contains duplicates"
            )

        expected_branch = f"agent/card-{claim.card_id}"
        expected_worktree = self.source_worktree_root / f"card-{claim.card_id}"
        if feature_branch != expected_branch or claim.feature_branch != expected_branch:
            raise AgentDeploymentError(
                "Deployment feature branch does not match the card"
            )
        configured_worktree = Path(worktree_path).expanduser()
        if (
            not configured_worktree.is_absolute()
            or configured_worktree != expected_worktree
            or claim.worktree_path != str(expected_worktree)
        ):
            raise AgentDeploymentError(
                "Deployment worktree path does not match the card"
            )
        self._assert_path_within(
            expected_worktree,
            self.source_worktree_root,
            field="implementation worktree",
        )
        self._verify_worktree(
            expected_worktree,
            expected_branch=feature_branch,
            expected_common_directory=self.source_common_directory,
        )

        if base_branch != (claim.base_branch or "main"):
            raise AgentDeploymentError(
                "Implementation base branch does not match the card"
            )
        if head_commit != base_commit:
            raise AgentDeploymentError(
                "Implementation branch contains commits; deployment candidates "
                "must be created by RemiHub"
            )
        current_head = self._resolve_commit(expected_worktree, "HEAD")
        if current_head != head_commit:
            raise AgentDeploymentError(
                "Implementation worktree HEAD changed after review"
            )

        current_status = self._run_git(
            expected_worktree,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            error_context="Unable to inspect implementation worktree status",
        ).stdout.rstrip()
        if current_status != status_porcelain:
            raise AgentDeploymentError(
                "Implementation worktree status changed after review"
            )
        current_changed_files = self._changed_files(expected_worktree)
        if current_changed_files != changed_files:
            raise AgentDeploymentError(
                "Implementation changed files no longer match review evidence"
            )
        self._require_documentation_only(changed_files, expected_worktree)

        expected_patch_path = (
            self.source_artifact_root
            / claim.card_id
            / f"{source.implementation_run_id}.patch"
        )
        configured_patch_path = Path(patch_path_value).expanduser()
        if (
            not configured_patch_path.is_absolute()
            or configured_patch_path != expected_patch_path
        ):
            raise AgentDeploymentError(
                "Implementation patch path does not match the approved run"
            )
        self._assert_path_within(
            expected_patch_path,
            self.source_artifact_root,
            field="implementation patch",
        )
        if expected_patch_path.is_symlink() or not expected_patch_path.is_file():
            raise AgentDeploymentError("Approved implementation patch is missing")
        approved_patch = expected_patch_path.read_bytes()
        if len(approved_patch) != patch_size_bytes:
            raise AgentDeploymentError(
                "Approved implementation patch size changed after review"
            )
        current_patch = self._build_patch(expected_worktree)
        if current_patch != approved_patch:
            raise AgentDeploymentError(
                "Implementation patch changed after deployment approval"
            )
        self._reject_special_git_modes(approved_patch)
        patch_sha256 = hashlib.sha256(approved_patch).hexdigest()
        expected_tree = self._worktree_tree(expected_worktree, changed_files)

        return ApprovedImplementation(
            approval_id=source.approval_id,
            implementation_run_id=source.implementation_run_id,
            base_branch=base_branch,
            base_commit=base_commit,
            feature_branch=feature_branch,
            worktree_path=str(expected_worktree),
            head_commit=head_commit,
            changed_files=changed_files,
            status_porcelain=status_porcelain,
            patch_path=str(expected_patch_path),
            patch_size_bytes=patch_size_bytes,
            patch_sha256=patch_sha256,
            expected_tree=expected_tree,
        )

    def _materialize_candidate(
        self,
        claim: ClaimedRun,
        approved: ApprovedImplementation,
    ) -> DeploymentCandidate:
        candidate_branch = f"deployment/card-{claim.card_id}/r{claim.card_revision}"
        self._validate_branch_name(
            self.target_repository,
            candidate_branch,
            field="deployment candidate branch",
        )
        candidate_path = (
            self.candidate_worktree_root
            / f"card-{claim.card_id}-r{claim.card_revision}"
        )
        self._assert_path_within(
            candidate_path,
            self.candidate_worktree_root,
            field="deployment candidate worktree",
        )

        observed_target_before = self._resolve_commit(
            self.target_repository,
            self.target_branch,
        )
        existing_candidate = self._branch_commit(candidate_branch)

        if candidate_path.exists() or candidate_path.is_symlink():
            self._verify_worktree(
                candidate_path,
                expected_branch=candidate_branch,
                expected_common_directory=self.target_common_directory,
            )
        elif existing_candidate is not None:
            self._run_git(
                self.target_repository,
                "worktree",
                "add",
                str(candidate_path),
                candidate_branch,
                error_context="Unable to recover the QA deployment candidate",
            )
            self._verify_worktree(
                candidate_path,
                expected_branch=candidate_branch,
                expected_common_directory=self.target_common_directory,
            )
        else:
            if observed_target_before != approved.base_commit:
                raise AgentDeploymentError(
                    "QA target advanced before candidate creation"
                )
            self._run_git(
                self.target_repository,
                "worktree",
                "add",
                "-b",
                candidate_branch,
                str(candidate_path),
                approved.base_commit,
                error_context="Unable to create the QA deployment candidate",
            )
            self._verify_worktree(
                candidate_path,
                expected_branch=candidate_branch,
                expected_common_directory=self.target_common_directory,
            )

        branch_head = self._resolve_commit(candidate_path, "HEAD")
        if branch_head == approved.base_commit:
            self._require_clean(candidate_path)
            self._run_git(
                candidate_path,
                "apply",
                "--index",
                "--binary",
                "--whitespace=error-all",
                "--",
                approved.patch_path,
                error_context="Unable to apply the approved implementation patch",
            )
            staged_files = self._staged_files(candidate_path)
            if staged_files != approved.changed_files:
                raise AgentDeploymentError(
                    "QA candidate changed files do not match approval evidence"
                )
            self._run_git(
                candidate_path,
                "-c",
                "user.name=RemiHub Deployment",
                "-c",
                "user.email=remihub-deployment@invalid.local",
                "commit",
                "--no-gpg-sign",
                "-m",
                f"Deploy agent card {claim.card_id} revision {claim.card_revision}",
                error_context="Unable to create the immutable QA deployment commit",
            )
            candidate_commit = self._resolve_commit(candidate_path, "HEAD")
        else:
            candidate_commit = branch_head

        self._validate_candidate_commit(candidate_commit, approved)
        self._require_clean(candidate_path)

        current_target = self._resolve_commit(
            self.target_repository,
            self.target_branch,
        )
        if current_target == approved.base_commit:
            self._run_git(
                self.target_repository,
                "update-ref",
                f"refs/heads/{self.target_branch}",
                candidate_commit,
                approved.base_commit,
                error_context="Unable to fast-forward the QA deployment target",
            )
        elif current_target != candidate_commit:
            raise AgentDeploymentError(
                "QA deployment target advanced to an unexpected commit"
            )
        target_after = self._resolve_commit(
            self.target_repository,
            self.target_branch,
        )
        if target_after != candidate_commit:
            raise AgentDeploymentError(
                "QA deployment target verification failed"
            )

        manifest_path = self._write_manifest(
            claim,
            approved,
            candidate_branch=candidate_branch,
            candidate_commit=candidate_commit,
            target_before=approved.base_commit,
            target_after=target_after,
        )
        return DeploymentCandidate(
            approval_id=approved.approval_id,
            implementation_run_id=approved.implementation_run_id,
            candidate_branch=candidate_branch,
            candidate_commit=candidate_commit,
            target_branch=self.target_branch,
            target_before=approved.base_commit,
            target_after=target_after,
            base_commit=approved.base_commit,
            changed_files=approved.changed_files,
            patch_sha256=approved.patch_sha256,
            patch_size_bytes=approved.patch_size_bytes,
            manifest_path=str(manifest_path),
        )

    def _validate_candidate_commit(
        self,
        candidate_commit: str,
        approved: ApprovedImplementation,
    ) -> None:
        parent = self._resolve_commit(
            self.target_repository,
            f"{candidate_commit}^",
        )
        if parent != approved.base_commit:
            raise AgentDeploymentError(
                "QA deployment candidate has an unexpected parent commit"
            )
        changed_files = tuple(
            sorted(
                self._run_git(
                    self.target_repository,
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    candidate_commit,
                    error_context="Unable to inspect QA deployment commit",
                ).stdout.splitlines()
            )
        )
        if changed_files != approved.changed_files:
            raise AgentDeploymentError(
                "QA deployment commit differs from approved changed files"
            )
        candidate_tree = self._run_git(
            self.target_repository,
            "rev-parse",
            "--verify",
            f"{candidate_commit}^{{tree}}",
            error_context="Unable to resolve QA deployment candidate tree",
        ).stdout.strip()
        if candidate_tree != approved.expected_tree:
            raise AgentDeploymentError(
                "QA deployment commit tree differs from the approved worktree"
            )

    def _write_manifest(
        self,
        claim: ClaimedRun,
        approved: ApprovedImplementation,
        *,
        candidate_branch: str,
        candidate_commit: str,
        target_before: str,
        target_after: str,
    ) -> Path:
        card_root = self.deployment_artifact_root / claim.card_id
        card_root.mkdir(mode=0o750, exist_ok=True)
        if card_root.is_symlink():
            raise AgentDeploymentError(
                "Deployment artifact directory must not be a symlink"
            )
        self._assert_path_within(
            card_root,
            self.deployment_artifact_root,
            field="deployment artifact directory",
        )
        manifest_path = card_root / f"{claim.id}.candidate.json"
        manifest = {
            "schema_version": 1,
            "environment": "qa",
            "mode": "documentation-only-local-git",
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
            "target_before": target_before,
            "target_after": target_after,
            "changed_files": list(approved.changed_files),
            "patch_sha256": approved.patch_sha256,
            "patch_size_bytes": approved.patch_size_bytes,
            "migrations_applied": [],
            "service_restart_performed": False,
            "android_release_performed": False,
        }
        if manifest_path.exists() or manifest_path.is_symlink():
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise AgentDeploymentError(
                    "Existing deployment manifest is not a regular file"
                )
            try:
                existing_manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise AgentDeploymentError(
                    "Existing deployment manifest is invalid"
                ) from exc
            if existing_manifest != manifest:
                raise AgentDeploymentError(
                    "Existing deployment manifest conflicts with the candidate"
                )
            return manifest_path

        temporary_path = manifest_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.chmod(temporary_path, 0o640)
        temporary_path.replace(manifest_path)
        return manifest_path

    def _require_documentation_only(
        self,
        changed_files: tuple[str, ...],
        worktree: Path,
    ) -> None:
        for relative in changed_files:
            if any(ord(character) < 32 for character in relative):
                raise AgentDeploymentError(
                    "Documentation path contains control characters"
                )
            pure_path = PurePosixPath(relative)
            if (
                pure_path.is_absolute()
                or ".." in pure_path.parts
                or len(pure_path.parts) < 2
                or pure_path.parts[0] != "docs"
                or pure_path.suffix.lower() != ".md"
            ):
                raise AgentDeploymentError(
                    "Phase-one QA deployment permits only docs/*.md changes"
                )
            actual_path = worktree.joinpath(*pure_path.parts)
            if actual_path.is_symlink():
                raise AgentDeploymentError(
                    "Phase-one QA deployment rejects symbolic links"
                )
            if actual_path.exists():
                resolved = actual_path.resolve()
                if worktree.resolve() not in resolved.parents:
                    raise AgentDeploymentError(
                        "Documentation path escapes the implementation worktree"
                    )
                if not actual_path.is_file():
                    raise AgentDeploymentError(
                        "Phase-one QA deployment permits only regular files"
                    )
                try:
                    content = actual_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    raise AgentDeploymentError(
                        "Phase-one QA deployment requires UTF-8 documentation"
                    ) from exc
                if "\x00" in content:
                    raise AgentDeploymentError(
                        "Phase-one QA deployment rejects binary documentation"
                    )

    @staticmethod
    def _reject_special_git_modes(patch: bytes) -> None:
        forbidden = (
            b"new file mode 120000",
            b"old mode 120000",
            b"new file mode 160000",
            b"old mode 160000",
            b"new file mode 100755",
            b"old mode 100755",
            b"new mode 100755",
            b"Subproject commit ",
        )
        if any(marker in patch for marker in forbidden):
            raise AgentDeploymentError(
                "Phase-one QA deployment rejects symlinks and submodules"
            )

    def _worktree_tree(
        self,
        worktree: Path,
        changed_files: tuple[str, ...],
    ) -> str:
        with tempfile.TemporaryDirectory(
            prefix="remihub-deployment-index-",
            dir=self.deployment_artifact_root,
        ) as temporary_directory:
            index_path = Path(temporary_directory) / "index"
            environment_overrides = {"GIT_INDEX_FILE": str(index_path)}
            self._run_git(
                worktree,
                "read-tree",
                "HEAD",
                environment_overrides=environment_overrides,
                error_context="Unable to initialize deployment validation index",
            )
            self._run_git(
                worktree,
                "add",
                "-A",
                "--",
                *changed_files,
                environment_overrides=environment_overrides,
                error_context="Unable to stage deployment validation tree",
            )
            return self._run_git(
                worktree,
                "write-tree",
                environment_overrides=environment_overrides,
                error_context="Unable to create deployment validation tree",
            ).stdout.strip()

    def _build_patch(self, worktree: Path) -> bytes:
        tracked = self._run_git_bytes(
            worktree,
            "diff",
            "--binary",
            "--no-ext-diff",
            "HEAD",
            "--",
            error_context="Unable to recreate the implementation patch",
        ).stdout
        untracked_parts: list[bytes] = []
        for relative in self._untracked_files(worktree):
            result = self._run_git_bytes(
                worktree,
                "diff",
                "--no-index",
                "--binary",
                "--",
                "/dev/null",
                relative,
                allowed_return_codes=(0, 1),
                error_context="Unable to include an untracked implementation file",
            )
            untracked_parts.append(result.stdout)
        return tracked + b"".join(untracked_parts)

    def _changed_files(self, worktree: Path) -> tuple[str, ...]:
        tracked = self._run_git(
            worktree,
            "diff",
            "--name-only",
            "--no-ext-diff",
            "HEAD",
            "--",
            error_context="Unable to list implementation changes",
        ).stdout.splitlines()
        return tuple(sorted(set(tracked) | set(self._untracked_files(worktree))))

    def _untracked_files(self, worktree: Path) -> tuple[str, ...]:
        result = self._run_git_bytes(
            worktree,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            error_context="Unable to list untracked implementation files",
        )
        return tuple(
            sorted(
                item.decode("utf-8", errors="surrogateescape")
                for item in result.stdout.split(b"\0")
                if item
            )
        )

    def _staged_files(self, worktree: Path) -> tuple[str, ...]:
        return tuple(
            sorted(
                self._run_git(
                    worktree,
                    "diff",
                    "--cached",
                    "--name-only",
                    "--no-ext-diff",
                    "--",
                    error_context="Unable to inspect staged QA changes",
                ).stdout.splitlines()
            )
        )

    def _require_clean(self, worktree: Path) -> None:
        status = self._run_git(
            worktree,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            error_context="Unable to inspect QA candidate worktree",
        ).stdout.strip()
        if status:
            raise AgentDeploymentError(
                "QA candidate worktree contains unexpected changes"
            )

    def _branch_commit(self, branch: str) -> str | None:
        result = self._run_git(
            self.target_repository,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}^{{commit}}",
            allowed_return_codes=(0, 1),
            error_context="Unable to inspect QA candidate branch",
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def _verify_worktree(
        self,
        path: Path,
        *,
        expected_branch: str,
        expected_common_directory: Path,
    ) -> None:
        if path.is_symlink() or not path.is_dir():
            raise AgentDeploymentError("Expected Git worktree is missing")
        top_level = Path(
            self._run_git(
                path,
                "rev-parse",
                "--show-toplevel",
                error_context="Deployment path is not a Git worktree",
            ).stdout.strip()
        ).resolve()
        if top_level != path.resolve():
            raise AgentDeploymentError("Deployment path is not a worktree root")
        if self._common_git_directory(path) != expected_common_directory:
            raise AgentDeploymentError(
                "Deployment worktree belongs to an unexpected Git repository"
            )
        branch = self._run_git(
            path,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            error_context="Deployment worktree must use a named branch",
        ).stdout.strip()
        if branch != expected_branch:
            raise AgentDeploymentError(
                "Deployment worktree is checked out on an unexpected branch"
            )

    @staticmethod
    def _required_string(
        mapping: dict,
        key: str,
        *,
        allow_empty: bool = False,
    ) -> str:
        value = mapping.get(key)
        if not isinstance(value, str):
            raise AgentDeploymentError(
                f"Implementation result is missing {key}"
            )
        normalized = value.strip()
        if not allow_empty and not normalized:
            raise AgentDeploymentError(
                f"Implementation result is missing {key}"
            )
        return normalized

    @classmethod
    def _required_commit(cls, mapping: dict, key: str) -> str:
        value = cls._required_string(mapping, key)
        invalid_character = any(
            character not in "0123456789abcdef" for character in value
        )
        if len(value) != 40 or invalid_character:
            raise AgentDeploymentError(
                f"Implementation result has an invalid {key}"
            )
        return value

    def _resolve_commit(self, repository: Path, reference: str) -> str:
        return self._run_git(
            repository,
            "rev-parse",
            "--verify",
            f"{reference}^{{commit}}",
            error_context=f"Unable to resolve Git reference: {reference}",
        ).stdout.strip()

    def _validate_branch_name(
        self,
        repository: Path,
        value: str,
        *,
        field: str,
    ) -> str:
        normalized = value.strip()
        if not normalized:
            raise AgentWorkerConfigurationError(f"{field} must not be blank")
        self._run_git(
            repository,
            "check-ref-format",
            "--branch",
            normalized,
            error_context=f"Invalid {field}",
        )
        return normalized

    def _common_git_directory(self, repository: Path) -> Path:
        value = self._run_git(
            repository,
            "rev-parse",
            "--git-common-dir",
            error_context="Unable to resolve Git common directory",
        ).stdout.strip()
        path = Path(value)
        if not path.is_absolute():
            path = repository / path
        return path.resolve()

    @staticmethod
    def _assert_path_within(path: Path, root: Path, *, field: str) -> None:
        resolved_root = root.resolve()
        resolved_parent = path.parent.resolve()
        if (
            resolved_parent != resolved_root
            and resolved_root not in resolved_parent.parents
        ):
            raise AgentDeploymentError(f"{field} escapes its configured root")

    @staticmethod
    def _existing_absolute_directory(
        value: str | Path,
        *,
        field: str,
    ) -> Path:
        configured = Path(value).expanduser()
        if not configured.is_absolute():
            raise AgentWorkerConfigurationError(f"{field} must be an absolute path")
        resolved = configured.resolve()
        if not resolved.is_dir():
            raise AgentWorkerConfigurationError(
                f"{field} does not exist or is not a directory: {resolved}"
            )
        return resolved

    def _git_environment(self) -> dict[str, str]:
        return {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": os.environ.get("HOME", "/nonexistent"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }

    def _run_git(
        self,
        repository: Path,
        *arguments: str,
        allowed_return_codes: Sequence[int] = (0,),
        environment_overrides: dict[str, str] | None = None,
        error_context: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = self._git_environment()
        if environment_overrides:
            environment.update(environment_overrides)
        try:
            result = subprocess.run(
                [self.git_binary, "-C", str(repository), *arguments],
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=self.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentDeploymentError(error_context) from exc
        if result.returncode not in allowed_return_codes:
            detail = result.stderr.strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise AgentDeploymentError(f"{error_context}{suffix}")
        return result

    def _run_git_bytes(
        self,
        repository: Path,
        *arguments: str,
        allowed_return_codes: Sequence[int] = (0,),
        error_context: str,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                [self.git_binary, "-C", str(repository), *arguments],
                check=False,
                capture_output=True,
                env=self._git_environment(),
                timeout=self.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentDeploymentError(error_context) from exc
        if result.returncode not in allowed_return_codes:
            raise AgentDeploymentError(error_context)
        return result


class GitQaDeploymentExecutor:
    allowed_phases = frozenset({RunPhase.DEPLOYMENT})

    def __init__(self, *, deployment_manager: GitQaDeploymentManager):
        self.deployment_manager = deployment_manager

    def execute(self, claim: ClaimedRun) -> ExecutionResult:
        candidate = self.deployment_manager.deploy(claim)
        changed_files = "\n".join(
            f"- `{path}`" for path in candidate.changed_files
        )
        message = f"""
Created and deployed an immutable documentation-only QA candidate.

- Candidate commit: `{candidate.candidate_commit}`
- Target branch: `{candidate.target_branch}`
- Service restart performed: no
- Migrations applied: none

Changed files:
{changed_files}
""".strip()
        return ExecutionResult(
            message=message,
            card_status=CardStatus.COMPLETED,
            metadata={
                "executor": "git_qa_deployment",
                "phase": claim.phase.value,
                "environment": "qa",
                "mode": "documentation-only-local-git",
                "candidate": asdict(candidate),
            },
        )

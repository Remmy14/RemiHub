from __future__ import annotations

import fcntl
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

from backend.core.agent_state import RunPhase
from backend.core.agent_worker import (
    AgentWorkerConfigurationError,
    ClaimedRun,
)


class AgentWorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImplementationWorkspace:
    source_repository: Path
    path: Path
    base_branch: str
    feature_branch: str
    base_commit: str


@dataclass(frozen=True)
class WorkspaceSnapshot:
    branch: str
    head_commit: str
    changed_files: tuple[str, ...]
    status_porcelain: str
    diff_stat: str
    patch_path: Path
    patch_size_bytes: int


WorkspacePersistence = Callable[[str, str], None]


class GitImplementationWorkspaceManager:
    """Create and recover per-card Git worktrees without remote credentials."""

    def __init__(
        self,
        *,
        source_repository: str | Path,
        worktree_root: str | Path,
        artifact_root: str | Path,
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
        self.worktree_root = self._existing_absolute_directory(
            worktree_root,
            field="REMIHUB_AGENT_WORKTREE_ROOT",
        )
        self.artifact_root = self._existing_absolute_directory(
            artifact_root,
            field="REMIHUB_AGENT_ARTIFACT_ROOT",
        )

        self._run_git(
            self.source_repository,
            "rev-parse",
            "--git-dir",
            error_context="The implementation source is not a Git repository",
        )
        self.source_common_directory = self._common_git_directory(
            self.source_repository
        )

        self.lock_root = self.worktree_root / ".locks"
        self.lock_root.mkdir(mode=0o750, exist_ok=True)
        if self.lock_root.is_symlink():
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_WORKTREE_ROOT/.locks must not be a symlink"
            )

    @contextmanager
    def locked_workspace(
        self,
        claim: ClaimedRun,
        *,
        persist_workspace: WorkspacePersistence,
    ) -> Iterator[ImplementationWorkspace]:
        if claim.phase is not RunPhase.IMPLEMENTATION:
            raise AgentWorkspaceError(
                "Implementation workspaces are limited to implementation runs"
            )

        lock_path = self.lock_root / f"{claim.card_id}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            os.chmod(lock_path, 0o640)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            workspace = self._prepare_workspace(
                claim,
                persist_workspace=persist_workspace,
            )
            try:
                yield workspace
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def capture_snapshot(
        self,
        claim: ClaimedRun,
        workspace: ImplementationWorkspace,
    ) -> WorkspaceSnapshot:
        self._verify_workspace(workspace.path, workspace.feature_branch)
        status_porcelain = self._run_git(
            workspace.path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            error_context="Unable to inspect implementation workspace status",
        ).stdout.rstrip()
        changed_files = self._changed_files(workspace.path)
        diff_stat = self._run_git(
            workspace.path,
            "diff",
            "--stat",
            "--no-ext-diff",
            "HEAD",
            "--",
            error_context="Unable to summarize implementation changes",
        ).stdout.rstrip()
        head_commit = self._run_git(
            workspace.path,
            "rev-parse",
            "HEAD^{commit}",
            error_context="Unable to resolve implementation HEAD",
        ).stdout.strip()
        patch_path = self._write_patch(claim, workspace.path)

        return WorkspaceSnapshot(
            branch=workspace.feature_branch,
            head_commit=head_commit,
            changed_files=changed_files,
            status_porcelain=status_porcelain,
            diff_stat=diff_stat,
            patch_path=patch_path,
            patch_size_bytes=patch_path.stat().st_size,
        )

    def _prepare_workspace(
        self,
        claim: ClaimedRun,
        *,
        persist_workspace: WorkspacePersistence,
    ) -> ImplementationWorkspace:
        if bool(claim.feature_branch) != bool(claim.worktree_path):
            raise AgentWorkspaceError(
                "Card implementation workspace metadata is incomplete"
            )

        expected_branch = self._expected_feature_branch(claim.card_id)
        expected_path = self.worktree_root / f"card-{claim.card_id}"
        base_branch = self._validate_branch_name(
            claim.base_branch or "main",
            field="base branch",
        )

        if claim.feature_branch:
            feature_branch = self._validate_branch_name(
                claim.feature_branch,
                field="feature branch",
            )
            if feature_branch != expected_branch:
                raise AgentWorkspaceError(
                    "Card feature branch does not match its deterministic branch"
                )
            configured_path = Path(claim.worktree_path).expanduser()
            if not configured_path.is_absolute():
                raise AgentWorkspaceError("Card worktree path must be absolute")
            if configured_path != expected_path:
                raise AgentWorkspaceError(
                    "Card worktree path does not match its deterministic path"
                )
        else:
            feature_branch = expected_branch

        self._assert_path_within_root(expected_path)
        self._resolve_commit(base_branch)

        if expected_path.exists() or expected_path.is_symlink():
            self._verify_workspace(expected_path, feature_branch)
        elif self._branch_exists(feature_branch):
            self._run_git(
                self.source_repository,
                "worktree",
                "add",
                str(expected_path),
                feature_branch,
                error_context="Unable to recover the implementation worktree",
            )
            self._verify_workspace(expected_path, feature_branch)
        else:
            self._run_git(
                self.source_repository,
                "worktree",
                "add",
                "-b",
                feature_branch,
                str(expected_path),
                base_branch,
                error_context="Unable to create the implementation worktree",
            )
            self._verify_workspace(expected_path, feature_branch)

        base_commit = self._implementation_base_commit(
            expected_path,
            base_branch=base_branch,
            feature_branch=feature_branch,
        )

        if not claim.feature_branch:
            persist_workspace(feature_branch, str(expected_path))

        return ImplementationWorkspace(
            source_repository=self.source_repository,
            path=expected_path,
            base_branch=base_branch,
            feature_branch=feature_branch,
            base_commit=base_commit,
        )

    def _verify_workspace(self, path: Path, feature_branch: str) -> None:
        self._assert_path_within_root(path)
        if path.is_symlink():
            raise AgentWorkspaceError("Implementation worktree must not be a symlink")
        if not path.is_dir():
            raise AgentWorkspaceError(
                f"Implementation worktree does not exist: {path}"
            )

        top_level = Path(
            self._run_git(
                path,
                "rev-parse",
                "--show-toplevel",
                error_context="Implementation path is not a Git worktree",
            ).stdout.strip()
        ).resolve()
        if top_level != path.resolve():
            raise AgentWorkspaceError(
                "Implementation path is not the Git worktree root"
            )

        actual_common_directory = self._common_git_directory(path)
        if actual_common_directory != self.source_common_directory:
            raise AgentWorkspaceError(
                "Implementation worktree belongs to a different Git repository"
            )

        actual_branch = self._run_git(
            path,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            error_context="Implementation worktree must use a named branch",
        ).stdout.strip()
        if actual_branch != feature_branch:
            raise AgentWorkspaceError(
                "Implementation worktree is checked out on an unexpected branch"
            )

    def _write_patch(self, claim: ClaimedRun, worktree_path: Path) -> Path:
        card_artifact_root = self.artifact_root / claim.card_id
        card_artifact_root.mkdir(mode=0o750, exist_ok=True)
        if card_artifact_root.is_symlink():
            raise AgentWorkspaceError("Card artifact directory must not be a symlink")
        self._assert_path_within(
            card_artifact_root,
            self.artifact_root,
            field="artifact directory",
        )

        patch_path = card_artifact_root / f"{claim.id}.patch"
        tracked_patch = self._run_git(
            worktree_path,
            "diff",
            "--binary",
            "--no-ext-diff",
            "HEAD",
            "--",
            error_context="Unable to create the implementation patch",
        ).stdout
        untracked_patch_parts: list[str] = []
        for relative_path in self._untracked_files(worktree_path):
            result = self._run_git(
                worktree_path,
                "diff",
                "--no-index",
                "--binary",
                "--",
                "/dev/null",
                relative_path,
                allowed_return_codes=(0, 1),
                error_context="Unable to include an untracked implementation file",
            )
            untracked_patch_parts.append(result.stdout)

        temporary_path = patch_path.with_suffix(".patch.tmp")
        temporary_path.write_text(
            tracked_patch + "".join(untracked_patch_parts),
            encoding="utf-8",
            newline="\n",
        )
        os.chmod(temporary_path, 0o640)
        temporary_path.replace(patch_path)
        return patch_path

    def _changed_files(self, worktree_path: Path) -> tuple[str, ...]:
        tracked = self._run_git(
            worktree_path,
            "diff",
            "--name-only",
            "--no-ext-diff",
            "HEAD",
            "--",
            error_context="Unable to list changed implementation files",
        ).stdout.splitlines()
        return tuple(sorted(set(tracked) | set(self._untracked_files(worktree_path))))

    def _untracked_files(self, worktree_path: Path) -> tuple[str, ...]:
        result = self._run_git_bytes(
            worktree_path,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            error_context="Unable to list untracked implementation files",
        )
        paths = [
            item.decode("utf-8", errors="surrogateescape")
            for item in result.stdout.split(b"\0")
            if item
        ]
        return tuple(sorted(paths))

    def _branch_exists(self, branch: str) -> bool:
        result = self._run_git(
            self.source_repository,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            allowed_return_codes=(0, 1),
            error_context="Unable to inspect implementation branch",
        )
        return result.returncode == 0

    def _resolve_commit(self, reference: str) -> str:
        return self._run_git(
            self.source_repository,
            "rev-parse",
            "--verify",
            f"{reference}^{{commit}}",
            error_context=f"Implementation base branch does not exist: {reference}",
        ).stdout.strip()

    def _implementation_base_commit(
        self,
        worktree_path: Path,
        *,
        base_branch: str,
        feature_branch: str,
    ) -> str:
        head_commit = self._resolve_commit_at(worktree_path, "HEAD")
        merge_base = self._run_git(
            worktree_path,
            "merge-base",
            feature_branch,
            base_branch,
            error_context="Unable to resolve the implementation branch base",
        ).stdout.strip()
        if head_commit != merge_base:
            raise AgentWorkspaceError(
                "Implementation branch contains commits; executor work must remain "
                "uncommitted for review"
            )
        return merge_base

    def _resolve_commit_at(self, repository: Path, reference: str) -> str:
        return self._run_git(
            repository,
            "rev-parse",
            "--verify",
            f"{reference}^{{commit}}",
            error_context=f"Unable to resolve Git reference: {reference}",
        ).stdout.strip()

    def _validate_branch_name(self, value: str, *, field: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise AgentWorkspaceError(f"{field} must not be blank")
        self._run_git(
            self.source_repository,
            "check-ref-format",
            "--branch",
            normalized,
            error_context=f"Invalid {field}",
        )
        return normalized

    @staticmethod
    def _expected_feature_branch(card_id: str) -> str:
        return f"agent/card-{card_id}"

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

    def _assert_path_within_root(self, path: Path) -> None:
        self._assert_path_within(
            path,
            self.worktree_root,
            field="worktree path",
        )

    @staticmethod
    def _assert_path_within(path: Path, root: Path, *, field: str) -> None:
        resolved_root = root.resolve()
        resolved_parent = path.parent.resolve()
        if (
            resolved_parent != resolved_root
            and resolved_root not in resolved_parent.parents
        ):
            raise AgentWorkspaceError(f"{field} escapes its configured root")

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

    def _run_git(
        self,
        repository: Path,
        *arguments: str,
        allowed_return_codes: Sequence[int] = (0,),
        error_context: str,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.git_binary, "-C", str(repository), *arguments]
        environment = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": os.environ.get("HOME", "/nonexistent"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=self.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentWorkspaceError(error_context) from exc
        if result.returncode not in allowed_return_codes:
            detail = result.stderr.strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise AgentWorkspaceError(f"{error_context}{suffix}")
        return result

    def _run_git_bytes(
        self,
        repository: Path,
        *arguments: str,
        error_context: str,
    ) -> subprocess.CompletedProcess[bytes]:
        command = [self.git_binary, "-C", str(repository), *arguments]
        environment = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": os.environ.get("HOME", "/nonexistent"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                env=environment,
                timeout=self.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentWorkspaceError(error_context) from exc
        if result.returncode != 0:
            raise AgentWorkspaceError(error_context)
        return result

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, Sequence

from backend.config import load_config
from backend.core.agent_state import (
    CardStatus,
    RepositoryScope,
    RunPhase,
    require_backend_repository_scope,
)
from backend.core.agent_worker import (
    AgentTemporarilyBlockedError,
    AgentWorkerConfigurationError,
    ClaimedRun,
    DeploymentSource,
    ExecutionResult,
)
from backend.database import migration_runner


class AgentDeploymentError(RuntimeError):
    pass


class DeploymentValidationError(AgentDeploymentError):
    pass


class DeploymentRollbackError(AgentDeploymentError):
    pass


class DeploymentRolledBackError(AgentDeploymentError):
    """A deployment failed, but code/database state was restored safely."""


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
    implementation_tests: tuple[dict, ...]


@dataclass(frozen=True)
class MigrationPlan:
    versions: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    expected_history: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class ValidationEvidence:
    command: str
    duration_ms: int
    stdout_sha256: str
    stdout_tail: str
    stderr_tail: str


@dataclass(frozen=True)
class FrontendArtifactEvidence:
    changed: bool
    artifact_directory: str | None = None
    archive_path: str | None = None
    archive_sha256: str | None = None
    manifest_path: str | None = None
    manifest_sha256: str | None = None
    artifact_identity: str | None = None
    lockfile_sha256: str | None = None
    node_version: str | None = None
    npm_version: str | None = None
    commands: tuple[dict[str, Any], ...] = ()
    reproducibility: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BackupEvidence:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class RuntimeHealth:
    service_active: bool
    url: str
    status_code: int
    response_sha256: str


@dataclass(frozen=True)
class DeploymentCandidate:
    approval_id: str
    implementation_run_id: str
    environment: str
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
    rollback_ref: str
    validation: dict
    frontend_artifact: dict | None = None
    service_restart_performed: bool = False
    migrations_applied: tuple[str, ...] = ()
    database_backup: dict | None = None
    rollback_performed: bool = False


GITHUB_SYNC_LOCAL_INCOMPLETE = "local_deployment_incomplete"
GITHUB_SYNC_PENDING = "github_sync_pending"
GITHUB_SYNC_RUNNING = "github_sync_running"
GITHUB_SYNC_FAILED_RETRYABLE = "github_sync_failed_retryable"
GITHUB_SYNC_SUCCEEDED = "github_sync_succeeded"
GITHUB_SYNC_FAILED_NON_RETRYABLE = "github_sync_failed_non_retryable"

GITHUB_SYNC_RETRY_ACTION = "retry_github_sync"

GITHUB_SYNC_RETRYABLE_BLOCKERS = frozenset(
    {
        "github_sync_pending",
        "github_sync_failed",
        "github_sync_helper_unavailable",
        "github_sync_timeout",
        "github_sync_canonical_dirty",
        "github_sync_health_failed",
        "github_sync_manifest_pending",
    }
)
GITHUB_SYNC_NON_RETRYABLE_BLOCKERS = frozenset(
    {
        "github_sync_remote_divergent",
        "github_sync_integrity_failure",
    }
)


def github_sync_blocker_code(error: str) -> str:
    normalized = error.lower()
    if "remote" in normalized and (
        "ancestor" in normalized
        or "diverg" in normalized
        or "non-fast-forward" in normalized
        or "neither the expected base nor candidate" in normalized
    ):
        return "github_sync_remote_divergent"
    if (
        "protected repository refs" in normalized
        or "canonical commit does not match" in normalized
        or "canonical tree does not match" in normalized
        or "production target" in normalized
        or "implementation main" in normalized
        or "planning checkout changed" in normalized
        or "canonical commit changed" in normalized
    ):
        return "github_sync_integrity_failure"
    if "canonical worktree has an unexpected dirty state" in normalized:
        return "github_sync_canonical_dirty"
    if "health" in normalized:
        return "github_sync_health_failed"
    if "could not be executed" in normalized:
        return "github_sync_helper_unavailable"
    if "timed out" in normalized or "timeoutexpired" in normalized:
        return "github_sync_timeout"
    return "github_sync_failed"


def github_sync_retryable(blocker_code: str) -> bool:
    if blocker_code in GITHUB_SYNC_NON_RETRYABLE_BLOCKERS:
        return False
    return blocker_code in GITHUB_SYNC_RETRYABLE_BLOCKERS


def deployment_recovery_metadata(
    *,
    github_sync_status: str,
    retryable: bool,
    blocker_code: str | None,
    last_error: str | None,
    candidate_commit: str | None,
    deployment_run_id: str,
    production_deployed: bool,
) -> dict[str, Any]:
    return {
        "github_sync_status": github_sync_status,
        "retryable": retryable,
        "blocker_code": blocker_code,
        "last_error": last_error,
        "candidate_commit": candidate_commit,
        "deployment_run_id": deployment_run_id,
        "production_deployed": production_deployed,
    }


class BackendValidator(Protocol):
    def validate(self, candidate_worktree: Path) -> ValidationEvidence: ...


class FrontendArtifactBuilder(Protocol):
    def build(
        self,
        *,
        candidate_worktree: Path,
        artifact_root: Path,
        card_id: str,
        card_revision: int,
        deployment_run_id: str,
        approval_id: str,
        implementation_run_id: str,
        candidate_commit: str,
        changed_files: tuple[str, ...],
    ) -> FrontendArtifactEvidence: ...


class DeploymentDatabase(Protocol):
    def migration_history(self) -> tuple[dict[str, str], ...]: ...

    def pending_versions(self, migrations_dir: Path) -> tuple[str, ...]: ...

    def backup(self, *, card_id: str, deployment_run_id: str) -> BackupEvidence: ...

    def upgrade(
        self,
        migrations_dir: Path,
        expected_versions: tuple[str, ...],
    ) -> tuple[str, ...]: ...

    def downgrade(
        self,
        migrations_dir: Path,
        versions: tuple[str, ...],
    ) -> tuple[str, ...]: ...


class MigrationHistoryReader(Protocol):
    def migration_history(self) -> tuple[dict[str, str], ...]: ...

    def database_identity(self) -> tuple[str | None, ...]: ...


class DeploymentRuntime(Protocol):
    def stop(self) -> None: ...

    def start(self) -> None: ...

    def promote(
        self,
        *,
        candidate_branch: str,
        candidate_commit: str,
        expected_before: str,
        rollback_ref: str,
    ) -> None: ...

    def restore(self, *, expected_current: str, rollback_commit: str) -> None: ...

    def frontend_install(
        self,
        *,
        artifact_manifest: str,
        artifact_archive: str,
        artifact_identity: str,
        candidate_commit: str,
        card_id: str,
        deployment_run_id: str,
    ) -> dict[str, Any]: ...

    def frontend_restore(self) -> dict[str, Any]: ...

    def frontend_verify(
        self,
        *,
        artifact_manifest: str,
        artifact_identity: str,
    ) -> dict[str, Any]: ...

    def synchronize_sources(
        self,
        *,
        candidate_branch: str,
        candidate_commit: str,
        expected_before: str,
        rollback_ref: str,
    ) -> None: ...

    def restore_sources(self, *, expected_current: str, rollback_commit: str) -> None: ...

    def verify(self) -> RuntimeHealth: ...


class BackendGitHubSynchronizer(Protocol):
    def synchronize(
        self,
        *,
        candidate_commit: str,
        base_commit: str,
        card_id: str,
        deployment_run_id: str,
    ) -> dict[str, Any]: ...


class SandboxBackendValidator:
    def __init__(
        self,
        *,
        validation_command: str | Path,
        timeout_seconds: int = 900,
    ):
        command = Path(validation_command).expanduser()
        if not command.is_absolute():
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_DEPLOYMENT_VALIDATOR must be an absolute path"
            )
        if not command.is_file() or not os.access(command, os.X_OK):
            raise AgentWorkerConfigurationError(
                f"Deployment validator is not executable: {command}"
            )
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")
        self.validation_command = command.resolve()
        self.timeout_seconds = timeout_seconds

    def validate(self, candidate_worktree: Path) -> ValidationEvidence:
        started = time.monotonic()
        try:
            result = subprocess.run(
                [str(self.validation_command), str(candidate_worktree)],
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env={
                    "HOME": "/nonexistent",
                    "LANG": os.environ.get("LANG", "C.UTF-8"),
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                },
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentValidationError(
                "Unable to execute the isolated backend validator"
            ) from exc
        duration_ms = round((time.monotonic() - started) * 1000)
        stdout = result.stdout
        stderr = result.stderr
        evidence = ValidationEvidence(
            command=f"{self.validation_command} {candidate_worktree}",
            duration_ms=duration_ms,
            stdout_sha256=hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            stdout_tail=_tail(stdout, 8000),
            stderr_tail=_tail(stderr, 8000),
        )
        if result.returncode != 0:
            detail = evidence.stderr_tail or evidence.stdout_tail
            suffix = f": {detail}" if detail else ""
            raise DeploymentValidationError(
                f"Backend compile/test validation failed{suffix}"
            )
        return evidence


FRONTEND_WEB_POLICY = {
    "node_version": "v22.22.2",
    "npm_version": "10.9.7",
    "root": "frontend-web",
    "lockfile": "frontend-web/package-lock.json",
    "prepare_command": ("npm", "ci", "--ignore-scripts"),
    "lint_command": ("npm", "run", "lint"),
    "build_command": ("npm", "run", "build"),
    "archive_mtime": 0,
    "archive_uid": 0,
    "archive_gid": 0,
    "archive_uname": "root",
    "archive_gname": "root",
    "directory_mode": 0o755,
    "file_mode": 0o644,
}


def frontend_web_changed(changed_files: tuple[str, ...]) -> bool:
    return any(path == "frontend-web" or path.startswith("frontend-web/") for path in changed_files)


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _frontend_artifact_manifest(
    dist_root: Path,
    *,
    candidate_commit: str,
    lockfile_sha256: str,
) -> dict[str, Any]:
    if dist_root.is_symlink() or not dist_root.is_dir():
        raise DeploymentValidationError("Frontend build did not produce dist")
    entries: list[dict[str, Any]] = []
    for directory, names, files in os.walk(dist_root, topdown=True, followlinks=False):
        names.sort()
        files.sort()
        current = Path(directory)
        relative_directory = current.relative_to(dist_root)
        for name in names:
            child = current / name
            relative = child.relative_to(dist_root).as_posix()
            if child.is_symlink():
                raise DeploymentValidationError("Frontend artifact rejects symbolic links")
            if not child.is_dir():
                raise DeploymentValidationError("Frontend artifact rejects special files")
            if any(ord(character) < 32 for character in relative) or ".." in PurePosixPath(relative).parts:
                raise DeploymentValidationError("Frontend artifact contains an unsafe path")
            entries.append(
                {
                    "path": relative,
                    "type": "directory",
                    "mode": "0755",
                }
            )
        for name in files:
            child = current / name
            relative = child.relative_to(dist_root).as_posix()
            if child.is_symlink():
                raise DeploymentValidationError("Frontend artifact rejects symbolic links")
            try:
                mode = child.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise DeploymentValidationError("Frontend artifact cannot be inspected") from exc
            if not stat.S_ISREG(mode):
                raise DeploymentValidationError("Frontend artifact rejects special files")
            if mode & 0o111:
                raise DeploymentValidationError("Frontend artifact rejects executable files")
            if any(ord(character) < 32 for character in relative) or ".." in PurePosixPath(relative).parts:
                raise DeploymentValidationError("Frontend artifact contains an unsafe path")
            size = child.stat(follow_symlinks=False).st_size
            if size > 10_000_000:
                raise DeploymentValidationError("Frontend artifact file is too large")
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "mode": "0644",
                    "size": size,
                    "sha256": _sha256_file(child),
                }
            )
    entries.sort(key=lambda item: (item["path"], item["type"]))
    manifest = {
        "schema_version": 1,
        "frontend_root": "frontend-web/dist",
        "candidate_commit": candidate_commit,
        "lockfile_sha256": lockfile_sha256,
        "entries": entries,
    }
    manifest["artifact_identity"] = hashlib.sha256(
        _canonical_json_bytes(manifest)
    ).hexdigest()
    return manifest


def _write_deterministic_frontend_archive(
    dist_root: Path,
    archive_path: Path,
    manifest: dict[str, Any],
) -> None:
    with tarfile.open(archive_path, "w", format=tarfile.PAX_FORMAT) as archive:
        for entry in manifest["entries"]:
            source = dist_root / entry["path"]
            info = archive.gettarinfo(str(source), arcname=entry["path"])
            info.mtime = FRONTEND_WEB_POLICY["archive_mtime"]
            info.uid = FRONTEND_WEB_POLICY["archive_uid"]
            info.gid = FRONTEND_WEB_POLICY["archive_gid"]
            info.uname = FRONTEND_WEB_POLICY["archive_uname"]
            info.gname = FRONTEND_WEB_POLICY["archive_gname"]
            info.pax_headers = {}
            if entry["type"] == "directory":
                info.mode = FRONTEND_WEB_POLICY["directory_mode"]
                archive.addfile(info)
            else:
                info.mode = FRONTEND_WEB_POLICY["file_mode"]
                with source.open("rb") as file_object:
                    archive.addfile(info, file_object)
    os.chmod(archive_path, 0o640)


def verify_frontend_archive(
    *,
    archive_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    if archive_path.is_symlink() or not archive_path.is_file():
        raise DeploymentValidationError("Frontend artifact archive is missing")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise DeploymentValidationError("Frontend artifact manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_entries = manifest.get("entries")
    if not isinstance(expected_entries, list):
        raise DeploymentValidationError("Frontend artifact manifest is invalid")
    expected_names = [entry["path"] for entry in expected_entries]
    with tarfile.open(archive_path, "r:*") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if names != expected_names:
            raise DeploymentValidationError("Frontend artifact archive entry order differs from manifest")
        for member, entry in zip(members, expected_entries):
            if member.mtime != FRONTEND_WEB_POLICY["archive_mtime"]:
                raise DeploymentValidationError("Frontend artifact archive has non-deterministic timestamps")
            if (
                member.uid != FRONTEND_WEB_POLICY["archive_uid"]
                or member.gid != FRONTEND_WEB_POLICY["archive_gid"]
                or member.uname != FRONTEND_WEB_POLICY["archive_uname"]
                or member.gname != FRONTEND_WEB_POLICY["archive_gname"]
            ):
                raise DeploymentValidationError("Frontend artifact archive has non-deterministic ownership")
            expected_mode = (
                FRONTEND_WEB_POLICY["directory_mode"]
                if entry["type"] == "directory"
                else FRONTEND_WEB_POLICY["file_mode"]
            )
            if member.mode != expected_mode:
                raise DeploymentValidationError("Frontend artifact archive has non-deterministic modes")
            if entry["type"] == "directory" and not member.isdir():
                raise DeploymentValidationError("Frontend artifact archive type mismatch")
            if entry["type"] == "file":
                if not member.isfile() or member.size != entry["size"]:
                    raise DeploymentValidationError("Frontend artifact archive file metadata mismatch")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise DeploymentValidationError("Frontend artifact archive file cannot be read")
                if hashlib.sha256(extracted.read()).hexdigest() != entry["sha256"]:
                    raise DeploymentValidationError("Frontend artifact archive content differs from manifest")
    identity_manifest = {key: manifest[key] for key in ("schema_version", "frontend_root", "candidate_commit", "lockfile_sha256", "entries")}
    identity = hashlib.sha256(_canonical_json_bytes(identity_manifest)).hexdigest()
    if identity != manifest.get("artifact_identity"):
        raise DeploymentValidationError("Frontend artifact identity does not match manifest")
    return {
        "artifact_identity": identity,
        "archive_sha256": _sha256_file(archive_path),
        "manifest_sha256": _sha256_file(manifest_path),
    }


def _frontend_artifact_from_manifest(
    manifest: dict[str, Any],
) -> FrontendArtifactEvidence | None:
    raw = manifest.get("frontend_artifact")
    if not isinstance(raw, dict) or not raw.get("changed"):
        return None
    required = ("manifest_path", "archive_path", "artifact_identity")
    if not all(isinstance(raw.get(key), str) and raw[key] for key in required):
        raise AgentDeploymentError("Existing deployment manifest has invalid frontend artifact evidence")
    return FrontendArtifactEvidence(
        changed=True,
        artifact_directory=raw.get("artifact_directory"),
        archive_path=raw.get("archive_path"),
        archive_sha256=raw.get("archive_sha256"),
        manifest_path=raw.get("manifest_path"),
        manifest_sha256=raw.get("manifest_sha256"),
        artifact_identity=raw.get("artifact_identity"),
        lockfile_sha256=raw.get("lockfile_sha256"),
        node_version=raw.get("node_version"),
        npm_version=raw.get("npm_version"),
        commands=tuple(raw.get("commands") or ()),
        reproducibility=dict(raw.get("reproducibility") or {}),
    )


class LocalFrontendArtifactBuilder:
    def __init__(
        self,
        *,
        timeout_seconds: int = 900,
        node_binary: str | Path = "/usr/bin/node",
        npm_binary: str | Path = "/usr/bin/npm",
        cache_root: str | Path = "/var/cache/remihub-agent/npm",
        deployment_control: str | Path = "/usr/local/libexec/remihub-backend-deployment-control",
        sudo_binary: str | Path = "/usr/bin/sudo",
        environment: str | None = None,
    ):
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")
        self.timeout_seconds = timeout_seconds
        self.node_binary = Path(node_binary)
        self.npm_binary = Path(npm_binary)
        self.cache_root = Path(cache_root)
        self.deployment_control = Path(deployment_control)
        self.sudo_binary = Path(sudo_binary)
        self.environment = environment or os.environ.get(
            "REMIHUB_AGENT_ENVIRONMENT", ""
        )
        if self.environment not in {"qa", "production"}:
            raise ValueError("environment must be qa or production")

    def build(
        self,
        *,
        candidate_worktree: Path,
        artifact_root: Path,
        card_id: str,
        card_revision: int,
        deployment_run_id: str,
        approval_id: str,
        implementation_run_id: str,
        candidate_commit: str,
        changed_files: tuple[str, ...],
    ) -> FrontendArtifactEvidence:
        if not frontend_web_changed(changed_files):
            return FrontendArtifactEvidence(changed=False)
        frontend_root = candidate_worktree / "frontend-web"
        package_json = frontend_root / "package.json"
        lockfile = frontend_root / "package-lock.json"
        if package_json.is_symlink() or not package_json.is_file():
            raise DeploymentValidationError("Frontend package.json is missing")
        if lockfile.is_symlink() or not lockfile.is_file():
            raise DeploymentValidationError("Frontend package-lock.json is missing")
        lockfile_sha256 = _sha256_file(lockfile)
        try:
            node_version = self._run_version([str(self.node_binary), "--version"])
            npm_version = self._run_version([str(self.npm_binary), "--version"])
        except DeploymentValidationError:
            raise
        if node_version != FRONTEND_WEB_POLICY["node_version"]:
            raise DeploymentValidationError("Frontend Node version does not match policy")
        if npm_version != FRONTEND_WEB_POLICY["npm_version"]:
            raise DeploymentValidationError("Frontend npm version does not match policy")

        artifact_directory = artifact_root / card_id / deployment_run_id / "frontend-web" / candidate_commit
        artifact_directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        if artifact_directory.is_symlink():
            raise DeploymentValidationError("Frontend artifact directory is unsafe")
        first = self._build_once(
            candidate_worktree=candidate_worktree,
            frontend_root=frontend_root,
            lockfile_sha256=lockfile_sha256,
            candidate_commit=candidate_commit,
            artifact_directory=artifact_directory,
            label="build-1",
        )
        second = self._build_once(
            candidate_worktree=candidate_worktree,
            frontend_root=frontend_root,
            lockfile_sha256=lockfile_sha256,
            candidate_commit=candidate_commit,
            artifact_directory=artifact_directory,
            label="build-2",
        )
        if first["artifact_identity"] != second["artifact_identity"]:
            raise DeploymentValidationError("Frontend build artifact identity is not reproducible")
        return FrontendArtifactEvidence(
            changed=True,
            artifact_directory=str(artifact_directory),
            archive_path=second["archive_path"],
            archive_sha256=second["archive_sha256"],
            manifest_path=second["manifest_path"],
            manifest_sha256=second["manifest_sha256"],
            artifact_identity=second["artifact_identity"],
            lockfile_sha256=lockfile_sha256,
            node_version=node_version,
            npm_version=npm_version,
            commands=tuple(first["commands"] + second["commands"]),
            reproducibility={
                "first_identity": first["artifact_identity"],
                "second_identity": second["artifact_identity"],
                "matched": True,
                "archive_identity_source": "normalized manifest and content hashes",
            },
        )

    def _build_once(
        self,
        *,
        candidate_worktree: Path,
        frontend_root: Path,
        lockfile_sha256: str,
        candidate_commit: str,
        artifact_directory: Path,
        label: str,
    ) -> dict[str, Any]:
        staging = Path(tempfile.mkdtemp(prefix=f"remihub-frontend-{label}-", dir=str(artifact_directory)))
        try:
            source = staging / "source"
            shutil.copytree(
                frontend_root,
                source / "frontend-web",
                ignore=shutil.ignore_patterns("dist", "node_modules", ".env", ".env.*"),
                symlinks=True,
            )
            self._reject_unsafe_tree(
                source / "frontend-web",
                context="Frontend source snapshot",
            )
            cache_verification = self._verify_prepared_cache(lockfile_sha256)
            cache_source = self.cache_root / lockfile_sha256
            if cache_source.is_symlink() or not cache_source.is_dir():
                raise DeploymentValidationError(
                    "Prepared frontend npm cache is missing or unsafe"
                )
            shutil.copytree(
                cache_source,
                source / ".npm-cache",
                symlinks=True,
            )
            self._reject_unsafe_tree(
                source / ".npm-cache",
                context="Prepared frontend npm cache",
            )
            (source / ".npm-home").mkdir(mode=0o700)
            commands = [
                cache_verification,
                self._run_command(
                    [str(self.npm_binary), "ci", "--ignore-scripts"],
                    cwd=source / "frontend-web",
                    network="offline-prepared-cache",
                ),
                self._run_command(
                    [str(self.npm_binary), "run", "lint"],
                    cwd=source / "frontend-web",
                    network="denied-by-validation-sandbox",
                ),
                self._run_command(
                    [str(self.npm_binary), "run", "build"],
                    cwd=source / "frontend-web",
                    network="denied-by-validation-sandbox",
                ),
            ]
            dist_root = source / "frontend-web" / "dist"
            manifest = _frontend_artifact_manifest(
                dist_root,
                candidate_commit=candidate_commit,
                lockfile_sha256=lockfile_sha256,
            )
            manifest_path = artifact_directory / f"{label}.manifest.json"
            manifest_path.write_bytes(_canonical_json_bytes(manifest))
            os.chmod(manifest_path, 0o640)
            archive_path = artifact_directory / f"{label}.dist.tar"
            _write_deterministic_frontend_archive(dist_root, archive_path, manifest)
            verified = verify_frontend_archive(
                archive_path=archive_path,
                manifest_path=manifest_path,
            )
            final_manifest = artifact_directory / "manifest.json"
            final_archive = artifact_directory / "dist.tar"
            if label == "build-2":
                manifest_path.replace(final_manifest)
                archive_path.replace(final_archive)
                manifest_path = final_manifest
                archive_path = final_archive
                verified = verify_frontend_archive(
                    archive_path=archive_path,
                    manifest_path=manifest_path,
                )
            return {
                "artifact_identity": verified["artifact_identity"],
                "archive_sha256": verified["archive_sha256"],
                "manifest_sha256": verified["manifest_sha256"],
                "archive_path": str(archive_path),
                "manifest_path": str(manifest_path),
                "commands": commands,
            }
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _run_version(self, command: list[str]) -> str:
        evidence = self._run_command(
            command,
            cwd=Path(tempfile.gettempdir()),
            network="not_required",
        )
        return str(evidence["stdout_tail"]).strip()

    @staticmethod
    def _reject_unsafe_tree(path: Path, *, context: str) -> None:
        for directory, names, files in os.walk(
            path, topdown=True, followlinks=False
        ):
            current = Path(directory)
            try:
                current_mode = current.lstat().st_mode
            except OSError as exc:
                raise DeploymentValidationError(
                    f"{context} cannot be inspected"
                ) from exc
            if current.is_symlink() or not stat.S_ISDIR(current_mode):
                raise DeploymentValidationError(
                    f"{context} contains a symbolic link or special file"
                )
            for name in names + files:
                child = current / name
                try:
                    metadata = child.lstat()
                except OSError as exc:
                    raise DeploymentValidationError(
                        f"{context} cannot be inspected"
                    ) from exc
                if stat.S_ISDIR(metadata.st_mode):
                    continue
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                ):
                    raise DeploymentValidationError(
                        f"{context} contains a symbolic link or special file"
                    )

    def _verify_prepared_cache(self, lockfile_sha256: str) -> dict[str, Any]:
        command = [
            str(self.sudo_binary),
            "-n",
            str(self.deployment_control),
            "frontend-cache-verify",
            self.environment,
            lockfile_sha256,
        ]
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env={
                    "HOME": "/nonexistent",
                    "LANG": os.environ.get("LANG", "C.UTF-8"),
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                },
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentValidationError(
                "Prepared frontend npm cache could not be verified"
            ) from exc
        duration_ms = round((time.monotonic() - started) * 1000)
        if result.returncode != 0:
            detail = _tail(result.stderr or result.stdout, 3000)
            raise DeploymentValidationError(
                "Prepared frontend npm cache verification failed"
                + (f": {detail}" if detail else "")
            )
        return {
            "command": " ".join(command),
            "duration_ms": duration_ms,
            "return_code": result.returncode,
            "stdout_sha256": hashlib.sha256(
                result.stdout.encode("utf-8")
            ).hexdigest(),
            "stdout_tail": _tail(result.stdout, 3000),
            "stderr_tail": _tail(result.stderr, 3000),
            "network": "not_required",
        }

    @staticmethod
    def _frontend_command_environment(cwd: Path) -> dict[str, str]:
        environment = {
            "HOME": "/nonexistent",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": "/usr/bin:/bin",
            "NPM_CONFIG_IGNORE_SCRIPTS": "true",
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_FUND": "false",
            "SOURCE_DATE_EPOCH": "0",
        }
        prepared_cache = cwd.parent / ".npm-cache"
        prepared_home = cwd.parent / ".npm-home"
        if prepared_cache.is_dir():
            prepared_home.mkdir(mode=0o700, exist_ok=True)
            environment.update(
                {
                    "HOME": str(prepared_home),
                    "NPM_CONFIG_CACHE": str(prepared_cache),
                    "NPM_CONFIG_OFFLINE": "true",
                    "NPM_CONFIG_UPDATE_NOTIFIER": "false",
                    "NPM_CONFIG_LOGS_MAX": "0",
                    "NPM_CONFIG_USERCONFIG": "/dev/null",
                    "NPM_CONFIG_GLOBALCONFIG": "/nonexistent/remihub-global-npmrc",
                }
            )
        return environment

    def _run_command(self, command: list[str], *, cwd: Path, network: str) -> dict[str, Any]:
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env=self._frontend_command_environment(cwd),
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentValidationError(f"Frontend command failed: {' '.join(command)}") from exc
        evidence = {
            "command": " ".join(command),
            "return_code": result.returncode,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "stdout_sha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
            "stdout_tail": _tail(result.stdout, 4000),
            "stderr_tail": _tail(result.stderr, 4000),
            "network": network,
        }
        if result.returncode != 0:
            detail = evidence["stderr_tail"] or evidence["stdout_tail"]
            raise DeploymentValidationError(
                f"Frontend command failed: {' '.join(command)}: {detail}"
            )
        return evidence


class PostgresDeploymentDatabase:
    def __init__(
        self,
        *,
        config_path: str | Path,
        backup_root: str | Path,
        owner_role: str,
        pg_dump_binary: str = "/usr/bin/pg_dump",
        pg_restore_binary: str = "/usr/bin/pg_restore",
        command_timeout_seconds: int = 900,
    ):
        self.config_path = _required_absolute_file(
            config_path,
            field="REMIHUB_AGENT_DEPLOYMENT_DATABASE_CONFIG",
        )
        self.backup_root = _required_absolute_directory(
            backup_root,
            field="REMIHUB_AGENT_DEPLOYMENT_BACKUP_ROOT",
        )
        normalized_role = owner_role.strip()
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", normalized_role):
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_DEPLOYMENT_DATABASE_OWNER_ROLE is invalid"
            )
        self.owner_role = normalized_role
        self.pg_dump_binary = _required_executable(
            pg_dump_binary,
            field="pg_dump",
        )
        self.pg_restore_binary = _required_executable(
            pg_restore_binary,
            field="pg_restore",
        )
        if command_timeout_seconds < 1:
            raise ValueError("command_timeout_seconds must be at least 1")
        self.command_timeout_seconds = command_timeout_seconds

    def migration_history(self) -> tuple[dict[str, str], ...]:
        conn = self._connect()
        try:
            migration_runner._ensure_migration_table(conn)
            migration_runner._acquire_lock(conn)
            return _migration_history_from_applied(
                migration_runner._applied_migrations(conn)
            )
        finally:
            try:
                migration_runner._release_lock(conn)
            finally:
                conn.close()

    def database_identity(self) -> tuple[str | None, ...]:
        conn = self._connect()
        try:
            return _database_identity(conn)
        finally:
            conn.close()

    def pending_versions(self, migrations_dir: Path) -> tuple[str, ...]:
        migrations = migration_runner.discover_migrations(migrations_dir)
        conn = self._connect()
        try:
            migration_runner._ensure_migration_table(conn)
            migration_runner._acquire_lock(conn)
            applied = migration_runner._applied_migrations(conn)
            migration_runner._validate_applied_checksums(migrations, applied)
            return tuple(
                migration.version
                for migration in migrations
                if migration.version not in applied
            )
        finally:
            try:
                migration_runner._release_lock(conn)
            finally:
                conn.close()

    def backup(self, *, card_id: str, deployment_run_id: str) -> BackupEvidence:
        card_root = self.backup_root / card_id
        card_root.mkdir(mode=0o750, exist_ok=True)
        if card_root.is_symlink():
            raise AgentDeploymentError("Database backup directory must not be a symlink")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = card_root / f"{deployment_run_id}-{timestamp}.dump"
        if backup_path.exists() or backup_path.is_symlink():
            raise AgentDeploymentError("Database backup path already exists")

        database = load_config(self.config_path)["Database"]
        environment = {
            "HOME": "/nonexistent",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": "/usr/bin:/bin",
            "PGPASSWORD": database["password"],
        }
        command = [
            str(self.pg_dump_binary),
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--host",
            database["host"],
            "--port",
            str(database["port"]),
            "--username",
            database["user"],
            "--role",
            self.owner_role,
            "--file",
            str(backup_path),
            database["database"],
        ]
        self._run(command, environment=environment, context="PostgreSQL backup failed")
        os.chmod(backup_path, 0o600)
        self._run(
            [str(self.pg_restore_binary), "--list", str(backup_path)],
            environment={
                "HOME": "/nonexistent",
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "PATH": "/usr/bin:/bin",
            },
            context="PostgreSQL backup verification failed",
        )
        return BackupEvidence(
            path=str(backup_path),
            size_bytes=backup_path.stat().st_size,
            sha256=_sha256_file(backup_path),
        )

    def upgrade(
        self,
        migrations_dir: Path,
        expected_versions: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not expected_versions:
            return ()
        migrations = migration_runner.discover_migrations(migrations_dir)
        conn = self._connect()
        try:
            migration_runner._ensure_migration_table(conn)
            migration_runner._acquire_lock(conn)
            before = migration_runner._applied_migrations(conn)
            migration_runner._validate_applied_checksums(migrations, before)
            pending = tuple(
                migration.version
                for migration in migrations
                if migration.version not in before
            )
            if pending != expected_versions:
                raise AgentDeploymentError(
                    "Database pending migrations do not match the approved candidate: "
                    f"expected {expected_versions!r}, found {pending!r}"
                )
            migration_runner.upgrade(conn, migrations)
            after = migration_runner._applied_migrations(conn)
            applied = tuple(version for version in after if version not in before)
            if applied != expected_versions:
                raise AgentDeploymentError(
                    "Applied migrations do not match the approved candidate"
                )
            return applied
        finally:
            try:
                migration_runner._release_lock(conn)
            finally:
                conn.close()

    def downgrade(
        self,
        migrations_dir: Path,
        versions: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not versions:
            return ()
        migrations = migration_runner.discover_migrations(migrations_dir)
        conn = self._connect()
        try:
            migration_runner._ensure_migration_table(conn)
            migration_runner._acquire_lock(conn)
            applied = migration_runner._applied_migrations(conn)
            migration_runner._validate_applied_checksums(migrations, applied)
            current_tail = tuple(sorted(applied, reverse=True)[: len(versions)])
            expected_tail = tuple(reversed(versions))
            if current_tail != expected_tail:
                raise DeploymentRollbackError(
                    "Database migration tail changed before rollback: "
                    f"expected {expected_tail!r}, found {current_tail!r}"
                )
            migration_runner.downgrade(conn, migrations, len(versions))
            remaining = migration_runner._applied_migrations(conn)
            still_applied = tuple(version for version in versions if version in remaining)
            if still_applied:
                raise DeploymentRollbackError(
                    f"Migration rollback did not remove {still_applied!r}"
                )
            return expected_tail
        finally:
            try:
                migration_runner._release_lock(conn)
            finally:
                conn.close()

    def _connect(self):
        conn = migration_runner._connect(self.config_path)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET ROLE {self.owner_role}")
            return conn
        except Exception:
            conn.close()
            raise

    def _run(
        self,
        command: list[str],
        *,
        environment: dict[str, str],
        context: str,
    ) -> None:
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
            raise AgentDeploymentError(context) from exc
        if result.returncode != 0:
            detail = _tail(result.stderr, 2000)
            suffix = f": {detail}" if detail else ""
            raise AgentDeploymentError(f"{context}{suffix}")


class PostgresMigrationHistoryReader:
    """Read-only schema_migrations access for cross-environment parity checks."""

    def __init__(
        self,
        *,
        config_path: str | Path,
        role: str | None = None,
    ):
        self.config_path = _required_absolute_file(
            config_path,
            field="REMIHUB_AGENT_DEPLOYMENT_QA_PARITY_DATABASE_CONFIG",
        )
        normalized_role = role.strip() if role is not None else None
        if normalized_role and not re.fullmatch(
            r"[a-z_][a-z0-9_]{0,62}",
            normalized_role,
        ):
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_DEPLOYMENT_QA_PARITY_DATABASE_ROLE is invalid"
            )
        self.role = normalized_role

    def migration_history(self) -> tuple[dict[str, str], ...]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
            migration_runner._acquire_lock(conn)
            return _migration_history_from_applied(
                migration_runner._applied_migrations(conn)
            )
        finally:
            try:
                migration_runner._release_lock(conn)
            finally:
                conn.rollback()
                conn.close()

    def database_identity(self) -> tuple[str | None, ...]:
        conn = self._connect()
        try:
            return _database_identity(conn)
        finally:
            conn.rollback()
            conn.close()

    def _connect(self):
        conn = _connect_explicit_database_config(self.config_path)
        try:
            if self.role is not None:
                with conn.cursor() as cur:
                    cur.execute(f"SET ROLE {self.role}")
            return conn
        except Exception:
            conn.close()
            raise


class PrivilegedDeploymentRuntime:
    def __init__(
        self,
        *,
        environment: str,
        helper_path: str | Path,
        health_url: str,
        sudo_binary: str | Path = "/usr/bin/sudo",
        command_timeout_seconds: int = 180,
        health_attempts: int = 10,
        health_delay_seconds: float = 1.0,
    ):
        normalized_environment = environment.strip().lower()
        if normalized_environment not in {"qa", "production"}:
            raise AgentWorkerConfigurationError("environment must be qa or production")
        self.environment = normalized_environment
        self.helper_path = _required_executable(
            helper_path,
            field="REMIHUB_AGENT_DEPLOYMENT_RUNTIME_HELPER",
        )
        self.sudo_binary = _required_executable(sudo_binary, field="sudo")
        normalized_url = health_url.strip()
        if not re.fullmatch(r"http://127\.0\.0\.1:\d+/openapi\.json", normalized_url):
            raise AgentWorkerConfigurationError(
                "Deployment health URL must be a loopback /openapi.json URL"
            )
        self.health_url = normalized_url
        if command_timeout_seconds < 1:
            raise ValueError("command_timeout_seconds must be at least 1")
        if health_attempts < 1:
            raise ValueError("health_attempts must be at least 1")
        self.command_timeout_seconds = command_timeout_seconds
        self.health_attempts = health_attempts
        self.health_delay_seconds = health_delay_seconds

    def stop(self) -> None:
        self._helper("stop")

    def start(self) -> None:
        self._helper("start")

    def promote(
        self,
        *,
        candidate_branch: str,
        candidate_commit: str,
        expected_before: str,
        rollback_ref: str,
    ) -> None:
        self._helper(
            "promote",
            candidate_branch,
            candidate_commit,
            expected_before,
            rollback_ref,
        )

    def restore(self, *, expected_current: str, rollback_commit: str) -> None:
        self._helper("restore", expected_current, rollback_commit)

    def frontend_install(
        self,
        *,
        artifact_manifest: str,
        artifact_archive: str,
        artifact_identity: str,
        candidate_commit: str,
        card_id: str,
        deployment_run_id: str,
    ) -> dict[str, Any]:
        payload = self._helper(
            "frontend-install",
            artifact_manifest,
            artifact_archive,
            artifact_identity,
            candidate_commit,
            card_id,
            deployment_run_id,
        )
        return _json_helper_payload(payload, context="frontend install")

    def frontend_restore(self) -> dict[str, Any]:
        return _json_helper_payload(
            self._helper("frontend-restore"),
            context="frontend restore",
        )

    def frontend_verify(
        self,
        *,
        artifact_manifest: str,
        artifact_identity: str,
    ) -> dict[str, Any]:
        return _json_helper_payload(
            self._helper("frontend-verify", artifact_manifest, artifact_identity),
            context="frontend verify",
        )

    def synchronize_sources(
        self,
        *,
        candidate_branch: str,
        candidate_commit: str,
        expected_before: str,
        rollback_ref: str,
    ) -> None:
        if self.environment != "production":
            return
        self._helper(
            "synchronize-sources",
            candidate_branch,
            candidate_commit,
            expected_before,
            rollback_ref,
        )

    def restore_sources(self, *, expected_current: str, rollback_commit: str) -> None:
        if self.environment != "production":
            return
        self._helper("restore-sources", expected_current, rollback_commit)

    def verify(self) -> RuntimeHealth:
        status_output = self._helper("status").strip()
        if status_output != "active":
            raise AgentDeploymentError(
                f"Deployment service is not active: {status_output or 'unknown'}"
            )
        last_error: Exception | None = None
        for attempt in range(self.health_attempts):
            try:
                request = urllib.request.Request(
                    self.health_url,
                    headers={"Accept": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = response.read(5_000_000)
                    status_code = int(response.status)
                payload = json.loads(body.decode("utf-8"))
                if status_code != 200 or not isinstance(payload, dict):
                    raise AgentDeploymentError(
                        "Deployment /openapi.json response is invalid"
                    )
                if "openapi" not in payload or "paths" not in payload:
                    raise AgentDeploymentError(
                        "Deployment /openapi.json is missing OpenAPI fields"
                    )
                return RuntimeHealth(
                    service_active=True,
                    url=self.health_url,
                    status_code=status_code,
                    response_sha256=hashlib.sha256(body).hexdigest(),
                )
            except (
                AgentDeploymentError,
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                urllib.error.URLError,
            ) as exc:
                last_error = exc
                if attempt + 1 < self.health_attempts:
                    time.sleep(self.health_delay_seconds)
        raise AgentDeploymentError(
            f"Deployment health verification failed: {last_error}"
        )

    def _helper(self, action: str, *arguments: str) -> str:
        command = [
            str(self.sudo_binary),
            "-n",
            str(self.helper_path),
            action,
            self.environment,
            *arguments,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env={
                    "HOME": "/nonexistent",
                    "LANG": os.environ.get("LANG", "C.UTF-8"),
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                },
                timeout=self.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentDeploymentError(
                f"Deployment runtime helper failed during {action}"
            ) from exc
        if result.returncode != 0:
            detail = _tail(result.stderr, 3000) or _tail(result.stdout, 3000)
            suffix = f": {detail}" if detail else ""
            raise AgentDeploymentError(
                f"Deployment runtime helper failed during {action}{suffix}"
            )
        return result.stdout


def _json_helper_payload(payload: str, *, context: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AgentDeploymentError(
            f"Deployment runtime helper returned invalid JSON during {context}"
        ) from exc
    if not isinstance(parsed, dict):
        raise AgentDeploymentError(
            f"Deployment runtime helper returned a non-object result during {context}"
        )
    return parsed


class PrivilegedBackendGitHubSynchronizer:
    """Invoke the installed, narrowly authorized backend GitHub sync helper."""

    def __init__(
        self,
        *,
        helper_path: str | Path,
        command_timeout_seconds: int = 300,
    ):
        if command_timeout_seconds < 1:
            raise ValueError("command_timeout_seconds must be at least 1")
        self.helper_path = _required_executable(
            helper_path,
            field="REMIHUB_AGENT_DEPLOYMENT_GITHUB_SYNC_HELPER",
        )
        self.command_timeout_seconds = command_timeout_seconds

    def synchronize(
        self,
        *,
        candidate_commit: str,
        base_commit: str,
        card_id: str,
        deployment_run_id: str,
    ) -> dict[str, Any]:
        command = [
            "sudo",
            "-n",
            str(self.helper_path),
            "synchronize",
            "production",
            candidate_commit,
            base_commit,
            card_id,
            deployment_run_id,
        ]
        environment = {
            "HOME": "/nonexistent",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
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
            raise AgentDeploymentError(
                "Backend GitHub synchronization helper could not be executed"
            ) from exc
        if result.returncode != 0:
            detail = _tail(result.stderr, 3000) or _tail(result.stdout, 3000)
            suffix = f": {detail}" if detail else ""
            raise AgentDeploymentError(
                f"Backend GitHub synchronization helper failed{suffix}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AgentDeploymentError(
                "Backend GitHub synchronization helper returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise AgentDeploymentError(
                "Backend GitHub synchronization helper returned a non-object result"
            )
        if (
            payload.get("status") != "verified"
            or payload.get("candidate_commit") != candidate_commit
            or payload.get("remote_after") != candidate_commit
        ):
            raise AgentDeploymentError(
                "Backend GitHub synchronization helper did not verify the candidate"
            )
        return payload


class GitBackendDeploymentManager:
    """Validate, test, migrate, promote, verify, and roll back one backend candidate."""

    MAX_PATCH_BYTES = 5_000_000
    MAX_CHANGED_FILES = 100
    PROTECTED_BACKEND_PATHS = frozenset(
        {
            "backend/agent_worker.py",
            "backend/config.py",
            "backend/core/agent_deployment.py",
            "backend/core/agent_worker.py",
            "backend/core/agent_workspace.py",
            "backend/core/codex_implementation.py",
            "backend/core/codex_planning.py",
            "backend/core/auth.py",
            "backend/core/firebase_auth.py",
            "backend/database/database.py",
            "backend/database/migration_runner.py",
            "backend/routers/auth.py",
            "backend/services/agent_worker_service.py",
            "backend/services/auth_service.py",
        }
    )
    FRONTEND_FIXED_FILES = frozenset(
        {
            "frontend-web/package.json",
            "frontend-web/package-lock.json",
            "frontend-web/index.html",
            "frontend-web/vite.config.ts",
            "frontend-web/tsconfig.json",
            "frontend-web/tsconfig.app.json",
            "frontend-web/tsconfig.node.json",
            "frontend-web/eslint.config.js",
            "frontend-web/postcss.config.cjs",
            "frontend-web/tailwind.config.js",
            "frontend-web/README.md",
            "frontend-web/.gitignore",
        }
    )
    FRONTEND_SOURCE_SUFFIXES = frozenset(
        {
            ".css",
            ".gif",
            ".html",
            ".ico",
            ".jpg",
            ".jpeg",
            ".json",
            ".md",
            ".png",
            ".svg",
            ".ts",
            ".tsx",
            ".txt",
            ".webp",
        }
    )
    FRONTEND_SECRET_NAME_RE = re.compile(
        r"(^|[-_.])(secret|token|credential|credentials|private[-_.]?key|id_rsa|id_ed25519)([-_.]|$)",
        re.IGNORECASE,
    )

    def __init__(
        self,
        *,
        environment: str,
        source_repository: str | Path,
        source_worktree_root: str | Path,
        source_artifact_root: str | Path,
        target_repository: str | Path,
        candidate_worktree_root: str | Path,
        deployment_artifact_root: str | Path,
        target_branch: str,
        validator: BackendValidator,
        database: DeploymentDatabase,
        runtime: DeploymentRuntime,
        frontend_builder: FrontendArtifactBuilder | None = None,
        qa_history_reader: MigrationHistoryReader | None = None,
        qa_candidate_repository: str | Path | None = None,
        git_binary: str = "git",
        command_timeout_seconds: int = 120,
    ):
        normalized_environment = environment.strip().lower()
        if normalized_environment not in {"qa", "production"}:
            raise AgentWorkerConfigurationError("environment must be qa or production")
        self.environment = normalized_environment
        self.git_binary = git_binary.strip()
        if not self.git_binary:
            raise AgentWorkerConfigurationError("git_binary must not be blank")
        if command_timeout_seconds < 1:
            raise ValueError("command_timeout_seconds must be at least 1")
        self.command_timeout_seconds = command_timeout_seconds
        self.validator = validator
        self.database = database
        self.runtime = runtime
        self.frontend_builder = frontend_builder or LocalFrontendArtifactBuilder(
            timeout_seconds=command_timeout_seconds,
            environment=normalized_environment,
        )
        self.qa_history_reader = qa_history_reader
        self.qa_candidate_repository = None

        self.source_repository = _required_absolute_directory(
            source_repository,
            field="REMIHUB_AGENT_REPOSITORY",
        )
        self.source_worktree_root = _required_absolute_directory(
            source_worktree_root,
            field="REMIHUB_AGENT_WORKTREE_ROOT",
        )
        self.source_artifact_root = _required_absolute_directory(
            source_artifact_root,
            field="REMIHUB_AGENT_ARTIFACT_ROOT",
        )
        self.target_repository = _required_absolute_directory(
            target_repository,
            field="REMIHUB_AGENT_DEPLOYMENT_TARGET_REPOSITORY",
        )
        self.candidate_worktree_root = _required_absolute_directory(
            candidate_worktree_root,
            field="REMIHUB_AGENT_DEPLOYMENT_WORKTREE_ROOT",
        )
        self.deployment_artifact_root = _required_absolute_directory(
            deployment_artifact_root,
            field="REMIHUB_AGENT_DEPLOYMENT_ARTIFACT_ROOT",
        )
        self.target_branch = self._validate_branch_name(
            self.target_repository,
            target_branch,
            field="deployment target branch",
        )
        expected_target_branch = (
            "qa-main" if self.environment == "qa" else "production-main"
        )
        if self.target_branch != expected_target_branch:
            raise AgentWorkerConfigurationError(
                f"{self.environment} deployment target branch must be "
                f"{expected_target_branch}"
            )
        if self.environment == "production" and self.qa_history_reader is None:
            raise AgentWorkerConfigurationError(
                "Production backend deployment requires QA migration parity reader"
            )
        if self.environment == "production":
            if qa_candidate_repository is None:
                raise AgentWorkerConfigurationError(
                    "Production backend deployment requires QA candidate repository"
                )
            self.qa_candidate_repository = _required_absolute_directory(
                qa_candidate_repository,
                field="REMIHUB_AGENT_DEPLOYMENT_QA_CANDIDATE_REPOSITORY",
            )
        elif qa_candidate_repository is not None:
            raise AgentWorkerConfigurationError(
                "QA candidate repository verification is restricted to production"
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
            error_context="The deployment target is not a Git repository",
        )
        self.source_common_directory = self._common_git_directory(
            self.source_repository
        )
        self.target_common_directory = self._common_git_directory(
            self.target_repository
        )
        if self.source_common_directory == self.target_common_directory:
            raise AgentWorkerConfigurationError(
                "Deployment target must be separate from implementation source"
            )
        if self.qa_candidate_repository is not None:
            self._run_git(
                self.qa_candidate_repository,
                "rev-parse",
                "--git-dir",
                error_context="The QA candidate repository is not a Git repository",
            )
            qa_candidate_common_directory = self._common_git_directory(
                self.qa_candidate_repository
            )
            if qa_candidate_common_directory == self.target_common_directory:
                raise AgentWorkerConfigurationError(
                    "Production deployment target must be separate from QA candidate repository"
                )
        if self._run_git(
            self.target_repository,
            "rev-parse",
            "--is-bare-repository",
            error_context="Unable to inspect the deployment target",
        ).stdout.strip() != "true":
            raise AgentWorkerConfigurationError(
                "Deployment target must be a bare Git repository"
            )
        if self._run_git(
            self.target_repository,
            "remote",
            error_context="Unable to inspect deployment target remotes",
        ).stdout.strip():
            raise AgentWorkerConfigurationError(
                "Deployment target must not have Git remotes"
            )
        self._resolve_commit(self.target_repository, self.target_branch)

        self.lock_root = self.candidate_worktree_root / ".locks"
        self.lock_root.mkdir(mode=0o750, exist_ok=True)
        if self.lock_root.is_symlink():
            raise AgentWorkerConfigurationError(
                "Deployment worktree lock directory must not be a symlink"
            )

    def deploy(self, claim: ClaimedRun) -> DeploymentCandidate:
        if claim.phase is not RunPhase.DEPLOYMENT:
            raise AgentDeploymentError(
                "The backend deployment manager accepts only deployment runs"
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
                candidate_branch, candidate_commit, candidate_path = (
                    self._materialize_candidate(claim, approved)
                )
                self._verify_qa_candidate(claim, candidate_commit)
                migration_plan = self._migration_plan(
                    approved.base_commit,
                    candidate_commit,
                    candidate_path,
                )
                frontend_changed = frontend_web_changed(approved.changed_files)
                rollback_ref = self._rollback_ref(claim)
                manifest_path = self._manifest_path(claim)
                manifest = self._load_or_initialize_manifest(
                    claim,
                    approved,
                    candidate_branch=candidate_branch,
                    candidate_commit=candidate_commit,
                    rollback_ref=rollback_ref,
                    migration_plan=migration_plan,
                    frontend_changed=frontend_changed,
                )

                prior_success = self._successful_attempt(manifest)
                current_target = self._resolve_commit(
                    self.target_repository,
                    self.target_branch,
                )
                if prior_success is not None and current_target == candidate_commit:
                    health = self.runtime.verify()
                    frontend_artifact = _frontend_artifact_from_manifest(manifest)
                    if frontend_artifact is not None:
                        self.runtime.frontend_verify(
                            artifact_manifest=frontend_artifact.manifest_path,
                            artifact_identity=frontend_artifact.artifact_identity,
                        )
                    return self._candidate_from_success(
                        approved,
                        candidate_branch=candidate_branch,
                        candidate_commit=candidate_commit,
                        rollback_ref=rollback_ref,
                        manifest_path=manifest_path,
                        attempt=prior_success,
                        health=health,
                        frontend_artifact=frontend_artifact,
                    )

                if current_target != approved.base_commit:
                    raise AgentDeploymentError(
                        "Deployment target advanced before candidate execution"
                    )

                attempt = self._begin_attempt(manifest, claim)
                self._write_manifest(manifest_path, manifest)
                return self._execute_candidate(
                    claim,
                    approved,
                    candidate_branch=candidate_branch,
                    candidate_commit=candidate_commit,
                    candidate_path=candidate_path,
                    migration_plan=migration_plan,
                    rollback_ref=rollback_ref,
                    manifest_path=manifest_path,
                    manifest=manifest,
                    attempt=attempt,
                )
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def record_github_sync(
        self,
        claim: ClaimedRun,
        candidate: DeploymentCandidate,
        evidence: dict[str, Any],
    ) -> None:
        manifest_path = Path(candidate.manifest_path).resolve()
        if not manifest_path.is_relative_to(self.deployment_artifact_root):
            raise AgentDeploymentError(
                "GitHub synchronization manifest is outside the deployment artifact root"
            )
        lock_path = self.lock_root / f"{claim.card_id}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            os.chmod(lock_path, 0o640)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AgentDeploymentError(
                        "Deployment manifest is unavailable for GitHub synchronization evidence"
                    ) from exc
                if (
                    not isinstance(manifest, dict)
                    or manifest.get("card_id") != claim.card_id
                    or manifest.get("deployment_run_id") != claim.id
                    or manifest.get("candidate_commit") != candidate.candidate_commit
                ):
                    raise AgentDeploymentError(
                        "Deployment manifest does not match GitHub synchronization evidence"
                    )
                attempt = self._successful_attempt(manifest)
                if attempt is None:
                    raise AgentDeploymentError(
                        "GitHub synchronization requires a successful local deployment attempt"
                    )
                normalized = dict(evidence)
                status = normalized.get("status")
                if status not in {"pending", "verified"}:
                    raise AgentDeploymentError(
                        "GitHub synchronization evidence has an invalid status"
                    )
                normalized["recorded_at"] = _utc_now()
                attempt["github_sync"] = normalized
                attempt["stage"] = (
                    "github_synchronized"
                    if status == "verified"
                    else "github_sync_pending"
                )
                manifest["github_sync"] = normalized
                self._write_manifest(manifest_path, manifest)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _qa_candidate_commit_from_pipeline(self, claim: ClaimedRun) -> str:
        pipeline = claim.result_metadata.get("deployment_pipeline")
        if (
            not isinstance(pipeline, dict)
            or pipeline.get("stage") != "qa_succeeded"
        ):
            raise AgentDeploymentError(
                "Production deployment requires QA-succeeded pipeline metadata"
            )
        candidate_commit = pipeline.get("candidate_commit")
        if not isinstance(candidate_commit, str) or not re.fullmatch(
            r"[0-9a-f]{40}",
            candidate_commit,
        ):
            raise AgentDeploymentError(
                "Production deployment requires QA candidate commit metadata"
            )
        return candidate_commit

    def _verify_qa_candidate(
        self,
        claim: ClaimedRun,
        candidate_commit: str,
    ) -> None:
        if self.environment != "production":
            return
        assert self.qa_candidate_repository is not None
        if self._qa_candidate_commit_from_pipeline(claim) != candidate_commit:
            raise AgentDeploymentError(
                "Production candidate does not match the QA-validated candidate"
            )
        qa_head = self._resolve_commit(
            self.qa_candidate_repository,
            "qa-main",
        )
        if qa_head != candidate_commit:
            raise AgentDeploymentError(
                "Production candidate does not match the QA-validated candidate"
            )


    def _execute_candidate(
        self,
        claim: ClaimedRun,
        approved: ApprovedImplementation,
        *,
        candidate_branch: str,
        candidate_commit: str,
        candidate_path: Path,
        migration_plan: MigrationPlan,
        rollback_ref: str,
        manifest_path: Path,
        manifest: dict,
        attempt: dict,
    ) -> DeploymentCandidate:
        validation: ValidationEvidence | None = None
        frontend_artifact = FrontendArtifactEvidence(
            changed=bool(manifest.get("frontend_changed"))
        )
        backup: BackupEvidence | None = None
        migrations_applied: tuple[str, ...] = ()
        runtime_promotion_attempted = False
        runtime_promoted = False
        target_update_attempted = False
        target_updated = False
        source_sync_attempted = False
        sources_synchronized = False
        service_stopped = False
        frontend_install_attempted = False
        frontend_installed = False
        migrations_dir = candidate_path / "backend" / "database" / "migrations"

        try:
            validation = self.validator.validate(candidate_path)
            attempt["validation"] = asdict(validation)
            attempt["stage"] = "validated"
            self._write_manifest(manifest_path, manifest)
            frontend_artifact = self.frontend_builder.build(
                candidate_worktree=candidate_path,
                artifact_root=self.deployment_artifact_root,
                card_id=claim.card_id,
                card_revision=claim.card_revision,
                deployment_run_id=claim.id,
                approval_id=approved.approval_id,
                implementation_run_id=approved.implementation_run_id,
                candidate_commit=candidate_commit,
                changed_files=approved.changed_files,
            )
            manifest["frontend_artifact"] = asdict(frontend_artifact)
            attempt["frontend_artifact"] = asdict(frontend_artifact)
            attempt["stage"] = (
                "frontend_artifact_created"
                if frontend_artifact.changed
                else "frontend_not_changed"
            )
            self._write_manifest(manifest_path, manifest)
        except DeploymentValidationError as exc:
            attempt["status"] = "failed_validation"
            attempt["error"] = str(exc)[:10000]
            attempt["finished_at"] = _utc_now()
            self._write_manifest(manifest_path, manifest)
            raise

        if self.environment == "production":
            try:
                self._record_and_require_migration_history(
                    attempt,
                    manifest_path,
                    manifest,
                    source="qa",
                    stage="qa_history_before_production_mutation",
                    expected_history=migration_plan.expected_history,
                    reader=self._required_qa_history_reader(),
                )
            except Exception as exc:
                attempt["status"] = "failed_migration_parity"
                attempt["error"] = f"{type(exc).__name__}: {exc}"[:10000]
                attempt["finished_at"] = _utc_now()
                self._write_manifest(manifest_path, manifest)
                raise

        try:
            pending = self.database.pending_versions(migrations_dir)
            if pending != migration_plan.versions:
                raise AgentDeploymentError(
                    "Database pending migrations do not match the approved candidate: "
                    f"expected {migration_plan.versions!r}, found {pending!r}"
                )

            self._ensure_rollback_ref(rollback_ref, approved.base_commit)
            attempt["stage"] = "rollback_reference_created"
            self._write_manifest(manifest_path, manifest)

            if migration_plan.versions:
                backup = self.database.backup(
                    card_id=claim.card_id,
                    deployment_run_id=claim.id,
                )
                attempt["database_backup"] = asdict(backup)
                attempt["stage"] = "database_backed_up"
                self._write_manifest(manifest_path, manifest)

            self.runtime.stop()
            service_stopped = True
            attempt["stage"] = "service_stopped"
            self._write_manifest(manifest_path, manifest)

            if migration_plan.versions:
                try:
                    migrations_applied = self.database.upgrade(
                        migrations_dir,
                        migration_plan.versions,
                    )
                except Exception:
                    pending_after_failure = self.database.pending_versions(
                        migrations_dir
                    )
                    migrations_applied = tuple(
                        version
                        for version in migration_plan.versions
                        if version not in pending_after_failure
                    )
                    attempt["migrations_applied"] = list(migrations_applied)
                    attempt["stage"] = "migration_failed"
                    self._write_manifest(manifest_path, manifest)
                    raise
                attempt["migrations_applied"] = list(migrations_applied)
                attempt["stage"] = "migrations_applied"
                self._write_manifest(manifest_path, manifest)

            runtime_promotion_attempted = True
            self.runtime.promote(
                candidate_branch=candidate_branch,
                candidate_commit=candidate_commit,
                expected_before=approved.base_commit,
                rollback_ref=rollback_ref,
            )
            runtime_promoted = True
            attempt["stage"] = "runtime_promoted"
            self._write_manifest(manifest_path, manifest)

            if frontend_artifact.changed:
                frontend_install_attempted = True
                assert frontend_artifact.manifest_path is not None
                assert frontend_artifact.archive_path is not None
                assert frontend_artifact.artifact_identity is not None
                install_evidence = self.runtime.frontend_install(
                    artifact_manifest=frontend_artifact.manifest_path,
                    artifact_archive=frontend_artifact.archive_path,
                    artifact_identity=frontend_artifact.artifact_identity,
                    candidate_commit=candidate_commit,
                    card_id=claim.card_id,
                    deployment_run_id=claim.id,
                )
                frontend_installed = True
                attempt["frontend_install"] = install_evidence
                attempt["stage"] = "frontend_installed"
                self._write_manifest(manifest_path, manifest)

            self.runtime.start()
            service_stopped = False
            health = self.runtime.verify()
            attempt["health"] = asdict(health)
            attempt["stage"] = "health_verified"
            self._write_manifest(manifest_path, manifest)

            if frontend_artifact.changed:
                assert frontend_artifact.manifest_path is not None
                assert frontend_artifact.artifact_identity is not None
                attempt["frontend_verified"] = self.runtime.frontend_verify(
                    artifact_manifest=frontend_artifact.manifest_path,
                    artifact_identity=frontend_artifact.artifact_identity,
                )
                attempt["stage"] = "frontend_verified"
                self._write_manifest(manifest_path, manifest)

            if self.environment == "qa":
                self._record_and_require_migration_history(
                    attempt,
                    manifest_path,
                    manifest,
                    source="qa",
                    stage="qa_history_after_validation",
                    expected_history=migration_plan.expected_history,
                    reader=self.database,
                )
            else:
                self._record_and_require_migration_history(
                    attempt,
                    manifest_path,
                    manifest,
                    source="production",
                    stage="production_history_after_validation",
                    expected_history=migration_plan.expected_history,
                    reader=self.database,
                )

            target_update_attempted = True
            self._run_git(
                self.target_repository,
                "update-ref",
                f"refs/heads/{self.target_branch}",
                candidate_commit,
                approved.base_commit,
                error_context="Unable to advance the deployment target",
            )
            target_updated = True
            attempt["stage"] = "target_advanced"
            self._write_manifest(manifest_path, manifest)

            source_sync_attempted = self.environment == "production"
            self.runtime.synchronize_sources(
                candidate_branch=candidate_branch,
                candidate_commit=candidate_commit,
                expected_before=approved.base_commit,
                rollback_ref=rollback_ref,
            )
            sources_synchronized = self.environment == "production"
            attempt["stage"] = "sources_synchronized"
            attempt["status"] = "succeeded"
            attempt["finished_at"] = _utc_now()
            self._write_manifest(manifest_path, manifest)

            return DeploymentCandidate(
                approval_id=approved.approval_id,
                implementation_run_id=approved.implementation_run_id,
                environment=self.environment,
                candidate_branch=candidate_branch,
                candidate_commit=candidate_commit,
                target_branch=self.target_branch,
                target_before=approved.base_commit,
                target_after=candidate_commit,
                base_commit=approved.base_commit,
                changed_files=approved.changed_files,
                patch_sha256=approved.patch_sha256,
                patch_size_bytes=approved.patch_size_bytes,
                manifest_path=str(manifest_path),
                rollback_ref=rollback_ref,
                validation=asdict(validation),
                frontend_artifact=(
                    asdict(frontend_artifact) if frontend_artifact.changed else None
                ),
                service_restart_performed=True,
                migrations_applied=migrations_applied,
                database_backup=asdict(backup) if backup else None,
                rollback_performed=False,
            )
        except Exception as exc:
            rollback_errors: list[str] = []
            attempt["failure_stage"] = attempt.get("stage")
            attempt["error"] = f"{type(exc).__name__}: {exc}"[:10000]

            # Treat stop/start/promote/sync timeouts as ambiguous. Always force the
            # service down before touching code or database state, then reconcile
            # each durable boundary from its observed state instead of trusting a
            # local boolean that may not have been updated before the failure.
            try:
                self.runtime.stop()
                service_stopped = True
            except Exception as rollback_exc:
                rollback_errors.append(f"stop: {rollback_exc}")

            if source_sync_attempted:
                try:
                    self.runtime.restore_sources(
                        expected_current=candidate_commit,
                        rollback_commit=approved.base_commit,
                    )
                    sources_synchronized = False
                except Exception as rollback_exc:
                    rollback_errors.append(f"restore_sources: {rollback_exc}")

            if target_update_attempted or target_updated:
                try:
                    observed_target = self._resolve_commit(
                        self.target_repository,
                        self.target_branch,
                    )
                    if observed_target == candidate_commit:
                        self._run_git(
                            self.target_repository,
                            "update-ref",
                            f"refs/heads/{self.target_branch}",
                            approved.base_commit,
                            candidate_commit,
                            error_context="Unable to restore the deployment target",
                        )
                    elif observed_target != approved.base_commit:
                        raise AgentDeploymentError(
                            "Deployment target is neither the candidate nor rollback commit"
                        )
                    target_updated = False
                except Exception as rollback_exc:
                    rollback_errors.append(f"target: {rollback_exc}")

            if runtime_promotion_attempted or runtime_promoted:
                try:
                    self.runtime.restore(
                        expected_current=candidate_commit,
                        rollback_commit=approved.base_commit,
                    )
                    runtime_promoted = False
                except Exception as rollback_exc:
                    rollback_errors.append(f"runtime: {rollback_exc}")

            if frontend_install_attempted or frontend_installed:
                try:
                    attempt["frontend_restored"] = self.runtime.frontend_restore()
                    frontend_installed = False
                except Exception as rollback_exc:
                    rollback_errors.append(f"frontend: {rollback_exc}")

            if migrations_applied:
                try:
                    migrations_dir = (
                        candidate_path / "backend" / "database" / "migrations"
                    )
                    rolled_back = self.database.downgrade(
                        migrations_dir,
                        migrations_applied,
                    )
                    attempt["migrations_rolled_back"] = list(rolled_back)
                    migrations_applied = ()
                except Exception as rollback_exc:
                    rollback_errors.append(f"database: {rollback_exc}")

            try:
                self.runtime.start()
                service_stopped = False
                restored_health = self.runtime.verify()
                attempt["restored_health"] = asdict(restored_health)
            except Exception as rollback_exc:
                rollback_errors.append(f"restart: {rollback_exc}")

            attempt["rollback_performed"] = True
            attempt["rollback_errors"] = rollback_errors
            attempt["finished_at"] = _utc_now()
            if rollback_errors:
                attempt["status"] = "rollback_failed"
                self._write_manifest(manifest_path, manifest)
                raise DeploymentRollbackError(
                    "Deployment failed and automatic rollback was incomplete: "
                    + " | ".join(rollback_errors)
                ) from exc

            attempt["status"] = "rolled_back"
            self._write_manifest(manifest_path, manifest)
            raise DeploymentRolledBackError(
                f"Deployment failed and was rolled back safely: {exc}"
            ) from exc

    def _failed_implementation_test_is_blocking(
        self,
        metadata: dict,
        test: dict,
    ) -> bool:
        # Backend Codex test reports are advisory review evidence. The protected
        # deployment validator independently compiles the exact materialized
        # candidate and runs the complete test suite before any promotion.
        return False

    def _validate_implementation_tests(
        self,
        metadata: dict,
    ) -> tuple[dict, ...]:
        implementation_tests_value = metadata.get("tests")
        if not isinstance(implementation_tests_value, list):
            raise AgentDeploymentError(
                "Implementation result is missing test evidence"
            )
        implementation_tests: list[dict] = []
        for test in implementation_tests_value:
            if not isinstance(test, dict):
                raise AgentDeploymentError("Implementation test evidence is invalid")
            command = test.get("command")
            status = test.get("status")
            details = test.get("details")
            if (
                not isinstance(command, str)
                or not command.strip()
                or status not in {"passed", "failed", "not_run"}
                or not isinstance(details, str)
            ):
                raise AgentDeploymentError(
                    "Implementation test evidence is invalid"
                )
            if status == "failed" and self._failed_implementation_test_is_blocking(
                metadata,
                test,
            ):
                raise AgentDeploymentError(
                    "Deployment approval contains failed implementation tests"
                )
            implementation_tests.append(
                {
                    "command": command.strip(),
                    "status": status,
                    "details": details.strip(),
                }
            )
        return tuple(implementation_tests)

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
            or patch_size_bytes > self.MAX_PATCH_BYTES
        ):
            raise AgentDeploymentError(
                "Implementation result has an invalid backend patch size"
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
        if len(changed_files) > self.MAX_CHANGED_FILES:
            raise AgentDeploymentError(
                f"Backend deployment permits at most {self.MAX_CHANGED_FILES} changed files"
            )
        if len(changed_files) != len(changed_files_value):
            raise AgentDeploymentError(
                "Implementation changed-file evidence contains duplicates"
            )

        implementation_tests = self._validate_implementation_tests(metadata)

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
        self._require_backend_paths(changed_files, expected_worktree)

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
            implementation_tests=implementation_tests,
        )

    def _materialize_candidate(
        self,
        claim: ClaimedRun,
        approved: ApprovedImplementation,
    ) -> tuple[str, str, Path]:
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

        target_head = self._resolve_commit(self.target_repository, self.target_branch)
        existing_candidate = self._branch_commit(candidate_branch)
        qa_candidate_commit = None
        if self.environment == "production":
            qa_candidate_commit = self._qa_candidate_commit_from_pipeline(claim)
            if target_head not in {approved.base_commit, existing_candidate}:
                raise AgentDeploymentError(
                    "Deployment target advanced before candidate creation"
                )
            if existing_candidate is None:
                assert self.qa_candidate_repository is not None
                self._run_git(
                    self.target_repository,
                    "fetch",
                    str(self.qa_candidate_repository),
                    f"{qa_candidate_commit}:refs/heads/{candidate_branch}",
                    error_context=(
                        "Unable to import the QA-validated deployment candidate"
                    ),
                )
                existing_candidate = self._branch_commit(candidate_branch)
            if existing_candidate != qa_candidate_commit:
                raise AgentDeploymentError(
                    "Production candidate does not match the QA-validated candidate"
                )
        if target_head not in {approved.base_commit, existing_candidate}:
            raise AgentDeploymentError(
                "Deployment target advanced before candidate creation"
            )

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
                error_context="Unable to recover the deployment candidate",
            )
            self._verify_worktree(
                candidate_path,
                expected_branch=candidate_branch,
                expected_common_directory=self.target_common_directory,
            )
        else:
            if target_head != approved.base_commit:
                raise AgentDeploymentError(
                    "Deployment target advanced before candidate creation"
                )
            self._run_git(
                self.target_repository,
                "worktree",
                "add",
                "-b",
                candidate_branch,
                str(candidate_path),
                approved.base_commit,
                error_context="Unable to create the deployment candidate",
            )
            self._verify_worktree(
                candidate_path,
                expected_branch=candidate_branch,
                expected_common_directory=self.target_common_directory,
            )

        branch_head = self._resolve_commit(candidate_path, "HEAD")
        if branch_head == approved.base_commit and self.environment != "production":
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
                    "Deployment candidate changed files do not match approval evidence"
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
                error_context="Unable to create the immutable deployment commit",
            )
            candidate_commit = self._resolve_commit(candidate_path, "HEAD")
        else:
            candidate_commit = branch_head

        if (
            qa_candidate_commit is not None
            and candidate_commit != qa_candidate_commit
        ):
            raise AgentDeploymentError(
                "Production candidate does not match the QA-validated candidate"
            )

        self._validate_candidate_commit(candidate_commit, approved)
        self._require_clean(candidate_path)
        return candidate_branch, candidate_commit, candidate_path

    def _validate_candidate_commit(
        self,
        candidate_commit: str,
        approved: ApprovedImplementation,
    ) -> None:
        parent = self._resolve_commit(self.target_repository, f"{candidate_commit}^")
        if parent != approved.base_commit:
            raise AgentDeploymentError(
                "Deployment candidate has an unexpected parent commit"
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
                    error_context="Unable to inspect deployment commit",
                ).stdout.splitlines()
            )
        )
        if changed_files != approved.changed_files:
            raise AgentDeploymentError(
                "Deployment commit differs from approved changed files"
            )
        candidate_tree = self._run_git(
            self.target_repository,
            "rev-parse",
            "--verify",
            f"{candidate_commit}^{{tree}}",
            error_context="Unable to resolve deployment candidate tree",
        ).stdout.strip()
        if candidate_tree != approved.expected_tree:
            raise AgentDeploymentError(
                "Deployment commit tree differs from the approved worktree"
            )

    def _migration_plan(
        self,
        base_commit: str,
        candidate_commit: str,
        candidate_path: Path,
    ) -> MigrationPlan:
        migration_prefix = "backend/database/migrations/"
        migrations_dir = candidate_path / migration_prefix
        expected_history = _expected_migration_history(migrations_dir)
        status_lines = self._run_git(
            self.target_repository,
            "diff",
            "--name-status",
            "--no-renames",
            base_commit,
            candidate_commit,
            "--",
            migration_prefix,
            error_context="Unable to inspect candidate migrations",
        ).stdout.splitlines()
        if not status_lines:
            return MigrationPlan(expected_history=expected_history)

        added_files: list[str] = []
        grouped: dict[tuple[str, str], set[str]] = {}
        for line in status_lines:
            parts = line.split("\t")
            if len(parts) != 2 or parts[0] != "A":
                raise AgentDeploymentError(
                    "Historical migration files may not be modified, renamed, or deleted"
                )
            relative = parts[1]
            filename = PurePosixPath(relative).name
            match = migration_runner.MIGRATION_PATTERN.fullmatch(filename)
            if match is None:
                raise AgentDeploymentError(
                    f"Invalid migration filename: {relative}"
                )
            key = (match.group("version"), match.group("name"))
            grouped.setdefault(key, set()).add(match.group("direction"))
            added_files.append(relative)
            self._validate_migration_sql(
                candidate_path / relative,
                direction=match.group("direction"),
            )

        base_files = self._run_git(
            self.target_repository,
            "ls-tree",
            "-r",
            "--name-only",
            base_commit,
            "--",
            migration_prefix,
            error_context="Unable to inspect historical migration versions",
        ).stdout.splitlines()
        base_versions = [
            match.group("version")
            for path in base_files
            if (
                match := migration_runner.MIGRATION_PATTERN.fullmatch(
                    PurePosixPath(path).name
                )
            )
        ]
        maximum_base = max(base_versions, default="0000")

        ordered = sorted(grouped.items())
        for (version, name), directions in ordered:
            if directions != {"up", "down"}:
                raise AgentDeploymentError(
                    f"Migration {version}_{name} must add both up and down SQL files"
                )
            if version <= maximum_base:
                raise AgentDeploymentError(
                    f"New migration version {version} must be greater than {maximum_base}"
                )
            self._validate_migration_pair(
                candidate_path / migration_prefix / f"{version}_{name}.up.sql",
                candidate_path / migration_prefix / f"{version}_{name}.down.sql",
            )

        return MigrationPlan(
            versions=tuple(key[0] for key, _ in ordered),
            names=tuple(key[1] for key, _ in ordered),
            files=tuple(sorted(added_files)),
            expected_history=expected_history,
        )

    @staticmethod
    def _validate_migration_sql(path: Path, *, direction: str) -> None:
        if path.is_symlink() or not path.is_file():
            raise AgentDeploymentError("Migration must be a regular file")
        if path.stat().st_size > 500_000:
            raise AgentDeploymentError("Migration SQL file is too large")
        try:
            sql = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AgentDeploymentError("Migration SQL must be UTF-8") from exc
        if not sql.strip() or "\x00" in sql:
            raise AgentDeploymentError("Migration SQL is empty or binary")
        if re.search(r"(?m)^\s*\\", sql):
            raise AgentDeploymentError("Migration SQL may not contain psql meta-commands")

        normalized = re.sub(r"--[^\n]*", " ", sql)
        normalized = re.sub(r"/\*.*?\*/", " ", normalized, flags=re.DOTALL)
        privileged_patterns = (
            r"\bALTER\s+SYSTEM\b",
            r"\bCREATE\s+(?:USER|ROLE|DATABASE|EXTENSION|FUNCTION|PROCEDURE)\b",
            r"\bDROP\s+(?:USER|ROLE|DATABASE|EXTENSION)\b",
            r"\bGRANT\b",
            r"\bREVOKE\b",
            r"\bSET\s+ROLE\b",
            r"\bSECURITY\s+DEFINER\b",
            r"\bCOPY\b[\s\S]*\bPROGRAM\b",
            r"\bDO\s+\$",
            r"\bCALL\b",
            r"\bpg_(?:read|write)_file\b",
            r"\blo_import\b",
            r"\bdblink\b",
            r"\bCREATE\s+INDEX\s+CONCURRENTLY\b",
            r"\b(?:BEGIN|COMMIT|ROLLBACK)\s*;",
        )
        for pattern in privileged_patterns:
            if re.search(pattern, normalized, flags=re.IGNORECASE):
                raise AgentDeploymentError(
                    "Migration SQL contains a blocked privileged or nontransactional statement"
                )

        if direction == "up":
            destructive_patterns = (
                r"\bDROP\s+(?:TABLE|SCHEMA|TYPE|INDEX|SEQUENCE|VIEW|MATERIALIZED\s+VIEW)\b",
                r"\bTRUNCATE\b",
                r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO)\b",
                r"\bALTER\s+TABLE\b[\s\S]*?\bDROP\s+(?:COLUMN|CONSTRAINT)\b",
                r"\bALTER\s+TABLE\b[\s\S]*?\bALTER\s+COLUMN\b[\s\S]*?\bTYPE\b",
                r"\bALTER\s+TABLE\b[\s\S]*?\bALTER\s+COLUMN\b[\s\S]*?\bDROP\s+(?:DEFAULT|NOT\s+NULL)\b",
            )
            for pattern in destructive_patterns:
                if re.search(pattern, normalized, flags=re.IGNORECASE):
                    raise AgentDeploymentError(
                        "Destructive or irreversible up migrations are blocked"
                    )

    @classmethod
    def _validate_migration_pair(cls, up_path: Path, down_path: Path) -> None:
        up_sql = cls._normalized_migration_sql(up_path)
        down_sql = cls._normalized_migration_sql(down_path)
        if re.search(r"\bCASCADE\b", down_sql, flags=re.IGNORECASE):
            raise AgentDeploymentError(
                "Automatic down migrations may not use CASCADE"
            )
        if re.search(
            r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO|TRUNCATE)\b",
            down_sql,
            flags=re.IGNORECASE,
        ):
            raise AgentDeploymentError(
                "Automatic down migrations may not mutate application data"
            )

        identifier = r"[a-z_][a-z0-9_$]*(?:\.[a-z_][a-z0-9_$]*)?"
        object_pattern = re.compile(
            rf"\bDROP\s+(MATERIALIZED\s+VIEW|TABLE|SCHEMA|TYPE|INDEX|SEQUENCE|VIEW)"
            rf"\s+(?:IF\s+EXISTS\s+)?({identifier})",
            flags=re.IGNORECASE,
        )
        create_keywords = {
            "MATERIALIZED VIEW": r"MATERIALIZED\s+VIEW",
            "TABLE": r"TABLE",
            "SCHEMA": r"SCHEMA",
            "TYPE": r"TYPE",
            "INDEX": r"(?:UNIQUE\s+)?INDEX",
            "SEQUENCE": r"SEQUENCE",
            "VIEW": r"VIEW",
        }
        for match in object_pattern.finditer(down_sql):
            object_type = re.sub(r"\s+", " ", match.group(1).upper())
            object_name = re.escape(match.group(2))
            create_pattern = (
                rf"\bCREATE\s+{create_keywords[object_type]}\s+"
                rf"(?:IF\s+NOT\s+EXISTS\s+)?{object_name}\b"
            )
            if not re.search(create_pattern, up_sql, flags=re.IGNORECASE):
                raise AgentDeploymentError(
                    "Down migration drops an object not created by its up migration: "
                    f"{match.group(2)}"
                )

        alter_drop_pattern = re.compile(
            rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?({identifier})"
            rf"[^;]*?\bDROP\s+(COLUMN|CONSTRAINT)\s+"
            rf"(?:IF\s+EXISTS\s+)?({identifier})",
            flags=re.IGNORECASE,
        )
        for match in alter_drop_pattern.finditer(down_sql):
            table_name = re.escape(match.group(1))
            object_type = match.group(2).upper()
            object_name = re.escape(match.group(3))
            if object_type == "COLUMN":
                add_pattern = (
                    rf"\bALTER\s+TABLE\s+{table_name}[^;]*?"
                    rf"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
                    rf"{object_name}\b"
                )
            else:
                add_pattern = (
                    rf"\bALTER\s+TABLE\s+{table_name}[^;]*?"
                    rf"\bADD\s+CONSTRAINT\s+{object_name}\b"
                )
            if not re.search(add_pattern, up_sql, flags=re.IGNORECASE):
                raise AgentDeploymentError(
                    "Down migration removes a table member not added by its up migration: "
                    f"{match.group(1)}.{match.group(3)}"
                )

        trigger_pattern = re.compile(
            rf"\bDROP\s+TRIGGER\s+(?:IF\s+EXISTS\s+)?({identifier})"
            rf"\s+ON\s+({identifier})",
            flags=re.IGNORECASE,
        )
        for match in trigger_pattern.finditer(down_sql):
            trigger_name = re.escape(match.group(1))
            table_name = re.escape(match.group(2))
            if not re.search(
                rf"\bCREATE\s+TRIGGER\s+{trigger_name}[^;]*?\bON\s+{table_name}\b",
                up_sql,
                flags=re.IGNORECASE,
            ):
                raise AgentDeploymentError(
                    "Down migration drops a trigger not created by its up migration: "
                    f"{match.group(1)}"
                )

    @staticmethod
    def _normalized_migration_sql(path: Path) -> str:
        try:
            sql = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AgentDeploymentError("Migration SQL must be UTF-8") from exc
        normalized = re.sub(r"--[^\n]*", " ", sql)
        return re.sub(r"/\*.*?\*/", " ", normalized, flags=re.DOTALL)

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
            if relative in self.PROTECTED_BACKEND_PATHS:
                raise AgentDeploymentError(
                    f"Automatic deployment blocks protected control-plane file: {relative}"
                )
            allowed = False
            frontend_file = False
            if len(pure_path.parts) >= 2 and pure_path.parts[0] == "backend":
                allowed = pure_path.suffix == ".py"
                if pure_path.parts[:3] == (
                    "backend",
                    "database",
                    "migrations",
                ):
                    allowed = bool(
                        migration_runner.MIGRATION_PATTERN.fullmatch(pure_path.name)
                    )
            elif len(pure_path.parts) >= 2 and pure_path.parts[0] == "tests":
                allowed = pure_path.suffix == ".py"
            elif len(pure_path.parts) >= 2 and pure_path.parts[0] == "docs":
                allowed = pure_path.suffix.lower() == ".md"
            elif len(pure_path.parts) >= 2 and pure_path.parts[0] == "frontend-web":
                frontend_file = True
                allowed = self._frontend_path_allowed(relative, pure_path)
            if not allowed:
                raise AgentDeploymentError(
                    "Backend deployment permits only backend Python, tests, docs, "
                    "paired SQL migration files, and reviewed frontend-web source files"
                )

            actual_path = worktree.joinpath(*pure_path.parts)
            if actual_path.is_symlink():
                raise AgentDeploymentError("Backend deployment rejects symbolic links")
            if actual_path.exists():
                resolved = actual_path.resolve()
                if worktree.resolve() not in resolved.parents:
                    raise AgentDeploymentError("Changed path escapes the worktree")
                if not actual_path.is_file():
                    raise AgentDeploymentError(
                        "Backend deployment permits only regular files"
                    )
                mode = actual_path.stat(follow_symlinks=False).st_mode
                if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode) or stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
                    raise AgentDeploymentError("Backend deployment rejects special files")
                if frontend_file:
                    if mode & 0o111:
                        raise AgentDeploymentError(
                            "Frontend deployment rejects unexpected executable files"
                        )
                    if actual_path.stat(follow_symlinks=False).st_size > 5_000_000:
                        raise AgentDeploymentError(
                            "Frontend deployment rejects unsafe oversized assets"
                        )
                try:
                    content = actual_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    if frontend_file and pure_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp"}:
                        continue
                    raise AgentDeploymentError("Backend deployment requires UTF-8 text files") from exc
                if "\x00" in content:
                    raise AgentDeploymentError("Backend deployment rejects binary files")

    def _frontend_path_allowed(self, relative: str, pure_path: PurePosixPath) -> bool:
        name = pure_path.name
        lowered = name.lower()
        if lowered == ".env" or lowered.startswith(".env."):
            raise AgentDeploymentError("Frontend deployment rejects environment files")
        if self.FRONTEND_SECRET_NAME_RE.search(name):
            raise AgentDeploymentError("Frontend deployment rejects secret-like files")
        if relative in self.FRONTEND_FIXED_FILES:
            return True
        if len(pure_path.parts) < 3:
            return False
        if pure_path.parts[1] in {"dist", "node_modules"}:
            raise AgentDeploymentError("Frontend deployment rejects generated dependency or dist paths")
        if pure_path.parts[1] not in {"src", "public"}:
            return False
        return pure_path.suffix.lower() in self.FRONTEND_SOURCE_SUFFIXES

    @staticmethod
    def _reject_special_git_modes(patch: bytes) -> None:
        forbidden = (
            b"new file mode 120000",
            b"old mode 120000",
            b"new file mode 160000",
            b"old mode 160000",
            b"new file mode 100755",
            b"old file mode 100755",
            b"old mode 100755",
            b"new mode 100755",
            b"Subproject commit ",
        )
        if any(marker in patch for marker in forbidden):
            raise AgentDeploymentError(
                "Backend deployment rejects executable files, symlinks, and submodules"
            )

    def _rollback_ref(self, claim: ClaimedRun) -> str:
        return f"rollback-before-agent-card-{claim.card_id}-r{claim.card_revision}"

    def _ensure_rollback_ref(self, rollback_ref: str, base_commit: str) -> None:
        existing = self._tag_commit(rollback_ref)
        if existing is None:
            self._run_git(
                self.target_repository,
                "-c",
                "user.name=RemiHub Deployment",
                "-c",
                "user.email=remihub-deployment@invalid.local",
                "tag",
                "-a",
                rollback_ref,
                base_commit,
                "-m",
                f"Rollback before {rollback_ref}",
                error_context="Unable to create the deployment rollback reference",
            )
        elif existing != base_commit:
            raise AgentDeploymentError(
                "Existing deployment rollback reference points to another commit"
            )

    def _tag_commit(self, tag: str) -> str | None:
        result = self._run_git(
            self.target_repository,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/tags/{tag}^{{commit}}",
            allowed_return_codes=(0, 1),
            error_context="Unable to inspect deployment rollback reference",
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def _manifest_path(self, claim: ClaimedRun) -> Path:
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
        return card_root / f"{claim.id}.deployment.json"

    def _load_or_initialize_manifest(
        self,
        claim: ClaimedRun,
        approved: ApprovedImplementation,
        *,
        candidate_branch: str,
        candidate_commit: str,
        rollback_ref: str,
        migration_plan: MigrationPlan,
        frontend_changed: bool,
    ) -> dict:
        manifest_path = self._manifest_path(claim)
        identity = {
            "schema_version": 3,
            "environment": self.environment,
            "mode": "backend-qa-to-production",
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
            "implementation_tests": list(approved.implementation_tests),
            "rollback_ref": rollback_ref,
            "migration_plan": asdict(migration_plan),
            "frontend_changed": frontend_changed,
            "android_release_performed": False,
        }
        # Normalize tuples to the same JSON-native list representation used after reload.
        identity = json.loads(json.dumps(identity))
        if not manifest_path.exists():
            return {**identity, "attempts": []}
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise AgentDeploymentError(
                "Existing deployment manifest is not a regular file"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentDeploymentError("Existing deployment manifest is invalid") from exc
        for key, value in identity.items():
            if manifest.get(key) != value:
                raise AgentDeploymentError(
                    f"Existing deployment manifest conflicts on {key}"
                )
        if not isinstance(manifest.get("attempts"), list):
            raise AgentDeploymentError("Existing deployment manifest attempts are invalid")
        return manifest

    @staticmethod
    def _begin_attempt(manifest: dict, claim: ClaimedRun) -> dict:
        attempts = manifest["attempts"]
        attempt = {
            "attempt_index": len(attempts) + 1,
            "worker_attempt_count": claim.attempt_count,
            "started_at": _utc_now(),
            "finished_at": None,
            "status": "running",
            "stage": "candidate_created",
            "migrations_applied": [],
            "migrations_rolled_back": [],
            "rollback_performed": False,
            "rollback_errors": [],
        }
        attempts.append(attempt)
        return attempt

    @staticmethod
    def _successful_attempt(manifest: dict) -> dict | None:
        for attempt in reversed(manifest["attempts"]):
            if attempt.get("status") == "succeeded":
                return attempt
        return None

    def _candidate_from_success(
        self,
        approved: ApprovedImplementation,
        *,
        candidate_branch: str,
        candidate_commit: str,
        rollback_ref: str,
        manifest_path: Path,
        attempt: dict,
        health: RuntimeHealth,
        frontend_artifact: FrontendArtifactEvidence | None,
    ) -> DeploymentCandidate:
        validation = attempt.get("validation") or {}
        backup = attempt.get("database_backup")
        return DeploymentCandidate(
            approval_id=approved.approval_id,
            implementation_run_id=approved.implementation_run_id,
            environment=self.environment,
            candidate_branch=candidate_branch,
            candidate_commit=candidate_commit,
            target_branch=self.target_branch,
            target_before=approved.base_commit,
            target_after=candidate_commit,
            base_commit=approved.base_commit,
            changed_files=approved.changed_files,
            patch_sha256=approved.patch_sha256,
            patch_size_bytes=approved.patch_size_bytes,
            manifest_path=str(manifest_path),
            rollback_ref=rollback_ref,
            validation={**validation, "idempotent_health": asdict(health)},
            frontend_artifact=(
                asdict(frontend_artifact) if frontend_artifact is not None else None
            ),
            service_restart_performed=True,
            migrations_applied=tuple(attempt.get("migrations_applied") or ()),
            database_backup=backup,
            rollback_performed=False,
        )

    def _required_qa_history_reader(self) -> MigrationHistoryReader:
        if self.qa_history_reader is None:
            raise AgentWorkerConfigurationError(
                "Production backend deployment requires QA migration parity reader"
            )
        return self.qa_history_reader

    def _record_and_require_migration_history(
        self,
        attempt: dict,
        manifest_path: Path,
        manifest: dict,
        *,
        source: str,
        stage: str,
        expected_history: tuple[dict[str, str], ...],
        reader: MigrationHistoryReader,
    ) -> None:
        observed_history = reader.migration_history()
        evidence = {
            "source": source,
            "checked_at": _utc_now(),
            "expected_history": list(expected_history),
            "observed_history": list(observed_history),
        }
        attempt[stage] = evidence
        attempt["stage"] = stage
        self._write_manifest(manifest_path, manifest)
        _require_exact_migration_history(
            source=source,
            expected=expected_history,
            observed=observed_history,
        )

    @staticmethod
    def _write_manifest(path: Path, manifest: dict) -> None:
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.chmod(temporary_path, 0o640)
        temporary_path.replace(path)

    def _worktree_tree(
        self,
        worktree: Path,
        changed_files: tuple[str, ...],
    ) -> str:
        with tempfile.TemporaryDirectory(
            prefix="remihub-deployment-index-",
            dir=self.deployment_artifact_root,
        ) as temporary_directory:
            temporary_root = Path(temporary_directory)
            index_path = temporary_root / "index"
            object_directory = temporary_root / "objects"
            object_directory.mkdir(mode=0o750)
            environment_overrides = {
                "GIT_INDEX_FILE": str(index_path),
                "GIT_OBJECT_DIRECTORY": str(object_directory),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(
                    self.source_common_directory / "objects"
                ),
            }
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
                    error_context="Unable to inspect staged deployment changes",
                ).stdout.splitlines()
            )
        )

    def _require_clean(self, worktree: Path) -> None:
        status = self._run_git(
            worktree,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            error_context="Unable to inspect deployment candidate worktree",
        ).stdout.strip()
        if status:
            raise AgentDeploymentError(
                "Deployment candidate worktree contains unexpected changes"
            )

    def _branch_commit(self, branch: str) -> str | None:
        result = self._run_git(
            self.target_repository,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}^{{commit}}",
            allowed_return_codes=(0, 1),
            error_context="Unable to inspect deployment candidate branch",
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
    def _required_string(mapping: dict, key: str) -> str:
        value = mapping.get(key)
        if not isinstance(value, str) or not value.strip():
            raise AgentDeploymentError(f"Implementation result is missing {key}")
        return value.strip()

    @classmethod
    def _required_commit(cls, mapping: dict, key: str) -> str:
        value = cls._required_string(mapping, key)
        if not re.fullmatch(r"[0-9a-f]{40}", value):
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
        if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
            raise AgentDeploymentError(f"{field} escapes its configured root")

    def _git_environment(self) -> dict[str, str]:
        return {
            "GIT_CONFIG_GLOBAL": (
                "/opt/remihub-agent/deployment/config/"
                "git-safe-directory.ini"
            ),
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
                _exact_git_command(self.git_binary, repository, arguments),
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
            detail = [
                line.strip()
                for line in result.stderr.splitlines()
                if line.strip()
            ]
            suffix = f": {' | '.join(detail[-3:])}" if detail else ""
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
                _exact_git_command(self.git_binary, repository, arguments),
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


def _exact_git_command(
    git_binary: str,
    repository: Path,
    arguments: Sequence[str],
) -> list[str]:
    resolved_repository = repository.resolve()
    return [
        git_binary,
        "-c",
        f"safe.directory={resolved_repository}",
        "-C",
        str(resolved_repository),
        *arguments,
    ]


class GitBackendDeploymentExecutor:
    allowed_phases = frozenset({RunPhase.DEPLOYMENT})
    allowed_repository_scopes = frozenset({RepositoryScope.BACKEND})

    def __init__(
        self,
        *,
        deployment_manager: GitBackendDeploymentManager,
        github_synchronizer: BackendGitHubSynchronizer | None = None,
        retry_after_seconds: int = 60,
    ):
        if retry_after_seconds < 1:
            raise ValueError("retry_after_seconds must be at least 1")
        if deployment_manager.environment == "production" and github_synchronizer is None:
            raise AgentWorkerConfigurationError(
                "Production backend deployment requires a GitHub synchronizer"
            )
        if deployment_manager.environment != "production" and github_synchronizer is not None:
            raise AgentWorkerConfigurationError(
                "Backend GitHub synchronization is restricted to production"
            )
        self.deployment_manager = deployment_manager
        self.github_synchronizer = github_synchronizer
        self.retry_after_seconds = retry_after_seconds

    def execute(self, claim: ClaimedRun) -> ExecutionResult:
        require_backend_repository_scope(
            claim.repository_scope,
            action="Deployment",
        )
        try:
            candidate = self.deployment_manager.deploy(claim)
        except DeploymentRolledBackError as exc:
            raise AgentTemporarilyBlockedError(
                str(exc),
                retry_after_seconds=self.retry_after_seconds,
            ) from exc

        github_sync: dict[str, Any] | None = None
        if candidate.environment == "production":
            assert self.github_synchronizer is not None
            try:
                github_sync = self.github_synchronizer.synchronize(
                    candidate_commit=candidate.candidate_commit,
                    base_commit=candidate.base_commit,
                    card_id=claim.card_id,
                    deployment_run_id=claim.id,
                )
            except AgentDeploymentError as exc:
                blocker_code = github_sync_blocker_code(str(exc))
                retryable = github_sync_retryable(blocker_code)
                recovery = deployment_recovery_metadata(
                    github_sync_status=(
                        GITHUB_SYNC_FAILED_RETRYABLE
                        if retryable
                        else GITHUB_SYNC_FAILED_NON_RETRYABLE
                    ),
                    retryable=retryable,
                    blocker_code=blocker_code,
                    last_error=_tail(str(exc), 3000),
                    candidate_commit=candidate.candidate_commit,
                    deployment_run_id=claim.id,
                    production_deployed=True,
                )
                pending = {
                    "status": "pending",
                    "candidate_commit": candidate.candidate_commit,
                    "blocker_code": blocker_code,
                    "failure_reason": _tail(str(exc), 3000),
                    "retryable": retryable,
                }
                record_error = None
                try:
                    self.deployment_manager.record_github_sync(
                        claim,
                        candidate,
                        pending,
                    )
                except AgentDeploymentError as evidence_exc:
                    record_error = evidence_exc
                reason = _tail(str(exc), 1000)
                if record_error is not None:
                    reason = (
                        f"{reason}; manifest evidence is also pending: "
                        f"{_tail(str(record_error), 1000)}"
                    )
                raise AgentTemporarilyBlockedError(
                    "Backend deployment and local source synchronization succeeded, "
                    f"but GitHub synchronization is pending: {reason}",
                    retry_after_seconds=self.retry_after_seconds,
                    metadata={
                        "executor": "git_backend_deployment",
                        "phase": claim.phase.value,
                        "environment": candidate.environment,
                        "mode": "backend-qa-to-production",
                        "candidate": asdict(candidate),
                        "github_sync": pending,
                        "deployment_recovery": recovery,
                    },
                ) from exc
            try:
                self.deployment_manager.record_github_sync(
                    claim,
                    candidate,
                    github_sync,
                )
            except AgentDeploymentError as exc:
                raise AgentTemporarilyBlockedError(
                    "Backend GitHub synchronization was verified, but deployment "
                    f"manifest evidence is pending: {_tail(str(exc), 1000)}",
                    retry_after_seconds=self.retry_after_seconds,
                ) from exc

        changed_files = "\n".join(
            f"- `{path}`" for path in candidate.changed_files
        )
        migration_text = (
            ", ".join(candidate.migrations_applied)
            if candidate.migrations_applied
            else "none"
        )
        github_text = (
            "verified" if github_sync is not None else "not applicable in QA"
        )
        message = f"""
Deployed the approved backend candidate to {candidate.environment}.

- Candidate commit: `{candidate.candidate_commit}`
- Target branch: `{candidate.target_branch}`
- Rollback reference: `{candidate.rollback_ref}`
- Service restart performed: yes
- Migrations applied: {migration_text}
- Health check: `/openapi.json` passed
- GitHub synchronization: {github_text}

Changed files:
{changed_files}
""".strip()
        return ExecutionResult(
            message=message,
            card_status=(
                CardStatus.DEPLOYMENT_QUEUED
                if candidate.environment == "qa"
                else CardStatus.COMPLETED
            ),
            metadata={
                "executor": "git_backend_deployment",
                "phase": claim.phase.value,
                "environment": candidate.environment,
                "mode": "backend-qa-to-production",
                "deployment_pipeline": {
                    "stage": (
                        "qa_succeeded"
                        if candidate.environment == "qa"
                        else "production_succeeded"
                    ),
                    "card_id": claim.card_id,
                    "deployment_run_id": claim.id,
                    "candidate_commit": candidate.candidate_commit,
                    "qa_candidate_repository": (
                        str(self.deployment_manager.target_repository)
                        if candidate.environment == "qa"
                        else None
                    ),
                },
                "candidate": asdict(candidate),
                "github_sync": github_sync,
                "deployment_recovery": deployment_recovery_metadata(
                    github_sync_status=(
                        GITHUB_SYNC_SUCCEEDED
                        if github_sync is not None
                        else GITHUB_SYNC_LOCAL_INCOMPLETE
                    ),
                    retryable=False,
                    blocker_code=None,
                    last_error=None,
                    candidate_commit=candidate.candidate_commit,
                    deployment_run_id=claim.id,
                    production_deployed=candidate.environment == "production",
                ),
            },
        )



def _required_absolute_directory(value: str | Path, *, field: str) -> Path:
    configured = Path(value).expanduser()
    if not configured.is_absolute():
        raise AgentWorkerConfigurationError(f"{field} must be an absolute path")
    resolved = configured.resolve()
    if not resolved.is_dir():
        raise AgentWorkerConfigurationError(
            f"{field} does not exist or is not a directory: {resolved}"
        )
    return resolved


def _required_absolute_file(value: str | Path, *, field: str) -> Path:
    configured = Path(value).expanduser()
    if not configured.is_absolute():
        raise AgentWorkerConfigurationError(f"{field} must be an absolute path")
    if configured.is_symlink() or not configured.is_file():
        raise AgentWorkerConfigurationError(
            f"{field} does not exist or is not a regular file: {configured}"
        )
    return configured.resolve()


def _required_executable(value: str | Path, *, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise AgentWorkerConfigurationError(f"{field} must be an absolute path")
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AgentWorkerConfigurationError(f"{field} is not executable: {path}")
    return path.resolve()


def _expected_migration_history(migrations_dir: Path) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "version": migration.version,
            "name": migration.name,
            "checksum": migration.checksum,
        }
        for migration in migration_runner.discover_migrations(migrations_dir)
    )


def _migration_history_from_applied(
    applied: dict[str, dict[str, str]],
) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "version": version,
            "name": record["name"],
            "checksum": record["checksum"],
        }
        for version, record in sorted(applied.items())
    )


def _require_exact_migration_history(
    *,
    source: str,
    expected: tuple[dict[str, str], ...],
    observed: tuple[dict[str, str], ...],
) -> None:
    if observed == expected:
        return

    expected_by_version = {item["version"]: item for item in expected}
    observed_by_version = {item["version"]: item for item in observed}

    missing = tuple(
        item["version"]
        for item in expected
        if item["version"] not in observed_by_version
    )
    unexpected = tuple(
        item["version"]
        for item in observed
        if item["version"] not in expected_by_version
    )
    mismatched_names = tuple(
        version
        for version, item in expected_by_version.items()
        if version in observed_by_version
        and observed_by_version[version]["name"] != item["name"]
    )
    mismatched_checksums = tuple(
        version
        for version, item in expected_by_version.items()
        if version in observed_by_version
        and observed_by_version[version]["name"] == item["name"]
        and observed_by_version[version]["checksum"] != item["checksum"]
    )

    details: list[str] = []
    if missing:
        details.append(f"missing={missing!r}")
    if unexpected:
        details.append(f"unexpected={unexpected!r}")
    if mismatched_names:
        details.append(f"name_mismatch={mismatched_names!r}")
    if mismatched_checksums:
        details.append(f"checksum_mismatch={mismatched_checksums!r}")
    suffix = "; ".join(details) or "history differs"
    raise AgentDeploymentError(
        f"{source} migration history does not match the approved candidate: {suffix}"
    )


def _connect_explicit_database_config(config_path: Path):
    import psycopg2

    config = load_config(config_path)["Database"]
    return psycopg2.connect(
        user=config["user"],
        password=config["password"],
        host=config["host"],
        port=config["port"],
        database=config["database"],
    )


def _database_identity(conn) -> tuple[str | None, ...]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                current_database(),
                session_user,
                current_user,
                inet_server_addr()::text,
                inet_server_port()::text;
            """
        )
        row = cur.fetchone()
    return tuple(None if value is None else str(value) for value in row)


def verify_distinct_database_identities(
    first: MigrationHistoryReader,
    second: MigrationHistoryReader,
) -> None:
    first_identity = first.database_identity()
    second_identity = second.database_identity()
    first_database = (first_identity[0], first_identity[3], first_identity[4])
    second_database = (second_identity[0], second_identity[3], second_identity[4])
    if first_database == second_database:
        raise AgentWorkerConfigurationError(
            "Production database and QA parity database must be distinct"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail(value: str, maximum: int) -> str:
    normalized = value.strip()
    if len(normalized) <= maximum:
        return normalized
    return normalized[-maximum:]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

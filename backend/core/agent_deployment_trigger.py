from __future__ import annotations

import os
from pathlib import Path

from backend.core.agent_state import RepositoryScope, coerce_repository_scope


TRIGGER_DIRECTORY = Path("/run/remihub-agent/deployment-trigger")
TRIGGER_REQUESTS = {
    RepositoryScope.BACKEND: TRIGGER_DIRECTORY / "backend.request",
    RepositoryScope.ANDROID: TRIGGER_DIRECTORY / "android.request",
}
TRIGGER_FILE_MODE = 0o640


class AgentDeploymentTriggerError(RuntimeError):
    pass


def _write_request(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)

    descriptor = os.open(path, flags, TRIGGER_FILE_MODE)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise OSError("deployment trigger request write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def trigger_deployment_worker(scope: RepositoryScope | str) -> None:
    normalized_scope = coerce_repository_scope(scope)
    try:
        request_path = TRIGGER_REQUESTS[normalized_scope]
    except KeyError as exc:
        raise AgentDeploymentTriggerError(
            "Deployment worker trigger requires backend or android scope"
        ) from exc

    try:
        _write_request(
            request_path,
            f"{normalized_scope.value}\n".encode("ascii"),
        )
    except OSError as exc:
        raise AgentDeploymentTriggerError(
            f"Unable to request the {normalized_scope.value} deployment worker: {exc}"
        ) from exc

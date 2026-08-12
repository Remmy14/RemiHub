from __future__ import annotations

import os
from pathlib import Path

from backend.core.agent_state import RepositoryScope, coerce_repository_scope


TRIGGER_DIRECTORY = Path("/run/remihub-agent/deployment-trigger")
BACKEND_QA_TRIGGER_REQUEST = TRIGGER_DIRECTORY / "backend-qa.request"
BACKEND_PRODUCTION_TRIGGER_REQUEST = TRIGGER_DIRECTORY / "backend-production.request"
TRIGGER_REQUESTS = {
    RepositoryScope.BACKEND: BACKEND_QA_TRIGGER_REQUEST,
    RepositoryScope.ANDROID: TRIGGER_DIRECTORY / "android.request",
}
TRIGGER_FILE_MODE = 0o640
DEPLOYMENT_TRIGGER_ENVIRONMENTS = frozenset({"qa", "production"})


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


def _deployment_request_path(
    scope: RepositoryScope,
    *,
    deployment_environment: str | None,
) -> Path:
    if scope is not RepositoryScope.BACKEND:
        if deployment_environment is not None:
            raise AgentDeploymentTriggerError(
                "Deployment environment wake targets are only valid for backend"
            )
        try:
            return TRIGGER_REQUESTS[scope]
        except KeyError as exc:
            raise AgentDeploymentTriggerError(
                "Deployment worker trigger requires backend or android scope"
            ) from exc

    environment = deployment_environment or "qa"
    if environment not in DEPLOYMENT_TRIGGER_ENVIRONMENTS:
        raise AgentDeploymentTriggerError(
            "Backend deployment worker trigger requires qa or production environment"
        )
    if environment == "production":
        return BACKEND_PRODUCTION_TRIGGER_REQUEST
    return BACKEND_QA_TRIGGER_REQUEST


def trigger_deployment_worker(
    scope: RepositoryScope | str,
    *,
    deployment_environment: str | None = None,
) -> None:
    normalized_scope = coerce_repository_scope(scope)
    request_path = _deployment_request_path(
        normalized_scope,
        deployment_environment=deployment_environment,
    )
    request_payload = (
        f"backend-{deployment_environment or 'qa'}"
        if normalized_scope is RepositoryScope.BACKEND
        else normalized_scope.value
    )

    try:
        _write_request(
            request_path,
            f"{request_payload}\n".encode("ascii"),
        )
    except OSError as exc:
        raise AgentDeploymentTriggerError(
            f"Unable to request the {normalized_scope.value} deployment worker: {exc}"
        ) from exc

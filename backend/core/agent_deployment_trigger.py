from __future__ import annotations

import os
from pathlib import Path
import subprocess

from backend.core.agent_state import RepositoryScope, coerce_repository_scope


TRIGGER_HELPER = Path("/usr/local/libexec/remihub-agent-deployment-trigger")
SUDO_BINARY = Path("/usr/bin/sudo")
TRIGGER_TIMEOUT_SECONDS = 15


class AgentDeploymentTriggerError(RuntimeError):
    pass


def trigger_deployment_worker(scope: RepositoryScope | str) -> None:
    normalized_scope = coerce_repository_scope(scope)
    if normalized_scope not in {RepositoryScope.BACKEND, RepositoryScope.ANDROID}:
        raise AgentDeploymentTriggerError(
            "Deployment worker trigger requires backend or android scope"
        )

    command = [
        str(SUDO_BINARY),
        "-n",
        str(TRIGGER_HELPER),
        normalized_scope.value,
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
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=TRIGGER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentDeploymentTriggerError(
            f"Unable to request the {normalized_scope.value} deployment worker"
        ) from exc

    if result.returncode != 0:
        detail = " | ".join(
            line.strip()
            for line in result.stderr.splitlines()
            if line.strip()
        )
        suffix = f": {detail[-1000:]}" if detail else ""
        raise AgentDeploymentTriggerError(
            f"Unable to request the {normalized_scope.value} deployment worker{suffix}"
        )

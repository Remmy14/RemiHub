from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from backend.core.agent_state import (
    CardStatus,
    RepositoryScope,
    RunPhase,
    require_resolved_repository_scope,
)
from backend.core.agent_worker import (
    AgentTemporarilyBlockedError,
    AgentWorkerConfigurationError,
    ClaimedRun,
    ExecutionResult,
)


PLANNING_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "response_markdown": {
            "type": "string",
            "description": (
                "The repository-informed plan or response to show the user."
            ),
        },
        "ready_for_implementation": {
            "type": "boolean",
            "description": (
                "True only when the plan is sufficiently complete for the user "
                "to approve implementation."
            ),
        },
        "repository_scope": {
            "type": "string",
            "enum": ["backend", "android", "backend_and_android"],
            "description": (
                "The resolved repository scope for the card. Never return auto."
            ),
        },
    },
    "required": [
        "response_markdown",
        "ready_for_implementation",
        "repository_scope",
    ],
}


PLANNING_DEVELOPER_INSTRUCTIONS = """
You are the planning agent for RemiHub. Work only in the planning phase.
Inspect the current repository before proposing changes. In dual-repository
planning workspaces, inspect the `backend/` checkout and the `android/`
checkout when needed to classify and coordinate the work. Do not create, edit,
delete, rename, or format files. Do not run commands that change repository,
database, service, package, network, or operating-system state. Never access
production secrets or deployment credentials. RemiHub owns builds, migrations,
service restarts, signing, publication, releases, and deployment.

Produce a concrete, repository-informed plan with affected components, data or
API changes, model/client coordination, validation, rollback considerations,
and any questions that truly block implementation. Set ready_for_implementation
to false when user input is still required; otherwise set it to true. Set
repository_scope to exactly one of backend, android, or backend_and_android.
Follow the relevant repository AGENTS.md files.
""".strip()


@dataclass(frozen=True)
class PlanningRepositoryPaths:
    cwd: Path
    backend: Path
    android: Path | None = None

    @property
    def dual_repository(self) -> bool:
        return self.android is not None


class CodexThreadStore(Protocol):
    def persist_codex_thread_id(
        self,
        claim: ClaimedRun,
        *,
        thread_id: str,
    ) -> None: ...


@dataclass(frozen=True)
class CodexPlanningTurn:
    thread_id: str
    turn_id: str
    final_response: str
    duration_ms: int | None = None
    sdk_version: str | None = None
    usage: dict | None = None


class CodexPlanningGateway(Protocol):
    def run_turn(
        self,
        *,
        existing_thread_id: str | None,
        repository_path: Path,
        prompt: str,
        model: str | None,
        on_thread_created: Callable[[str], None],
    ) -> CodexPlanningTurn: ...


class CodexTemporaryFailure(RuntimeError):
    pass


class OpenAICodexPlanningGateway:
    """Thin adapter around the optional OpenAI Codex Python SDK."""

    def run_turn(
        self,
        *,
        existing_thread_id: str | None,
        repository_path: Path,
        prompt: str,
        model: str | None,
        on_thread_created: Callable[[str], None],
    ) -> CodexPlanningTurn:
        try:
            import openai_codex
            from openai_codex import ApprovalMode, Codex, Sandbox
        except ImportError as exc:
            raise AgentWorkerConfigurationError(
                "The codex planning executor requires requirements-agent.txt"
            ) from exc

        lifecycle = {
            "approval_mode": ApprovalMode.deny_all,
            "cwd": str(repository_path),
            "developer_instructions": PLANNING_DEVELOPER_INSTRUCTIONS,
            "sandbox": Sandbox.read_only,
        }
        if model:
            lifecycle["model"] = model

        try:
            with Codex() as codex:
                if existing_thread_id:
                    thread = codex.thread_resume(
                        existing_thread_id,
                        **lifecycle,
                    )
                else:
                    thread = codex.thread_start(
                        ephemeral=False,
                        **lifecycle,
                    )
                    on_thread_created(thread.id)

                turn_arguments = {
                    "approval_mode": ApprovalMode.deny_all,
                    "cwd": str(repository_path),
                    "output_schema": PLANNING_OUTPUT_SCHEMA,
                    "sandbox": Sandbox.read_only,
                }
                if model:
                    turn_arguments["model"] = model

                result = thread.run(prompt, **turn_arguments)
        except Exception as exc:
            if _is_temporary_sdk_error(openai_codex, exc):
                raise CodexTemporaryFailure(str(exc).strip() or type(exc).__name__) from exc
            raise

        final_response = (result.final_response or "").strip()
        if not final_response:
            raise RuntimeError("Codex completed without a final planning response")

        return CodexPlanningTurn(
            thread_id=thread.id,
            turn_id=result.id,
            final_response=final_response,
            duration_ms=result.duration_ms,
            sdk_version=getattr(openai_codex, "__version__", None),
            usage=(
                result.usage.model_dump(mode="json")
                if result.usage is not None
                else None
            ),
        )


class CodexPlanningExecutor:
    allowed_phases = frozenset({RunPhase.PLANNING})

    def __init__(
        self,
        *,
        repository_path: str | Path | None = None,
        planning_workspace_path: str | Path | None = None,
        backend_repository_path: str | Path | None = None,
        android_repository_path: str | Path | None = None,
        thread_store: CodexThreadStore,
        model: str | None = None,
        retry_after_seconds: int = 900,
        gateway: CodexPlanningGateway | None = None,
    ):
        if retry_after_seconds < 1:
            raise ValueError("retry_after_seconds must be at least 1")

        self.repository_paths = _resolve_planning_paths(
            repository_path=repository_path,
            planning_workspace_path=planning_workspace_path,
            backend_repository_path=backend_repository_path,
            android_repository_path=android_repository_path,
        )
        self.repository_path = self.repository_paths.cwd
        self.thread_store = thread_store
        self.model = model.strip() if model and model.strip() else None
        self.retry_after_seconds = retry_after_seconds
        self.gateway = gateway or OpenAICodexPlanningGateway()

    def execute(self, claim: ClaimedRun) -> ExecutionResult:
        if claim.phase is not RunPhase.PLANNING:
            raise AgentWorkerConfigurationError(
                "The codex planning executor cannot run implementation or deployment"
            )

        try:
            turn = self.gateway.run_turn(
                existing_thread_id=claim.codex_thread_id,
                repository_path=self.repository_paths.cwd,
                prompt=_planning_prompt(claim),
                model=self.model,
                on_thread_created=lambda thread_id: (
                    self.thread_store.persist_codex_thread_id(
                        claim,
                        thread_id=thread_id,
                    )
                ),
            )
        except CodexTemporaryFailure as exc:
            raise AgentTemporarilyBlockedError(
                f"Codex is temporarily unavailable: {exc}",
                retry_after_seconds=self.retry_after_seconds,
            ) from exc

        response, ready, repository_scope = _parse_planning_response(
            turn.final_response
        )
        target_status = (
            CardStatus.AWAITING_IMPLEMENTATION_APPROVAL
            if ready
            else CardStatus.AWAITING_FEEDBACK
        )
        metadata = {
            "duration_ms": turn.duration_ms,
            "executor": "codex_planning",
            "model": self.model,
            "phase": claim.phase.value,
            "repository_scope": repository_scope.value,
            "sandbox": "read-only",
            "sdk_version": turn.sdk_version,
            "thread_id": turn.thread_id,
            "turn_id": turn.turn_id,
            "usage": turn.usage,
        }
        if self.repository_paths.dual_repository:
            metadata["planning_workspace"] = {
                "android_repository": str(self.repository_paths.android),
                "backend_repository": str(self.repository_paths.backend),
                "cwd": str(self.repository_paths.cwd),
                "mode": "dual-repository",
            }
        else:
            metadata["planning_workspace"] = {
                "backend_repository": str(self.repository_paths.backend),
                "cwd": str(self.repository_paths.cwd),
                "mode": "single-backend",
            }
        return ExecutionResult(
            message=response,
            card_status=target_status,
            repository_scope=repository_scope,
            metadata=metadata,
        )


def _latest_user_message(claim: ClaimedRun) -> str:
    for message in reversed(claim.messages):
        if message.get("author_type") == "user":
            content = str(message.get("content") or "").strip()
            if content:
                return content
    return claim.description.strip()


def _planning_prompt(claim: ClaimedRun) -> str:
    request = _latest_user_message(claim)
    if claim.codex_thread_id:
        introduction = "Continue the existing planning discussion."
    else:
        introduction = (
            f"Begin planning card {claim.title!r}. The original request is: "
            f"{claim.description.strip()}"
        )

    return f"""
{introduction}

Current user message:
{request}

Inspect the repository or repositories as needed, but remain read-only. If a
dual-repository planning workspace is configured, the backend checkout is
labeled `backend/` and the Android checkout is labeled `android/`. Explicitly
evaluate backend API/model changes, Android client changes, and coordination
between them before choosing repository_scope. Respond using the required
structured planning schema. This is card revision {claim.card_revision} and
run {claim.id}.
""".strip()


def _parse_planning_response(value: str) -> tuple[str, bool, RepositoryScope]:
    payload_text = value.strip()
    if payload_text.startswith("```json") and payload_text.endswith("```"):
        payload_text = payload_text[7:-3].strip()
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Codex returned invalid structured planning output") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("Codex planning output must be a JSON object")
    response = payload.get("response_markdown")
    ready = payload.get("ready_for_implementation")
    repository_scope_value = payload.get("repository_scope")
    if not isinstance(response, str) or not response.strip():
        raise RuntimeError("Codex planning output is missing response_markdown")
    if not isinstance(ready, bool):
        raise RuntimeError(
            "Codex planning output is missing ready_for_implementation"
        )
    if not isinstance(repository_scope_value, str):
        raise RuntimeError("Codex planning output is missing repository_scope")
    try:
        repository_scope = require_resolved_repository_scope(
            repository_scope_value
        )
    except ValueError as exc:
        raise RuntimeError(
            "Codex planning output has invalid repository_scope"
        ) from exc
    normalized_response = response.strip()
    if len(normalized_response) > 20000:
        raise RuntimeError("Codex planning response exceeds 20000 characters")
    return normalized_response, ready, repository_scope


def _resolve_planning_paths(
    *,
    repository_path: str | Path | None,
    planning_workspace_path: str | Path | None,
    backend_repository_path: str | Path | None,
    android_repository_path: str | Path | None,
) -> PlanningRepositoryPaths:
    if planning_workspace_path is None:
        if backend_repository_path is not None or android_repository_path is not None:
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_PLANNING_WORKSPACE is required when labeled "
                "planning repositories are configured"
            )
        if repository_path is None:
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_REPOSITORY is required for codex-planning"
            )
        resolved_repository = _required_git_checkout(
            repository_path,
            field="REMIHUB_AGENT_REPOSITORY",
        )
        return PlanningRepositoryPaths(
            cwd=resolved_repository,
            backend=resolved_repository,
        )

    if repository_path is not None:
        raise AgentWorkerConfigurationError(
            "Configure either REMIHUB_AGENT_REPOSITORY or "
            "REMIHUB_AGENT_PLANNING_WORKSPACE, not both"
        )
    if backend_repository_path is None:
        raise AgentWorkerConfigurationError(
            "REMIHUB_AGENT_BACKEND_REPOSITORY is required for dual-repository "
            "planning"
        )
    if android_repository_path is None:
        raise AgentWorkerConfigurationError(
            "REMIHUB_AGENT_ANDROID_REPOSITORY is required for dual-repository "
            "planning"
        )

    workspace = _required_directory(
        planning_workspace_path,
        field="REMIHUB_AGENT_PLANNING_WORKSPACE",
    )
    backend = _required_git_checkout(
        backend_repository_path,
        field="REMIHUB_AGENT_BACKEND_REPOSITORY",
    )
    android = _required_git_checkout(
        android_repository_path,
        field="REMIHUB_AGENT_ANDROID_REPOSITORY",
    )
    if backend != (workspace / "backend").resolve():
        raise AgentWorkerConfigurationError(
            "REMIHUB_AGENT_BACKEND_REPOSITORY must be the backend child of "
            "REMIHUB_AGENT_PLANNING_WORKSPACE"
        )
    if android != (workspace / "android").resolve():
        raise AgentWorkerConfigurationError(
            "REMIHUB_AGENT_ANDROID_REPOSITORY must be the android child of "
            "REMIHUB_AGENT_PLANNING_WORKSPACE"
        )
    return PlanningRepositoryPaths(cwd=workspace, backend=backend, android=android)


def _required_directory(value: str | Path, *, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise AgentWorkerConfigurationError(f"{field} must be an absolute path")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise AgentWorkerConfigurationError(
            f"{field} does not exist: {resolved}"
        )
    return resolved


def _required_git_checkout(value: str | Path, *, field: str) -> Path:
    resolved = _required_directory(value, field=field)
    if not (resolved / ".git").exists():
        raise AgentWorkerConfigurationError(
            f"{field} is not a Git checkout: {resolved}"
        )
    return resolved


def _is_temporary_sdk_error(sdk, exc: Exception) -> bool:
    checker = getattr(sdk, "is_retryable_error", None)
    if checker is not None and checker(exc):
        return True

    message = str(exc).lower()
    temporary_markers = (
        "rate limit",
        "usage limit",
        "server overloaded",
        "server_overloaded",
        "temporarily unavailable",
        "too many requests",
        "retry limit",
    )
    return any(marker in message for marker in temporary_markers)

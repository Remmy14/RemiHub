from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from backend.core.agent_state import CardStatus, RunPhase
from backend.core.agent_worker import (
    AgentTemporarilyBlockedError,
    AgentWorkerConfigurationError,
    ClaimedRun,
    ExecutionResult,
)
from backend.core.agent_workspace import (
    GitImplementationWorkspaceManager,
    ImplementationWorkspace,
    WorkspaceSnapshot,
)


logger = logging.getLogger("remihub.agent_worker")


IMPLEMENTATION_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "response_markdown": {
            "type": "string",
            "description": (
                "A concise implementation summary, review notes, and any "
                "limitations to show the user."
            ),
        },
        "tests": {
            "type": "array",
            "description": "Validation commands actually attempted during this turn.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "command": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["passed", "failed", "not_run"],
                    },
                    "details": {"type": "string"},
                },
                "required": ["command", "status", "details"],
            },
        },
    },
    "required": ["response_markdown", "tests"],
}


IMPLEMENTATION_DEVELOPER_INSTRUCTIONS = """
You are the implementation agent for RemiHub. Work only inside the assigned
implementation worktree and only on the approved card scope. Inspect the
existing implementation and the persistent planning discussion before editing.
Follow the repository's AGENTS.md and preserve unrelated changes.

You may create, edit, rename, and delete files inside the assigned worktree.
Do not access or modify files outside it. Do not change Git metadata, create
commits, fetch, pull, push, merge, rebase, or contact any remote. Do not use
network access or install packages. Never access production configuration,
database credentials, Firebase credentials, Codex state, SSH material, Android
signing material, or deployment credentials.

You may write migration files when the approved change requires them, but never
apply a migration or connect to a production database. Do not build or sign an
Android release, publish artifacts, restart services, deploy, or perform
rollback. RemiHub owns those operations after separate approval.

Run focused validation inside the sandbox when the existing environment permits
it. Never claim a command passed unless you actually ran it successfully. End
with the required structured response containing a concise review summary and
every validation command attempted or explicitly not run.
""".strip()


class ImplementationWorkspaceStore(Protocol):
    def persist_implementation_workspace(
        self,
        claim: ClaimedRun,
        *,
        feature_branch: str,
        worktree_path: str,
    ) -> None: ...


@dataclass(frozen=True)
class CodexImplementationTurn:
    thread_id: str
    turn_id: str
    final_response: str
    duration_ms: int | None = None
    sdk_version: str | None = None
    usage: dict | None = None


TurnInterrupt = Callable[[], object]
TurnControlCallback = Callable[[TurnInterrupt | None], None]


class CodexImplementationGateway(Protocol):
    def run_turn(
        self,
        *,
        thread_id: str,
        repository_path: Path,
        prompt: str,
        model: str | None,
        on_turn_control: TurnControlCallback,
    ) -> CodexImplementationTurn: ...


class CodexImplementationTemporaryFailure(RuntimeError):
    pass


class OpenAICodexImplementationGateway:
    """Workspace-write adapter around the optional OpenAI Codex Python SDK."""

    def __init__(self, *, codex_bin: str):
        normalized = codex_bin.strip()
        if not normalized:
            raise AgentWorkerConfigurationError("codex_bin must not be blank")
        path = Path(normalized).expanduser()
        if not path.is_absolute():
            raise AgentWorkerConfigurationError("codex_bin must be absolute")
        if not path.is_file():
            raise AgentWorkerConfigurationError(
                f"Codex sandbox wrapper does not exist: {path}"
            )
        if not os.access(path, os.X_OK):
            raise AgentWorkerConfigurationError(
                f"Codex sandbox wrapper is not executable: {path}"
            )
        self.codex_bin = path.resolve()

    def run_turn(
        self,
        *,
        thread_id: str,
        repository_path: Path,
        prompt: str,
        model: str | None,
        on_turn_control: TurnControlCallback,
    ) -> CodexImplementationTurn:
        try:
            import openai_codex
            from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
        except ImportError as exc:
            raise AgentWorkerConfigurationError(
                "The Codex implementation executor requires requirements-agent.txt"
            ) from exc

        lifecycle = {
            "approval_mode": ApprovalMode.deny_all,
            "cwd": str(repository_path),
            "developer_instructions": IMPLEMENTATION_DEVELOPER_INSTRUCTIONS,
            "sandbox": Sandbox.workspace_write,
        }
        if model:
            lifecycle["model"] = model

        try:
            config = CodexConfig(
                codex_bin=str(self.codex_bin),
                cwd=str(repository_path),
            )
            with Codex(config) as codex:
                thread = codex.thread_resume(thread_id, **lifecycle)
                turn_arguments = {
                    "approval_mode": ApprovalMode.deny_all,
                    "cwd": str(repository_path),
                    "output_schema": IMPLEMENTATION_OUTPUT_SCHEMA,
                    "sandbox": Sandbox.workspace_write,
                }
                if model:
                    turn_arguments["model"] = model

                handle = thread.turn(prompt, **turn_arguments)
                on_turn_control(handle.interrupt)
                try:
                    result = handle.run()
                finally:
                    on_turn_control(None)
        except Exception as exc:
            if _is_temporary_sdk_error(openai_codex, exc):
                raise CodexImplementationTemporaryFailure(
                    str(exc).strip() or type(exc).__name__
                ) from exc
            raise

        final_response = (result.final_response or "").strip()
        if not final_response:
            raise RuntimeError(
                "Codex completed without a final implementation response"
            )

        return CodexImplementationTurn(
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


class CodexImplementationExecutor:
    allowed_phases = frozenset({RunPhase.IMPLEMENTATION})

    def __init__(
        self,
        *,
        workspace_manager: GitImplementationWorkspaceManager,
        workspace_store: ImplementationWorkspaceStore,
        codex_bin: str | None = None,
        model: str | None = None,
        retry_after_seconds: int = 900,
        gateway: CodexImplementationGateway | None = None,
    ):
        if retry_after_seconds < 1:
            raise ValueError("retry_after_seconds must be at least 1")
        self.workspace_manager = workspace_manager
        self.workspace_store = workspace_store
        self.model = model.strip() if model and model.strip() else None
        self.retry_after_seconds = retry_after_seconds
        if gateway is None:
            if codex_bin is None:
                raise AgentWorkerConfigurationError(
                    "The implementation executor requires a Codex sandbox wrapper"
                )
            gateway = OpenAICodexImplementationGateway(codex_bin=codex_bin)
        self.gateway = gateway
        self._turn_lock = threading.Lock()
        self._active_run_id: str | None = None
        self._active_interrupt: TurnInterrupt | None = None

    def execute(self, claim: ClaimedRun) -> ExecutionResult:
        if claim.phase is not RunPhase.IMPLEMENTATION:
            raise AgentWorkerConfigurationError(
                "The Codex implementation executor cannot run planning or deployment"
            )
        if not claim.codex_thread_id:
            raise AgentWorkerConfigurationError(
                "Implementation requires the persistent planning Codex thread"
            )

        with self.workspace_manager.locked_workspace(
            claim,
            persist_workspace=lambda feature_branch, worktree_path: (
                self.workspace_store.persist_implementation_workspace(
                    claim,
                    feature_branch=feature_branch,
                    worktree_path=worktree_path,
                )
            ),
        ) as workspace:
            try:
                turn = self.gateway.run_turn(
                    thread_id=claim.codex_thread_id,
                    repository_path=workspace.path,
                    prompt=_implementation_prompt(claim, workspace),
                    model=self.model,
                    on_turn_control=lambda interrupt: self._set_turn_control(
                        claim.id,
                        interrupt,
                    ),
                )
            except CodexImplementationTemporaryFailure as exc:
                raise AgentTemporarilyBlockedError(
                    f"Codex is temporarily unavailable: {exc}",
                    retry_after_seconds=self.retry_after_seconds,
                ) from exc
            finally:
                self._set_turn_control(claim.id, None)

            response, tests = _parse_implementation_response(turn.final_response)
            snapshot = self.workspace_manager.capture_snapshot(claim, workspace)

        return ExecutionResult(
            message=_completion_message(response, snapshot),
            card_status=CardStatus.REVIEW_READY,
            metadata={
                "duration_ms": turn.duration_ms,
                "executor": "codex_implementation",
                "model": self.model,
                "phase": claim.phase.value,
                "approval_mode": "deny-all",
                "sandbox": "workspace-write",
                "sdk_version": turn.sdk_version,
                "thread_id": turn.thread_id,
                "turn_id": turn.turn_id,
                "usage": turn.usage,
                "tests": tests,
                "workspace": {
                    "artifact_patch": str(snapshot.patch_path),
                    "base_branch": workspace.base_branch,
                    "base_commit": workspace.base_commit,
                    "branch": snapshot.branch,
                    "changed_files": list(snapshot.changed_files),
                    "diff_stat": snapshot.diff_stat,
                    "head_commit": snapshot.head_commit,
                    "patch_size_bytes": snapshot.patch_size_bytes,
                    "status_porcelain": snapshot.status_porcelain,
                    "worktree_path": str(workspace.path),
                },
            },
        )

    def cancel(self, claim: ClaimedRun) -> None:
        with self._turn_lock:
            interrupt = (
                self._active_interrupt
                if self._active_run_id == claim.id
                else None
            )
        if interrupt is None:
            return
        try:
            interrupt()
        except Exception:
            logger.exception(
                "Unable to interrupt stale Codex implementation turn: run=%s",
                claim.id,
            )

    def _set_turn_control(
        self,
        run_id: str,
        interrupt: TurnInterrupt | None,
    ) -> None:
        with self._turn_lock:
            if interrupt is None:
                if self._active_run_id == run_id:
                    self._active_run_id = None
                    self._active_interrupt = None
                return
            self._active_run_id = run_id
            self._active_interrupt = interrupt


def _latest_user_message(claim: ClaimedRun) -> str:
    for message in reversed(claim.messages):
        if message.get("author_type") == "user":
            content = str(message.get("content") or "").strip()
            if content:
                return content
    return claim.description.strip()


def _implementation_prompt(
    claim: ClaimedRun,
    workspace: ImplementationWorkspace,
) -> str:
    request = _latest_user_message(claim)
    if claim.feature_branch:
        introduction = (
            "Continue implementation in the existing card worktree and address "
            "the latest review feedback."
        )
    else:
        introduction = (
            "Implementation has been explicitly approved for the existing plan. "
            "Implement that approved scope in the assigned worktree."
        )

    return f"""
{introduction}

Current approved request or review feedback:
{request}

Card title: {claim.title}
Card revision: {claim.card_revision}
Run: {claim.id}
Base branch: {workspace.base_branch}
Feature branch: {workspace.feature_branch}

Remain inside the assigned worktree. Do not commit or deploy. Run focused tests
inside the sandbox when possible, then respond using the required structured
implementation schema.
""".strip()


def _parse_implementation_response(value: str) -> tuple[str, list[dict]]:
    payload_text = value.strip()
    if payload_text.startswith("```json") and payload_text.endswith("```"):
        payload_text = payload_text[7:-3].strip()
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Codex returned invalid structured implementation output"
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError("Codex implementation output must be a JSON object")
    response = payload.get("response_markdown")
    tests = payload.get("tests")
    if not isinstance(response, str) or not response.strip():
        raise RuntimeError(
            "Codex implementation output is missing response_markdown"
        )
    normalized_response = response.strip()
    if len(normalized_response) > 16000:
        raise RuntimeError("Codex implementation response exceeds 16000 characters")
    if not isinstance(tests, list):
        raise RuntimeError("Codex implementation output is missing tests")
    if len(tests) > 50:
        raise RuntimeError("Codex implementation output contains too many tests")

    normalized_tests: list[dict] = []
    for index, test in enumerate(tests):
        if not isinstance(test, dict):
            raise RuntimeError(f"Codex test result {index} must be an object")
        command = test.get("command")
        status = test.get("status")
        details = test.get("details")
        if not isinstance(command, str) or not command.strip():
            raise RuntimeError(f"Codex test result {index} is missing command")
        if status not in {"passed", "failed", "not_run"}:
            raise RuntimeError(f"Codex test result {index} has an invalid status")
        if not isinstance(details, str):
            raise RuntimeError(f"Codex test result {index} is missing details")
        normalized_tests.append(
            {
                "command": command.strip()[:1000],
                "status": status,
                "details": details.strip()[:4000],
            }
        )

    return normalized_response, normalized_tests


def _completion_message(response: str, snapshot: WorkspaceSnapshot) -> str:
    if snapshot.changed_files:
        shown_files = list(snapshot.changed_files[:50])
        file_lines = "\n".join(f"- `{path}`" for path in shown_files)
        if len(snapshot.changed_files) > len(shown_files):
            file_lines += (
                f"\n- …and {len(snapshot.changed_files) - len(shown_files)} more"
            )
    else:
        file_lines = "- No changed files detected."

    appendix = f"""

## Worker-captured workspace state

- Feature branch: `{snapshot.branch}`
- Changed files: {len(snapshot.changed_files)}
- Review patch size: {snapshot.patch_size_bytes} bytes

{file_lines}
""".rstrip()
    message = response.rstrip() + appendix
    if len(message) > 20000:
        message = message[:19997].rstrip() + "..."
    return message


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

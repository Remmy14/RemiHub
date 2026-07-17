from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Protocol

from backend.core.agent_state import CardStatus, RunPhase


logger = logging.getLogger("remihub.agent_worker")


class AgentLeaseLostError(RuntimeError):
    pass


class AgentWorkerConfigurationError(RuntimeError):
    pass


class AgentTemporarilyBlockedError(RuntimeError):
    def __init__(self, reason: str, *, retry_after_seconds: int):
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("A temporary block requires a reason")
        if retry_after_seconds < 1:
            raise ValueError("retry_after_seconds must be at least 1")

        super().__init__(normalized_reason)
        self.reason = normalized_reason
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class DeploymentSource:
    approval_id: str
    implementation_run_id: str
    implementation_result_metadata: dict


@dataclass(frozen=True)
class ClaimedRun:
    id: str
    card_id: str
    phase: RunPhase
    card_status: CardStatus
    card_revision: int
    attempt_count: int
    lease_token: str
    worker_id: str
    title: str
    description: str
    base_branch: str = "main"
    feature_branch: str | None = None
    worktree_path: str | None = None
    codex_thread_id: str | None = None
    deployment_source: DeploymentSource | None = None
    messages: tuple[dict, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ExecutionResult:
    message: str
    card_status: CardStatus
    metadata: dict = field(default_factory=dict)


class AgentExecutor(Protocol):
    @property
    def allowed_phases(self) -> frozenset[RunPhase]: ...

    def execute(self, claim: ClaimedRun) -> ExecutionResult: ...


class AgentQueue(Protocol):
    def claim_next_run(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        allowed_phases: frozenset[RunPhase],
    ) -> ClaimedRun | None: ...

    def start_run(self, claim: ClaimedRun, *, lease_seconds: int) -> None: ...

    def heartbeat_run(self, claim: ClaimedRun, *, lease_seconds: int) -> None: ...

    def persist_codex_thread_id(
        self,
        claim: ClaimedRun,
        *,
        thread_id: str,
    ) -> None: ...

    def persist_implementation_workspace(
        self,
        claim: ClaimedRun,
        *,
        feature_branch: str,
        worktree_path: str,
    ) -> None: ...

    def complete_run(self, claim: ClaimedRun, result: ExecutionResult) -> None: ...

    def block_run(
        self,
        claim: ClaimedRun,
        *,
        reason: str,
        retry_after_seconds: int,
    ) -> None: ...

    def fail_run(self, claim: ClaimedRun, *, error_message: str) -> None: ...


class FakeAgentExecutor:
    """Deterministic executor for QA queue validation only."""

    allowed_phases = frozenset(RunPhase)

    def execute(self, claim: ClaimedRun) -> ExecutionResult:
        if claim.phase is RunPhase.PLANNING:
            return ExecutionResult(
                message=(
                    "Fake planning executor completed. No repository files were "
                    "read or modified."
                ),
                card_status=CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
                metadata={"executor": "fake", "phase": claim.phase.value},
            )

        if claim.phase is RunPhase.IMPLEMENTATION:
            return ExecutionResult(
                message=(
                    "Fake implementation executor completed. No repository files "
                    "were modified."
                ),
                card_status=CardStatus.REVIEW_READY,
                metadata={"executor": "fake", "phase": claim.phase.value},
            )

        return ExecutionResult(
            message=(
                "Fake deployment executor completed. No build, restart, or "
                "deployment was performed."
            ),
            card_status=CardStatus.COMPLETED,
            metadata={"executor": "fake", "phase": claim.phase.value},
        )


class AgentWorker:
    def __init__(
        self,
        *,
        queue: AgentQueue,
        executor: AgentExecutor,
        worker_id: str,
        lease_seconds: int,
        heartbeat_seconds: int,
        max_attempts: int,
    ):
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        if lease_seconds < 5:
            raise ValueError("lease_seconds must be at least 5")
        if heartbeat_seconds < 1:
            raise ValueError("heartbeat_seconds must be at least 1")
        if heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds must be less than lease_seconds")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        self.queue = queue
        self.executor = executor
        self.worker_id = worker_id.strip()
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.max_attempts = max_attempts

    def process_once(self) -> bool:
        claim = self.queue.claim_next_run(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            allowed_phases=self.executor.allowed_phases,
        )

        if claim is None:
            return False

        if claim.attempt_count > self.max_attempts:
            self.queue.fail_run(
                claim,
                error_message=(
                    f"Maximum worker attempts exceeded ({self.max_attempts})"
                ),
            )
            return True

        try:
            self.queue.start_run(claim, lease_seconds=self.lease_seconds)
        except AgentLeaseLostError:
            self._log_lease_lost(claim)
            return True

        try:
            result = self._execute_with_heartbeat(claim)
        except AgentLeaseLostError:
            self._log_lease_lost(claim)
            return True
        except AgentTemporarilyBlockedError as exc:
            try:
                self.queue.block_run(
                    claim,
                    reason=exc.reason,
                    retry_after_seconds=exc.retry_after_seconds,
                )
            except AgentLeaseLostError:
                self._log_lease_lost(claim)
            return True
        except Exception as exc:
            logger.exception("Agent run failed: run=%s", claim.id)
            try:
                self.queue.fail_run(
                    claim,
                    error_message=_safe_error_message(exc),
                )
            except AgentLeaseLostError:
                self._log_lease_lost(claim)
            return True

        try:
            self.queue.complete_run(claim, result)
        except AgentLeaseLostError:
            self._log_lease_lost(claim)

        return True

    def _execute_with_heartbeat(self, claim: ClaimedRun) -> ExecutionResult:
        stop_event = threading.Event()
        lease_lost = threading.Event()

        def heartbeat() -> None:
            while not stop_event.wait(self.heartbeat_seconds):
                try:
                    self.queue.heartbeat_run(
                        claim,
                        lease_seconds=self.lease_seconds,
                    )
                except AgentLeaseLostError:
                    lease_lost.set()
                    stop_event.set()
                    self._cancel_executor(claim)
                    return
                except Exception:
                    logger.exception(
                        "Agent heartbeat failed and will be retried: run=%s",
                        claim.id,
                    )

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"agent-heartbeat-{claim.id}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            result = self.executor.execute(claim)
        finally:
            stop_event.set()
            heartbeat_thread.join(timeout=self.heartbeat_seconds + 1)

        if lease_lost.is_set():
            raise AgentLeaseLostError(
                f"Lease lost while executor was running: {claim.id}"
            )

        return result

    def _cancel_executor(self, claim: ClaimedRun) -> None:
        cancel = getattr(self.executor, "cancel", None)
        if cancel is None:
            return
        try:
            cancel(claim)
        except Exception:
            logger.exception(
                "Agent executor cancellation failed: run=%s",
                claim.id,
            )

    @staticmethod
    def _log_lease_lost(claim: ClaimedRun) -> None:
        logger.warning(
            "Agent run lease was lost before completion: run=%s worker=%s",
            claim.id,
            claim.worker_id,
        )


def _safe_error_message(exc: Exception) -> str:
    detail = str(exc).strip()
    message = type(exc).__name__
    if detail:
        message = f"{message}: {detail}"
    return message[:10000]

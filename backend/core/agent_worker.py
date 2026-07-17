from __future__ import annotations

import logging
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
    messages: tuple[dict, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ExecutionResult:
    message: str
    card_status: CardStatus
    metadata: dict = field(default_factory=dict)


class AgentExecutor(Protocol):
    def execute(self, claim: ClaimedRun) -> ExecutionResult: ...


class AgentQueue(Protocol):
    def claim_next_run(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> ClaimedRun | None: ...

    def start_run(self, claim: ClaimedRun, *, lease_seconds: int) -> None: ...

    def heartbeat_run(self, claim: ClaimedRun, *, lease_seconds: int) -> None: ...

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
        max_attempts: int,
    ):
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        if lease_seconds < 5:
            raise ValueError("lease_seconds must be at least 5")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        self.queue = queue
        self.executor = executor
        self.worker_id = worker_id.strip()
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    def process_once(self) -> bool:
        claim = self.queue.claim_next_run(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
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
            result = self.executor.execute(claim)
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

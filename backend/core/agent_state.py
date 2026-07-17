from __future__ import annotations

from enum import Enum


class CardStatus(str, Enum):
    PLANNING_QUEUED = "planning_queued"
    PLANNING = "planning"
    AWAITING_FEEDBACK = "awaiting_feedback"
    AWAITING_IMPLEMENTATION_APPROVAL = "awaiting_implementation_approval"
    IMPLEMENTATION_QUEUED = "implementation_queued"
    IMPLEMENTING = "implementing"
    REVIEW_READY = "review_ready"
    DEPLOYMENT_QUEUED = "deployment_queued"
    DEPLOYING = "deploying"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class RunPhase(str, Enum):
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    DEPLOYMENT = "deployment"


class RunStatus(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class InvalidCardTransitionError(ValueError):
    pass


class InvalidRunCompletionError(ValueError):
    pass


TERMINAL_CARD_STATUSES = frozenset(
    {
        CardStatus.COMPLETED,
        CardStatus.CANCELLED,
        CardStatus.CLOSED,
    }
)


ALLOWED_CARD_TRANSITIONS: dict[CardStatus, frozenset[CardStatus]] = {
    CardStatus.PLANNING_QUEUED: frozenset(
        {
            CardStatus.PLANNING,
            CardStatus.FAILED,
            CardStatus.CANCELLED,
        }
    ),
    CardStatus.PLANNING: frozenset(
        {
            CardStatus.AWAITING_FEEDBACK,
            CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
            CardStatus.BLOCKED,
            CardStatus.FAILED,
            CardStatus.CANCELLED,
        }
    ),
    CardStatus.AWAITING_FEEDBACK: frozenset(
        {
            CardStatus.PLANNING_QUEUED,
            CardStatus.CANCELLED,
        }
    ),
    CardStatus.AWAITING_IMPLEMENTATION_APPROVAL: frozenset(
        {
            CardStatus.PLANNING_QUEUED,
            CardStatus.IMPLEMENTATION_QUEUED,
            CardStatus.CANCELLED,
        }
    ),
    CardStatus.IMPLEMENTATION_QUEUED: frozenset(
        {
            CardStatus.IMPLEMENTING,
            CardStatus.FAILED,
            CardStatus.CANCELLED,
        }
    ),
    CardStatus.IMPLEMENTING: frozenset(
        {
            CardStatus.REVIEW_READY,
            CardStatus.BLOCKED,
            CardStatus.FAILED,
            CardStatus.CANCELLED,
        }
    ),
    CardStatus.REVIEW_READY: frozenset(
        {
            CardStatus.IMPLEMENTATION_QUEUED,
            CardStatus.DEPLOYMENT_QUEUED,
            CardStatus.CANCELLED,
        }
    ),
    CardStatus.DEPLOYMENT_QUEUED: frozenset(
        {
            CardStatus.DEPLOYING,
            CardStatus.FAILED,
            CardStatus.CANCELLED,
        }
    ),
    CardStatus.DEPLOYING: frozenset(
        {
            CardStatus.BLOCKED,
            CardStatus.COMPLETED,
            CardStatus.FAILED,
        }
    ),
    CardStatus.BLOCKED: frozenset(
        {
            CardStatus.PLANNING,
            CardStatus.IMPLEMENTING,
            CardStatus.DEPLOYING,
            CardStatus.FAILED,
            CardStatus.CANCELLED,
        }
    ),
    CardStatus.COMPLETED: frozenset({CardStatus.CLOSED}),
    CardStatus.FAILED: frozenset(
        {
            CardStatus.PLANNING_QUEUED,
            CardStatus.IMPLEMENTATION_QUEUED,
            CardStatus.DEPLOYMENT_QUEUED,
            CardStatus.CANCELLED,
            CardStatus.CLOSED,
        }
    ),
    CardStatus.CANCELLED: frozenset({CardStatus.CLOSED}),
    CardStatus.CLOSED: frozenset(),
}


FOLLOW_UP_TARGETS: dict[CardStatus, tuple[CardStatus, RunPhase]] = {
    CardStatus.AWAITING_FEEDBACK: (
        CardStatus.PLANNING_QUEUED,
        RunPhase.PLANNING,
    ),
    CardStatus.AWAITING_IMPLEMENTATION_APPROVAL: (
        CardStatus.PLANNING_QUEUED,
        RunPhase.PLANNING,
    ),
    CardStatus.REVIEW_READY: (
        CardStatus.IMPLEMENTATION_QUEUED,
        RunPhase.IMPLEMENTATION,
    ),
}


QUEUED_CARD_STATUS_BY_PHASE: dict[RunPhase, CardStatus] = {
    RunPhase.PLANNING: CardStatus.PLANNING_QUEUED,
    RunPhase.IMPLEMENTATION: CardStatus.IMPLEMENTATION_QUEUED,
    RunPhase.DEPLOYMENT: CardStatus.DEPLOYMENT_QUEUED,
}


ACTIVE_CARD_STATUS_BY_PHASE: dict[RunPhase, CardStatus] = {
    RunPhase.PLANNING: CardStatus.PLANNING,
    RunPhase.IMPLEMENTATION: CardStatus.IMPLEMENTING,
    RunPhase.DEPLOYMENT: CardStatus.DEPLOYING,
}


COMPLETION_CARD_STATUSES_BY_PHASE: dict[RunPhase, frozenset[CardStatus]] = {
    RunPhase.PLANNING: frozenset(
        {
            CardStatus.AWAITING_FEEDBACK,
            CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
        }
    ),
    RunPhase.IMPLEMENTATION: frozenset({CardStatus.REVIEW_READY}),
    RunPhase.DEPLOYMENT: frozenset({CardStatus.COMPLETED}),
}


def coerce_card_status(value: CardStatus | str) -> CardStatus:
    if isinstance(value, CardStatus):
        return value

    try:
        return CardStatus(value)
    except ValueError as exc:
        raise InvalidCardTransitionError(f"Unknown card status: {value!r}") from exc


def require_card_transition(
    current: CardStatus | str,
    target: CardStatus | str,
) -> tuple[CardStatus, CardStatus]:
    current_status = coerce_card_status(current)
    target_status = coerce_card_status(target)

    if target_status not in ALLOWED_CARD_TRANSITIONS[current_status]:
        raise InvalidCardTransitionError(
            f"Card cannot transition from {current_status.value} "
            f"to {target_status.value}"
        )

    return current_status, target_status


def follow_up_target(
    current: CardStatus | str,
) -> tuple[CardStatus, RunPhase]:
    current_status = coerce_card_status(current)

    try:
        return FOLLOW_UP_TARGETS[current_status]
    except KeyError as exc:
        raise InvalidCardTransitionError(
            f"Card does not accept follow-up messages while {current_status.value}"
        ) from exc


def coerce_run_phase(value: RunPhase | str) -> RunPhase:
    if isinstance(value, RunPhase):
        return value

    try:
        return RunPhase(value)
    except ValueError as exc:
        raise InvalidRunCompletionError(f"Unknown run phase: {value!r}") from exc


def queued_card_status_for_phase(phase: RunPhase | str) -> CardStatus:
    return QUEUED_CARD_STATUS_BY_PHASE[coerce_run_phase(phase)]


def active_card_status_for_phase(phase: RunPhase | str) -> CardStatus:
    return ACTIVE_CARD_STATUS_BY_PHASE[coerce_run_phase(phase)]


def require_run_completion_status(
    phase: RunPhase | str,
    target: CardStatus | str,
) -> tuple[RunPhase, CardStatus]:
    run_phase = coerce_run_phase(phase)
    target_status = coerce_card_status(target)

    if target_status not in COMPLETION_CARD_STATUSES_BY_PHASE[run_phase]:
        raise InvalidRunCompletionError(
            f"A {run_phase.value} run cannot complete as {target_status.value}"
        )

    return run_phase, target_status

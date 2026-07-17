from __future__ import annotations

import json
from uuid import uuid4

from backend.core.agent_state import (
    CardStatus,
    RunPhase,
    RunStatus,
    active_card_status_for_phase,
    queued_card_status_for_phase,
    require_card_transition,
    require_run_completion_status,
)
from backend.core.agent_worker import (
    AgentLeaseLostError,
    ClaimedRun,
    DeploymentSource,
    ExecutionResult,
)
from backend.services.agent_service import (
    _insert_event,
    _insert_message,
    _row_to_dict,
    _rows_to_dicts,
    get_db_conn,
    put_db_conn,
)


class AgentQueueStateError(RuntimeError):
    pass


WORKER_DATABASE_IDENTITIES = {
    "qa": (
        "remihub_qa",
        "remihub_qa_agent_worker",
        "remihub_qa_agent_worker",
    ),
    "production": (
        "remihub",
        "remihub_agent_worker",
        "remihub_agent_worker",
    ),
}


def verify_worker_identity(environment: str) -> tuple[str, str, str]:
    normalized_environment = environment.strip().lower()
    try:
        expected = WORKER_DATABASE_IDENTITIES[normalized_environment]
    except KeyError as exc:
        raise ValueError("environment must be qa or production") from exc

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), session_user, current_user;")
            identity = cur.fetchone()

        if identity != expected:
            raise AgentQueueStateError(
                "Agent worker database identity mismatch: "
                f"expected {expected!r}, received {identity!r}"
            )

        return identity
    finally:
        conn.rollback()
        put_db_conn(conn)


def _lease_interval_sql() -> str:
    return "(%s * INTERVAL '1 second')"


def _validate_positive_seconds(value: int, *, field: str) -> int:
    if value < 1:
        raise ValueError(f"{field} must be at least 1")
    return value


def _deployment_source_from_row(row: dict) -> DeploymentSource | None:
    values = (
        row.get("deployment_approval_id"),
        row.get("implementation_run_id"),
        row.get("implementation_result_metadata"),
    )
    if not any(value is not None for value in values):
        return None
    if not all(value is not None for value in values):
        raise AgentQueueStateError(
            f"Deployment context is incomplete for run {row['id']}"
        )
    if not isinstance(row["implementation_result_metadata"], dict):
        raise AgentQueueStateError(
            f"Implementation result metadata is invalid for run {row['id']}"
        )
    return DeploymentSource(
        approval_id=row["deployment_approval_id"],
        implementation_run_id=row["implementation_run_id"],
        implementation_result_metadata=row["implementation_result_metadata"],
    )


def _claimed_run_from_row(row: dict, messages: list[dict]) -> ClaimedRun:
    return ClaimedRun(
        id=row["id"],
        card_id=row["card_id"],
        phase=RunPhase(row["phase"]),
        card_status=CardStatus(row["active_card_status"]),
        card_revision=row["card_revision"],
        attempt_count=row["attempt_count"],
        lease_token=row["lease_token"],
        worker_id=row["worker_id"],
        title=row["title"],
        description=row["description"],
        base_branch=row["base_branch"],
        feature_branch=row["feature_branch"],
        worktree_path=row["worktree_path"],
        codex_thread_id=row["codex_thread_id"],
        deployment_source=_deployment_source_from_row(row),
        messages=tuple(messages),
    )


def _validate_candidate(row: dict) -> tuple[RunPhase, CardStatus, CardStatus]:
    phase = RunPhase(row["phase"])
    current_card_status = CardStatus(row["card_status"])
    queued_card_status = queued_card_status_for_phase(phase)
    active_card_status = active_card_status_for_phase(phase)
    run_status = RunStatus(row["run_status"])

    if run_status is RunStatus.QUEUED:
        expected = queued_card_status
    elif run_status is RunStatus.BLOCKED:
        expected = CardStatus.BLOCKED
        if row["resume_status"] != queued_card_status.value:
            raise AgentQueueStateError(
                f"Blocked run {row['id']} has an invalid resume status"
            )
    else:
        expected = active_card_status

    if current_card_status is not expected:
        raise AgentQueueStateError(
            f"Run {row['id']} is {run_status.value} while card "
            f"{row['card_id']} is {current_card_status.value}; expected "
            f"{expected.value}"
        )

    if phase is RunPhase.DEPLOYMENT:
        _deployment_source_from_row(row)

    if current_card_status is not active_card_status:
        require_card_transition(current_card_status, active_card_status)

    return phase, current_card_status, active_card_status


def claim_next_run(
    *,
    worker_id: str,
    lease_seconds: int,
    allowed_phases: frozenset[RunPhase],
) -> ClaimedRun | None:
    normalized_worker_id = worker_id.strip()
    if not normalized_worker_id:
        raise ValueError("worker_id must not be blank")
    if len(normalized_worker_id) > 200:
        raise ValueError("worker_id must be at most 200 characters")
    lease_seconds = _validate_positive_seconds(
        lease_seconds,
        field="lease_seconds",
    )
    normalized_phases = sorted(
        {
            phase.value if isinstance(phase, RunPhase) else RunPhase(phase).value
            for phase in allowed_phases
        }
    )
    if not normalized_phases:
        raise ValueError("allowed_phases must not be empty")

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT runs.id,
                       runs.card_id,
                       runs.phase,
                       runs.status AS run_status,
                       runs.card_revision,
                       runs.attempt_count,
                       runs.worker_id AS previous_worker_id,
                       runs.lease_expires_at AS previous_lease_expires_at,
                       cards.status AS card_status,
                       cards.resume_status,
                       cards.title,
                       cards.description,
                       cards.base_branch,
                       cards.feature_branch,
                       cards.worktree_path,
                       cards.codex_thread_id,
                       deployment_approval.id AS deployment_approval_id,
                       implementation_run.id AS implementation_run_id,
                       implementation_run.result_metadata
                           AS implementation_result_metadata
                FROM agent.runs AS runs
                JOIN agent.cards AS cards
                  ON cards.id = runs.card_id
                LEFT JOIN LATERAL (
                    SELECT approvals.id
                    FROM agent.approvals AS approvals
                    WHERE approvals.card_id = runs.card_id
                      AND approvals.approval_type = 'deployment'
                      AND approvals.decision = 'approved'
                      AND approvals.card_revision = runs.card_revision
                    ORDER BY approvals.created_at DESC, approvals.id DESC
                    LIMIT 1
                ) AS deployment_approval
                  ON runs.phase = 'deployment'
                LEFT JOIN LATERAL (
                    SELECT prior_runs.id, prior_runs.result_metadata
                    FROM agent.runs AS prior_runs
                    WHERE prior_runs.card_id = runs.card_id
                      AND prior_runs.phase = 'implementation'
                      AND prior_runs.status = 'succeeded'
                      AND prior_runs.card_revision = runs.card_revision
                    ORDER BY prior_runs.created_at DESC, prior_runs.id DESC
                    LIMIT 1
                ) AS implementation_run
                  ON runs.phase = 'deployment'
                WHERE runs.phase = ANY(%s)
                  AND (
                    (
                        runs.status = 'queued'
                        AND runs.available_at <= CURRENT_TIMESTAMP
                    ) OR (
                        runs.status = 'blocked'
                        AND runs.available_at <= CURRENT_TIMESTAMP
                    ) OR (
                        runs.status IN ('claimed', 'running')
                        AND runs.lease_expires_at <= CURRENT_TIMESTAMP
                    )
                )
                ORDER BY
                    CASE
                        WHEN runs.status IN ('claimed', 'running') THEN 0
                        WHEN runs.status = 'blocked' THEN 1
                        ELSE 2
                    END,
                    runs.available_at,
                    runs.created_at,
                    runs.id
                FOR UPDATE OF runs, cards SKIP LOCKED
                LIMIT 1
                """,
                (normalized_phases,),
            )
            row = _row_to_dict(cur, cur.fetchone())

            if row is None:
                conn.rollback()
                return None

            phase, previous_card_status, active_card_status = _validate_candidate(row)
            previous_run_status = RunStatus(row["run_status"])
            lease_token = str(uuid4())
            attempt_count = row["attempt_count"] + (
                0 if previous_run_status is RunStatus.BLOCKED else 1
            )

            cur.execute(
                f"""
                UPDATE agent.runs
                SET status = %s,
                    worker_id = %s,
                    lease_token = %s,
                    lease_expires_at = CURRENT_TIMESTAMP + {_lease_interval_sql()},
                    attempt_count = %s,
                    last_heartbeat_at = CURRENT_TIMESTAMP,
                    available_at = CURRENT_TIMESTAMP,
                    blocked_reason = NULL,
                    started_at = NULL,
                    finished_at = NULL,
                    error_message = NULL,
                    result_message_id = NULL,
                    result_metadata = '{{}}'::jsonb
                WHERE id = %s
                """,
                (
                    RunStatus.CLAIMED.value,
                    normalized_worker_id,
                    lease_token,
                    lease_seconds,
                    attempt_count,
                    row["id"],
                ),
            )

            cur.execute(
                """
                UPDATE agent.cards
                SET status = %s,
                    resume_status = NULL,
                    blocked_reason = NULL,
                    blocked_until = NULL
                WHERE id = %s
                """,
                (active_card_status.value, row["card_id"]),
            )

            event_type = (
                "run.reclaimed"
                if previous_run_status in {RunStatus.CLAIMED, RunStatus.RUNNING}
                else "run.claimed"
            )
            _insert_event(
                cur,
                card_id=row["card_id"],
                event_type=event_type,
                actor_type="worker",
                actor_user_id=None,
                payload={
                    "attempt_count": attempt_count,
                    "from_card_status": previous_card_status.value,
                    "from_run_status": previous_run_status.value,
                    "run_id": row["id"],
                    "worker_id": normalized_worker_id,
                },
            )

            cur.execute(
                """
                SELECT id,
                       card_id,
                       author_type,
                       content,
                       created_by,
                       client_message_id,
                       created_at
                FROM agent.messages
                WHERE card_id = %s
                ORDER BY created_at, id
                """,
                (row["card_id"],),
            )
            messages = _rows_to_dicts(cur, cur.fetchall())

            claimed_row = {
                **row,
                "phase": phase.value,
                "active_card_status": active_card_status.value,
                "attempt_count": attempt_count,
                "lease_token": lease_token,
                "worker_id": normalized_worker_id,
            }

        conn.commit()
        return _claimed_run_from_row(claimed_row, messages)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _lock_owned_run(cur, claim: ClaimedRun, *, statuses: tuple[str, ...]) -> dict:
    cur.execute(
        """
        SELECT runs.id,
               runs.card_id,
               runs.phase,
               runs.status AS run_status,
               cards.status AS card_status
        FROM agent.runs AS runs
        JOIN agent.cards AS cards
          ON cards.id = runs.card_id
        WHERE runs.id = %s
          AND runs.card_id = %s
          AND runs.worker_id = %s
          AND runs.lease_token = %s
          AND runs.status = ANY(%s)
          AND runs.lease_expires_at > CURRENT_TIMESTAMP
        FOR UPDATE OF runs, cards
        """,
        (
            claim.id,
            claim.card_id,
            claim.worker_id,
            claim.lease_token,
            list(statuses),
        ),
    )
    row = _row_to_dict(cur, cur.fetchone())

    if row is None:
        raise AgentLeaseLostError(
            f"Lease lost for agent run {claim.id} owned by {claim.worker_id}"
        )

    if row["phase"] != claim.phase.value:
        raise AgentQueueStateError(f"Run phase changed for {claim.id}")

    return row


def start_run(claim: ClaimedRun, *, lease_seconds: int) -> None:
    lease_seconds = _validate_positive_seconds(
        lease_seconds,
        field="lease_seconds",
    )
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _lock_owned_run(cur, claim, statuses=(RunStatus.CLAIMED.value,))
            cur.execute(
                f"""
                UPDATE agent.runs
                SET status = %s,
                    started_at = CURRENT_TIMESTAMP,
                    last_heartbeat_at = CURRENT_TIMESTAMP,
                    lease_expires_at = CURRENT_TIMESTAMP + {_lease_interval_sql()}
                WHERE id = %s
                """,
                (RunStatus.RUNNING.value, lease_seconds, claim.id),
            )
            _insert_event(
                cur,
                card_id=claim.card_id,
                event_type="run.started",
                actor_type="worker",
                actor_user_id=None,
                payload={
                    "attempt_count": claim.attempt_count,
                    "run_id": claim.id,
                    "worker_id": claim.worker_id,
                },
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def heartbeat_run(claim: ClaimedRun, *, lease_seconds: int) -> None:
    lease_seconds = _validate_positive_seconds(
        lease_seconds,
        field="lease_seconds",
    )
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE agent.runs
                SET last_heartbeat_at = CURRENT_TIMESTAMP,
                    lease_expires_at = CURRENT_TIMESTAMP + {_lease_interval_sql()}
                WHERE id = %s
                  AND card_id = %s
                  AND worker_id = %s
                  AND lease_token = %s
                  AND status IN ('claimed', 'running')
                  AND lease_expires_at > CURRENT_TIMESTAMP
                """,
                (
                    lease_seconds,
                    claim.id,
                    claim.card_id,
                    claim.worker_id,
                    claim.lease_token,
                ),
            )

            if cur.rowcount != 1:
                raise AgentLeaseLostError(
                    f"Lease lost for agent run {claim.id} owned by {claim.worker_id}"
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def persist_codex_thread_id(claim: ClaimedRun, *, thread_id: str) -> None:
    normalized_thread_id = thread_id.strip()
    if not normalized_thread_id:
        raise ValueError("thread_id must not be blank")
    if len(normalized_thread_id) > 500:
        raise ValueError("thread_id must be at most 500 characters")

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _lock_owned_run(
                cur,
                claim,
                statuses=(RunStatus.RUNNING.value,),
            )
            cur.execute(
                """
                UPDATE agent.cards
                SET codex_thread_id = %s
                WHERE id = %s
                  AND (
                      codex_thread_id IS NULL
                      OR codex_thread_id = %s
                  )
                """,
                (
                    normalized_thread_id,
                    claim.card_id,
                    normalized_thread_id,
                ),
            )
            if cur.rowcount != 1:
                raise AgentQueueStateError(
                    f"Card {claim.card_id} already has a different Codex thread"
                )
            _insert_event(
                cur,
                card_id=claim.card_id,
                event_type="codex.thread_attached",
                actor_type="worker",
                actor_user_id=None,
                payload={
                    "run_id": claim.id,
                    "thread_id": normalized_thread_id,
                    "worker_id": claim.worker_id,
                },
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def persist_implementation_workspace(
    claim: ClaimedRun,
    *,
    feature_branch: str,
    worktree_path: str,
) -> None:
    if claim.phase is not RunPhase.IMPLEMENTATION:
        raise AgentQueueStateError(
            "Implementation workspace metadata requires an implementation run"
        )
    normalized_branch = feature_branch.strip()
    normalized_path = worktree_path.strip()
    if not normalized_branch:
        raise ValueError("feature_branch must not be blank")
    if len(normalized_branch) > 500:
        raise ValueError("feature_branch must be at most 500 characters")
    if not normalized_path:
        raise ValueError("worktree_path must not be blank")
    if len(normalized_path) > 2000:
        raise ValueError("worktree_path must be at most 2000 characters")

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _lock_owned_run(
                cur,
                claim,
                statuses=(RunStatus.RUNNING.value,),
            )
            cur.execute(
                """
                UPDATE agent.cards
                SET feature_branch = %s,
                    worktree_path = %s
                WHERE id = %s
                  AND (
                      (feature_branch IS NULL AND worktree_path IS NULL)
                      OR (feature_branch = %s AND worktree_path = %s)
                  )
                """,
                (
                    normalized_branch,
                    normalized_path,
                    claim.card_id,
                    normalized_branch,
                    normalized_path,
                ),
            )
            if cur.rowcount != 1:
                raise AgentQueueStateError(
                    f"Card {claim.card_id} already has a different implementation "
                    "workspace"
                )
            _insert_event(
                cur,
                card_id=claim.card_id,
                event_type="implementation.workspace_attached",
                actor_type="worker",
                actor_user_id=None,
                payload={
                    "feature_branch": normalized_branch,
                    "run_id": claim.id,
                    "worker_id": claim.worker_id,
                    "worktree_path": normalized_path,
                },
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def complete_run(claim: ClaimedRun, result: ExecutionResult) -> None:
    message = result.message.strip()
    if not message:
        raise ValueError("Agent completion message must not be blank")
    if len(message) > 20000:
        raise ValueError("Agent completion message must be at most 20000 characters")

    _, target_status = require_run_completion_status(
        claim.phase,
        result.card_status,
    )
    metadata = json.dumps(result.metadata or {}, sort_keys=True)

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            row = _lock_owned_run(
                cur,
                claim,
                statuses=(RunStatus.RUNNING.value,),
            )
            require_card_transition(row["card_status"], target_status)
            message_id = _insert_message(
                cur,
                card_id=claim.card_id,
                author_type="agent",
                content=message,
                created_by=None,
                client_message_id=None,
            )
            cur.execute(
                """
                UPDATE agent.runs
                SET status = %s,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    blocked_reason = NULL,
                    finished_at = CURRENT_TIMESTAMP,
                    error_message = NULL,
                    result_message_id = %s,
                    result_metadata = %s::jsonb
                WHERE id = %s
                """,
                (
                    RunStatus.SUCCEEDED.value,
                    message_id,
                    metadata,
                    claim.id,
                ),
            )
            cur.execute(
                """
                UPDATE agent.cards
                SET status = %s,
                    resume_status = NULL,
                    blocked_reason = NULL,
                    blocked_until = NULL
                WHERE id = %s
                """,
                (target_status.value, claim.card_id),
            )
            _insert_event(
                cur,
                card_id=claim.card_id,
                event_type="run.succeeded",
                actor_type="worker",
                actor_user_id=None,
                payload={
                    "attempt_count": claim.attempt_count,
                    "result_message_id": message_id,
                    "run_id": claim.id,
                    "to_card_status": target_status.value,
                    "worker_id": claim.worker_id,
                },
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def block_run(
    claim: ClaimedRun,
    *,
    reason: str,
    retry_after_seconds: int,
) -> None:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("A blocked run requires a reason")
    if len(normalized_reason) > 2000:
        raise ValueError("A blocked-run reason must be at most 2000 characters")
    retry_after_seconds = _validate_positive_seconds(
        retry_after_seconds,
        field="retry_after_seconds",
    )
    resume_status = queued_card_status_for_phase(claim.phase)

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            row = _lock_owned_run(
                cur,
                claim,
                statuses=(RunStatus.CLAIMED.value, RunStatus.RUNNING.value),
            )
            require_card_transition(row["card_status"], CardStatus.BLOCKED)
            cur.execute(
                f"""
                UPDATE agent.runs
                SET status = %s,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    available_at = CURRENT_TIMESTAMP + {_lease_interval_sql()},
                    blocked_reason = %s,
                    finished_at = NULL
                WHERE id = %s
                """,
                (
                    RunStatus.BLOCKED.value,
                    retry_after_seconds,
                    normalized_reason,
                    claim.id,
                ),
            )
            cur.execute(
                f"""
                UPDATE agent.cards
                SET status = %s,
                    resume_status = %s,
                    blocked_reason = %s,
                    blocked_until = CURRENT_TIMESTAMP + {_lease_interval_sql()}
                WHERE id = %s
                """,
                (
                    CardStatus.BLOCKED.value,
                    resume_status.value,
                    normalized_reason,
                    retry_after_seconds,
                    claim.card_id,
                ),
            )
            _insert_event(
                cur,
                card_id=claim.card_id,
                event_type="run.blocked",
                actor_type="worker",
                actor_user_id=None,
                payload={
                    "reason": normalized_reason,
                    "retry_after_seconds": retry_after_seconds,
                    "run_id": claim.id,
                    "worker_id": claim.worker_id,
                },
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def fail_run(claim: ClaimedRun, *, error_message: str) -> None:
    normalized_error = error_message.strip() or "Agent worker failed"
    normalized_error = normalized_error[:10000]

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            row = _lock_owned_run(
                cur,
                claim,
                statuses=(RunStatus.CLAIMED.value, RunStatus.RUNNING.value),
            )
            require_card_transition(row["card_status"], CardStatus.FAILED)
            _insert_message(
                cur,
                card_id=claim.card_id,
                author_type="system",
                content=f"Agent worker failed: {normalized_error}",
                created_by=None,
                client_message_id=None,
            )
            cur.execute(
                """
                UPDATE agent.runs
                SET status = %s,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    blocked_reason = NULL,
                    finished_at = CURRENT_TIMESTAMP,
                    error_message = %s,
                    result_message_id = NULL,
                    result_metadata = '{}'::jsonb
                WHERE id = %s
                """,
                (RunStatus.FAILED.value, normalized_error, claim.id),
            )
            cur.execute(
                """
                UPDATE agent.cards
                SET status = %s,
                    resume_status = NULL,
                    blocked_reason = NULL,
                    blocked_until = NULL
                WHERE id = %s
                """,
                (CardStatus.FAILED.value, claim.card_id),
            )
            _insert_event(
                cur,
                card_id=claim.card_id,
                event_type="run.failed",
                actor_type="worker",
                actor_user_id=None,
                payload={
                    "attempt_count": claim.attempt_count,
                    "error": normalized_error,
                    "run_id": claim.id,
                    "worker_id": claim.worker_id,
                },
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


class DatabaseAgentQueue:
    def __init__(self, *, environment: str):
        self.environment = environment

    def verify_identity(self) -> tuple[str, str, str]:
        return verify_worker_identity(self.environment)

    def claim_next_run(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        allowed_phases: frozenset[RunPhase],
    ) -> ClaimedRun | None:
        return claim_next_run(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            allowed_phases=allowed_phases,
        )

    def start_run(self, claim: ClaimedRun, *, lease_seconds: int) -> None:
        start_run(claim, lease_seconds=lease_seconds)

    def heartbeat_run(self, claim: ClaimedRun, *, lease_seconds: int) -> None:
        heartbeat_run(claim, lease_seconds=lease_seconds)

    def persist_codex_thread_id(
        self,
        claim: ClaimedRun,
        *,
        thread_id: str,
    ) -> None:
        persist_codex_thread_id(claim, thread_id=thread_id)

    def persist_implementation_workspace(
        self,
        claim: ClaimedRun,
        *,
        feature_branch: str,
        worktree_path: str,
    ) -> None:
        persist_implementation_workspace(
            claim,
            feature_branch=feature_branch,
            worktree_path=worktree_path,
        )

    def complete_run(self, claim: ClaimedRun, result: ExecutionResult) -> None:
        complete_run(claim, result)

    def block_run(
        self,
        claim: ClaimedRun,
        *,
        reason: str,
        retry_after_seconds: int,
    ) -> None:
        block_run(
            claim,
            reason=reason,
            retry_after_seconds=retry_after_seconds,
        )

    def fail_run(self, claim: ClaimedRun, *, error_message: str) -> None:
        fail_run(claim, error_message=error_message)

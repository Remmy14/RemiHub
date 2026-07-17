from __future__ import annotations

import json
from uuid import UUID, uuid4

from psycopg2 import errors

from backend.core.agent_state import (
    CardStatus,
    InvalidCardTransitionError,
    RunPhase,
    RunStatus,
    follow_up_target,
    require_card_transition,
)


CARD_COLUMNS = """
    id,
    title,
    description,
    status,
    revision,
    base_branch,
    feature_branch,
    worktree_path,
    codex_thread_id,
    resume_status,
    blocked_reason,
    blocked_until,
    created_by,
    closed_at,
    created_at,
    updated_at
"""


class AgentServiceError(RuntimeError):
    pass


class AgentCardNotFoundError(AgentServiceError):
    pass


class AgentConflictError(AgentServiceError):
    pass


class AgentStateConflictError(AgentConflictError):
    pass


def get_db_conn():
    # Keep router/model imports independent from database configuration. This
    # also lets OpenAPI and HTTP-boundary tests load the agent API without
    # opening a PostgreSQL connection.
    from backend.database.database import get_db_conn as acquire_connection

    return acquire_connection()


def put_db_conn(conn) -> None:
    from backend.database.database import put_db_conn as release_connection

    release_connection(conn)


def _serialize_value(value):
    if value is None:
        return None

    if isinstance(value, UUID):
        return str(value)

    if hasattr(value, "isoformat"):
        return value.isoformat()

    return value


def _rows_to_dicts(cur, rows) -> list[dict]:
    columns = [description[0] for description in cur.description]
    return [
        {
            column: _serialize_value(value)
            for column, value in zip(columns, row)
        }
        for row in rows
    ]


def _row_to_dict(cur, row) -> dict | None:
    if row is None:
        return None

    return _rows_to_dicts(cur, [row])[0]


def _required_text(value: str, *, field: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be blank")
    if len(normalized) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return normalized


def _optional_text(value: str | None, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None

    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return normalized or None


def _unique_violation_error(exc: errors.UniqueViolation) -> AgentConflictError:
    constraint = getattr(getattr(exc, "diag", None), "constraint_name", None)

    if constraint == "agent_one_open_card_uidx":
        return AgentConflictError("Another agent card is already open")
    if constraint == "agent_one_active_run_uidx":
        return AgentConflictError("Another agent run is already active")
    if constraint == "agent_messages_client_message_uidx":
        return AgentConflictError("This message has already been submitted")
    if constraint == "agent_approvals_approved_revision_uidx":
        return AgentConflictError("This card revision is already approved")

    return AgentConflictError("Agent data conflicts with an existing record")


def _require_transition(current: str, target: CardStatus) -> None:
    try:
        require_card_transition(current, target)
    except InvalidCardTransitionError as exc:
        raise AgentStateConflictError(str(exc)) from exc


def _locked_card(cur, card_id: str) -> dict:
    cur.execute(
        f"""
        SELECT {CARD_COLUMNS}
        FROM agent.cards
        WHERE id = %s
        FOR UPDATE
        """,
        (card_id,),
    )
    card = _row_to_dict(cur, cur.fetchone())

    if card is None:
        raise AgentCardNotFoundError(f"Agent card not found: {card_id}")

    return card


def _insert_message(
    cur,
    *,
    card_id: str,
    author_type: str,
    content: str,
    created_by: str | None,
    client_message_id: str | None,
) -> str:
    message_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO agent.messages (
            id,
            card_id,
            author_type,
            content,
            created_by,
            client_message_id
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            message_id,
            card_id,
            author_type,
            content,
            created_by,
            client_message_id,
        ),
    )
    return message_id


def _insert_run(
    cur,
    *,
    card_id: str,
    phase: RunPhase,
    card_revision: int,
    requested_by: str,
    input_message_id: str | None = None,
) -> str:
    run_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO agent.runs (
            id,
            card_id,
            phase,
            status,
            card_revision,
            input_message_id,
            requested_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id,
            card_id,
            phase.value,
            RunStatus.QUEUED.value,
            card_revision,
            input_message_id,
            requested_by,
        ),
    )
    return run_id


def _insert_approval(
    cur,
    *,
    card_id: str,
    approval_type: str,
    card_revision: int,
    decided_by: str,
    notes: str | None,
) -> str:
    approval_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO agent.approvals (
            id,
            card_id,
            approval_type,
            decision,
            card_revision,
            decided_by,
            notes
        )
        VALUES (%s, %s, %s, 'approved', %s, %s, %s)
        """,
        (
            approval_id,
            card_id,
            approval_type,
            card_revision,
            decided_by,
            notes,
        ),
    )
    return approval_id


def _deployment_implementation_result(
    cur,
    *,
    card_id: str,
    card_revision: int,
) -> dict:
    cur.execute(
        """
        SELECT id, result_metadata
        FROM agent.runs
        WHERE card_id = %s
          AND phase = 'implementation'
          AND status = 'succeeded'
          AND card_revision = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (card_id, card_revision),
    )
    result = _row_to_dict(cur, cur.fetchone())
    if result is None:
        raise AgentStateConflictError(
            "Deployment requires a successful implementation run for this revision"
        )
    metadata = result.get("result_metadata")
    workspace = metadata.get("workspace") if isinstance(metadata, dict) else None
    required_workspace_fields = {
        "artifact_patch",
        "base_branch",
        "base_commit",
        "branch",
        "changed_files",
        "head_commit",
        "patch_size_bytes",
        "status_porcelain",
        "worktree_path",
    }
    if (
        metadata.get("phase") != RunPhase.IMPLEMENTATION.value
        if isinstance(metadata, dict)
        else True
    ) or not isinstance(workspace, dict) or not required_workspace_fields.issubset(
        workspace
    ):
        raise AgentStateConflictError(
            "Implementation review evidence is incomplete for deployment"
        )
    return result


def _insert_event(
    cur,
    *,
    card_id: str,
    event_type: str,
    actor_type: str,
    actor_user_id: str | None,
    payload: dict | None = None,
) -> str:
    event_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO agent.events (
            id,
            card_id,
            event_type,
            actor_type,
            actor_user_id,
            payload
        )
        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            event_id,
            card_id,
            event_type,
            actor_type,
            actor_user_id,
            json.dumps(payload or {}, sort_keys=True),
        ),
    )
    return event_id


def _update_card_status(
    cur,
    *,
    card_id: str,
    status: CardStatus,
    revision: int | None = None,
    close: bool = False,
) -> None:
    assignments = ["status = %s"]
    values: list[object] = [status.value]

    if revision is not None:
        assignments.append("revision = %s")
        values.append(revision)

    if close:
        assignments.append("closed_at = CURRENT_TIMESTAMP")

    if status is not CardStatus.BLOCKED:
        assignments.extend(
            [
                "resume_status = NULL",
                "blocked_reason = NULL",
                "blocked_until = NULL",
            ]
        )

    values.append(card_id)
    cur.execute(
        f"""
        UPDATE agent.cards
        SET {', '.join(assignments)}
        WHERE id = %s
        """,
        tuple(values),
    )


def _card_detail(conn, card_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {CARD_COLUMNS}
            FROM agent.cards
            WHERE id = %s
            """,
            (card_id,),
        )
        card = _row_to_dict(cur, cur.fetchone())

        if card is None:
            raise AgentCardNotFoundError(f"Agent card not found: {card_id}")

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
            (card_id,),
        )
        card["messages"] = _rows_to_dicts(cur, cur.fetchall())

        cur.execute(
            """
            SELECT id,
                   card_id,
                   phase,
                   status,
                   card_revision,
                   input_message_id,
                   requested_by,
                   worker_id,
                   lease_expires_at,
                   attempt_count,
                   last_heartbeat_at,
                   available_at,
                   blocked_reason,
                   started_at,
                   finished_at,
                   error_message,
                   result_message_id,
                   result_metadata,
                   created_at,
                   updated_at
            FROM agent.runs
            WHERE card_id = %s
            ORDER BY created_at, id
            """,
            (card_id,),
        )
        card["runs"] = _rows_to_dicts(cur, cur.fetchall())

        cur.execute(
            """
            SELECT id,
                   card_id,
                   approval_type,
                   decision,
                   card_revision,
                   decided_by,
                   notes,
                   created_at
            FROM agent.approvals
            WHERE card_id = %s
            ORDER BY created_at, id
            """,
            (card_id,),
        )
        card["approvals"] = _rows_to_dicts(cur, cur.fetchall())

        cur.execute(
            """
            SELECT id,
                   card_id,
                   event_type,
                   actor_type,
                   actor_user_id,
                   payload,
                   created_at
            FROM agent.events
            WHERE card_id = %s
            ORDER BY created_at, id
            """,
            (card_id,),
        )
        card["events"] = _rows_to_dicts(cur, cur.fetchall())

    return card


def create_card(
    *,
    title: str,
    description: str,
    created_by: str,
    client_message_id: str | None = None,
) -> dict:
    title = _required_text(title, field="title", maximum=160)
    description = _required_text(
        description,
        field="description",
        maximum=20000,
    )
    card_id = str(uuid4())

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent.cards (
                    id,
                    title,
                    description,
                    status,
                    created_by
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    card_id,
                    title,
                    description,
                    CardStatus.PLANNING_QUEUED.value,
                    created_by,
                ),
            )
            message_id = _insert_message(
                cur,
                card_id=card_id,
                author_type="user",
                content=description,
                created_by=created_by,
                client_message_id=client_message_id,
            )
            run_id = _insert_run(
                cur,
                card_id=card_id,
                phase=RunPhase.PLANNING,
                card_revision=1,
                requested_by=created_by,
                input_message_id=message_id,
            )
            _insert_event(
                cur,
                card_id=card_id,
                event_type="card.created",
                actor_type="user",
                actor_user_id=created_by,
                payload={
                    "run_id": run_id,
                    "status": CardStatus.PLANNING_QUEUED.value,
                },
            )

        card = _card_detail(conn, card_id)
        conn.commit()
        return card
    except errors.UniqueViolation as exc:
        conn.rollback()
        raise _unique_violation_error(exc) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def list_cards(*, include_closed: bool = False) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            where_clause = "" if include_closed else "WHERE status <> 'closed'"
            cur.execute(
                f"""
                SELECT {CARD_COLUMNS}
                FROM agent.cards
                {where_clause}
                ORDER BY created_at DESC, id DESC
                """
            )
            return _rows_to_dicts(cur, cur.fetchall())
    finally:
        conn.rollback()
        put_db_conn(conn)


def get_card(card_id: str) -> dict:
    conn = get_db_conn()
    try:
        return _card_detail(conn, card_id)
    finally:
        conn.rollback()
        put_db_conn(conn)


def add_follow_up(
    *,
    card_id: str,
    content: str,
    created_by: str,
    client_message_id: str | None = None,
) -> dict:
    content = _required_text(content, field="content", maximum=20000)

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            card = _locked_card(cur, card_id)
            try:
                target_status, phase = follow_up_target(card["status"])
            except InvalidCardTransitionError as exc:
                raise AgentStateConflictError(str(exc)) from exc

            _require_transition(card["status"], target_status)
            revision = card["revision"] + 1
            message_id = _insert_message(
                cur,
                card_id=card_id,
                author_type="user",
                content=content,
                created_by=created_by,
                client_message_id=client_message_id,
            )
            run_id = _insert_run(
                cur,
                card_id=card_id,
                phase=phase,
                card_revision=revision,
                requested_by=created_by,
                input_message_id=message_id,
            )
            _update_card_status(
                cur,
                card_id=card_id,
                status=target_status,
                revision=revision,
            )
            _insert_event(
                cur,
                card_id=card_id,
                event_type="card.follow_up_submitted",
                actor_type="user",
                actor_user_id=created_by,
                payload={
                    "from_status": card["status"],
                    "phase": phase.value,
                    "revision": revision,
                    "run_id": run_id,
                    "to_status": target_status.value,
                },
            )

        result = _card_detail(conn, card_id)
        conn.commit()
        return result
    except errors.UniqueViolation as exc:
        conn.rollback()
        raise _unique_violation_error(exc) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def approve_implementation(
    *,
    card_id: str,
    approved_by: str,
    notes: str | None = None,
) -> dict:
    notes = _optional_text(notes, field="notes", maximum=2000)
    target_status = CardStatus.IMPLEMENTATION_QUEUED

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            card = _locked_card(cur, card_id)
            _require_transition(card["status"], target_status)
            approval_id = _insert_approval(
                cur,
                card_id=card_id,
                approval_type="implementation",
                card_revision=card["revision"],
                decided_by=approved_by,
                notes=notes,
            )
            run_id = _insert_run(
                cur,
                card_id=card_id,
                phase=RunPhase.IMPLEMENTATION,
                card_revision=card["revision"],
                requested_by=approved_by,
            )
            _update_card_status(
                cur,
                card_id=card_id,
                status=target_status,
            )
            _insert_event(
                cur,
                card_id=card_id,
                event_type="card.implementation_approved",
                actor_type="user",
                actor_user_id=approved_by,
                payload={
                    "approval_id": approval_id,
                    "revision": card["revision"],
                    "run_id": run_id,
                    "to_status": target_status.value,
                },
            )

        result = _card_detail(conn, card_id)
        conn.commit()
        return result
    except errors.UniqueViolation as exc:
        conn.rollback()
        raise _unique_violation_error(exc) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def approve_deployment(
    *,
    card_id: str,
    approved_by: str,
    notes: str | None = None,
) -> dict:
    notes = _optional_text(notes, field="notes", maximum=2000)
    target_status = CardStatus.DEPLOYMENT_QUEUED

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            card = _locked_card(cur, card_id)
            _require_transition(card["status"], target_status)
            implementation_result = _deployment_implementation_result(
                cur,
                card_id=card_id,
                card_revision=card["revision"],
            )
            approval_id = _insert_approval(
                cur,
                card_id=card_id,
                approval_type="deployment",
                card_revision=card["revision"],
                decided_by=approved_by,
                notes=notes,
            )
            run_id = _insert_run(
                cur,
                card_id=card_id,
                phase=RunPhase.DEPLOYMENT,
                card_revision=card["revision"],
                requested_by=approved_by,
            )
            _update_card_status(
                cur,
                card_id=card_id,
                status=target_status,
            )
            _insert_event(
                cur,
                card_id=card_id,
                event_type="card.deployment_approved",
                actor_type="user",
                actor_user_id=approved_by,
                payload={
                    "approval_id": approval_id,
                    "implementation_run_id": implementation_result["id"],
                    "revision": card["revision"],
                    "run_id": run_id,
                    "to_status": target_status.value,
                },
            )

        result = _card_detail(conn, card_id)
        conn.commit()
        return result
    except errors.UniqueViolation as exc:
        conn.rollback()
        raise _unique_violation_error(exc) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def cancel_card(
    *,
    card_id: str,
    cancelled_by: str,
    notes: str | None = None,
) -> dict:
    notes = _optional_text(notes, field="notes", maximum=2000)
    target_status = CardStatus.CANCELLED

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            card = _locked_card(cur, card_id)
            _require_transition(card["status"], target_status)
            cur.execute(
                """
                UPDATE agent.runs
                SET status = %s,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    blocked_reason = NULL,
                    finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
                WHERE card_id = %s
                  AND status IN ('queued', 'claimed', 'running', 'blocked')
                """,
                (RunStatus.CANCELLED.value, card_id),
            )
            cancelled_runs = cur.rowcount
            _update_card_status(
                cur,
                card_id=card_id,
                status=target_status,
            )
            _insert_event(
                cur,
                card_id=card_id,
                event_type="card.cancelled",
                actor_type="user",
                actor_user_id=cancelled_by,
                payload={
                    "cancelled_runs": cancelled_runs,
                    "from_status": card["status"],
                    "notes": notes,
                },
            )

        result = _card_detail(conn, card_id)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def close_card(
    *,
    card_id: str,
    closed_by: str,
    notes: str | None = None,
) -> dict:
    notes = _optional_text(notes, field="notes", maximum=2000)
    target_status = CardStatus.CLOSED

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            card = _locked_card(cur, card_id)
            _require_transition(card["status"], target_status)
            _update_card_status(
                cur,
                card_id=card_id,
                status=target_status,
                close=True,
            )
            _insert_event(
                cur,
                card_id=card_id,
                event_type="card.closed",
                actor_type="user",
                actor_user_id=closed_by,
                payload={
                    "from_status": card["status"],
                    "notes": notes,
                },
            )

        result = _card_detail(conn, card_id)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)

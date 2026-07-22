from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

from psycopg2 import errors

from backend.core.agent_state import (
    CardStatus,
    InvalidCardTransitionError,
    RepositoryScope,
    RunPhase,
    RunStatus,
    coerce_repository_scope,
    follow_up_target,
    require_backend_repository_scope,
    require_deployment_repository_scope,
    require_implementation_repository_scope,
    require_card_transition,
)


ANDROID_DEPLOYMENT_READINESS_PATH = Path(
    "/var/lib/remihub-agent/phase3b/android-signing-ready.json"
)
EXPECTED_ANDROID_PACKAGE_NAME = "com.alex.remihub"
EXPECTED_ANDROID_CERTIFICATE_SHA256 = (
    "029cc5d06bd10e1d07a56834dd45326c9762f6263c5835244bcaf4a6a6a6e03d"
)


CARD_COLUMNS = """
    id,
    title,
    description,
    status,
    repository_scope,
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


def _require_backend_scope(card: dict, *, action: str) -> None:
    try:
        require_backend_repository_scope(card["repository_scope"], action=action)
    except InvalidCardTransitionError as exc:
        raise AgentStateConflictError(str(exc)) from exc


def _require_deployment_scope(card: dict, *, action: str) -> RepositoryScope:
    try:
        return require_deployment_repository_scope(
            card["repository_scope"],
            action=action,
        )
    except InvalidCardTransitionError as exc:
        raise AgentStateConflictError(str(exc)) from exc


def _require_android_deployment_ready() -> None:
    path = ANDROID_DEPLOYMENT_READINESS_PATH
    if path.is_symlink() or not path.is_file():
        raise AgentStateConflictError(
            "Android deployment signing is not provisioned"
        )
    try:
        path_stat = path.stat()
        if path_stat.st_uid != 0 or path_stat.st_mode & 0o022:
            raise AgentStateConflictError(
                "Android deployment signing readiness permissions are unsafe"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except AgentStateConflictError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentStateConflictError(
            "Android deployment signing readiness is invalid"
        ) from exc
    expected = {
        "ready": True,
        "package_name": EXPECTED_ANDROID_PACKAGE_NAME,
        "certificate_sha256": EXPECTED_ANDROID_CERTIFICATE_SHA256,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise AgentStateConflictError(
                f"Android deployment signing readiness has unexpected {field}"
            )


def _require_implementation_scope(card: dict, *, action: str) -> None:
    try:
        require_implementation_repository_scope(
            card["repository_scope"],
            action=action,
        )
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
    repository_scope: RepositoryScope,
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
    if metadata.get("repository_scope") != repository_scope.value:
        raise AgentStateConflictError(
            "Implementation review evidence has the wrong repository scope"
        )
    if repository_scope is RepositoryScope.ANDROID:
        trusted_validation = metadata.get("trusted_validation")
        if not isinstance(trusted_validation, dict):
            raise AgentStateConflictError(
                "Android deployment requires trusted implementation validation"
            )
        required_validation = {
            "success": True,
            "gradle_offline": True,
            "network": "denied",
            "protected_build_files_unchanged": True,
        }
        for field, expected in required_validation.items():
            if trusted_validation.get(field) != expected:
                raise AgentStateConflictError(
                    f"Android implementation validation has unexpected {field}"
                )
        release_apk = trusted_validation.get("release_apk")
        if (
            not isinstance(release_apk, dict)
            or release_apk.get("signed") is not False
            or release_apk.get("package_name") != EXPECTED_ANDROID_PACKAGE_NAME
        ):
            raise AgentStateConflictError(
                "Android implementation validation release evidence is invalid"
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
                    "repository_scope": RepositoryScope.AUTO.value,
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
            _require_implementation_scope(
                card,
                action="Implementation approval",
            )
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
                    "repository_scope": coerce_repository_scope(
                        card["repository_scope"]
                    ).value,
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
            repository_scope = _require_deployment_scope(
                card,
                action="Deployment approval",
            )
            if repository_scope is RepositoryScope.ANDROID:
                _require_android_deployment_ready()
            implementation_result = _deployment_implementation_result(
                cur,
                card_id=card_id,
                card_revision=card["revision"],
                repository_scope=repository_scope,
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
                    "repository_scope": coerce_repository_scope(
                        card["repository_scope"]
                    ).value,
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

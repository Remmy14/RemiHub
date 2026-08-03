from __future__ import annotations

import json
import logging
from pathlib import Path
from uuid import UUID, uuid4

from psycopg2 import errors

from backend.core.agent_deployment import (
    GITHUB_SYNC_FAILED_NON_RETRYABLE,
    GITHUB_SYNC_FAILED_RETRYABLE,
    GITHUB_SYNC_LOCAL_INCOMPLETE,
    GITHUB_SYNC_PENDING,
    GITHUB_SYNC_RETRY_ACTION,
    GITHUB_SYNC_RUNNING,
    GITHUB_SYNC_SUCCEEDED,
    deployment_recovery_metadata,
)
from backend.core.agent_deployment_trigger import (
    AgentDeploymentTriggerError,
    trigger_deployment_worker,
)
from backend.core.agent_state import (
    ALLOWED_CARD_TRANSITIONS,
    CardStatus,
    InvalidCardTransitionError,
    RepositoryScope,
    RunPhase,
    RunStatus,
    coerce_repository_scope,
    follow_up_target,
    queued_card_status_for_phase,
    require_backend_repository_scope,
    require_deployment_repository_scope,
    require_implementation_repository_scope,
    require_card_transition,
)


ANDROID_DEPLOYMENT_READINESS_PATH = Path(
    "/var/lib/remihub-agent/phase3b/android-signing-ready.json"
)
EXPECTED_ANDROID_PACKAGE_NAME = "com.alex.remihub"
logger = logging.getLogger("remihub.agent_service")


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




def _latest_run_summary(run: dict | None) -> dict | None:
    if run is None:
        return None
    return {
        key: run.get(key)
        for key in (
            "id",
            "phase",
            "status",
            "card_revision",
            "attempt_count",
            "blocked_reason",
            "error_message",
            "created_at",
            "updated_at",
        )
    }


def _run_metadata(run: dict | None) -> dict:
    if run is None:
        return {}
    metadata = run.get("result_metadata")
    return metadata if isinstance(metadata, dict) else {}


def _deployment_recovery_from_run(run: dict | None) -> dict | None:
    if run is None or run.get("phase") != RunPhase.DEPLOYMENT.value:
        return None
    metadata = _run_metadata(run)
    recovery = metadata.get("deployment_recovery")
    github_sync = metadata.get("github_sync")
    candidate = metadata.get("candidate")

    candidate_commit = None
    production_deployed = False
    if isinstance(recovery, dict):
        candidate_commit = recovery.get("candidate_commit")
        production_deployed = bool(recovery.get("production_deployed"))
    if not isinstance(candidate_commit, str) and isinstance(candidate, dict):
        candidate_commit = candidate.get("candidate_commit")
    if not isinstance(candidate_commit, str) and isinstance(github_sync, dict):
        candidate_commit = github_sync.get("candidate_commit")

    if run.get("status") in {
        RunStatus.QUEUED.value,
        RunStatus.CLAIMED.value,
        RunStatus.RUNNING.value,
    }:
        if production_deployed:
            return deployment_recovery_metadata(
                github_sync_status=GITHUB_SYNC_RUNNING,
                retryable=False,
                blocker_code=None,
                last_error=None,
                candidate_commit=candidate_commit,
                deployment_run_id=run["id"],
                production_deployed=True,
            )
        return deployment_recovery_metadata(
            github_sync_status=GITHUB_SYNC_LOCAL_INCOMPLETE,
            retryable=False,
            blocker_code=None,
            last_error=None,
            candidate_commit=candidate_commit,
            deployment_run_id=run["id"],
            production_deployed=False,
        )

    if isinstance(recovery, dict):
        return recovery

    if isinstance(github_sync, dict):
        status = github_sync.get("status")
        if status == "verified":
            return deployment_recovery_metadata(
                github_sync_status=GITHUB_SYNC_SUCCEEDED,
                retryable=False,
                blocker_code=None,
                last_error=None,
                candidate_commit=candidate_commit,
                deployment_run_id=run["id"],
                production_deployed=True,
            )
        if status == "pending":
            retryable = bool(github_sync.get("retryable", True))
            has_failure = bool(github_sync.get("failure_reason"))
            return deployment_recovery_metadata(
                github_sync_status=(
                    GITHUB_SYNC_FAILED_RETRYABLE
                    if retryable and has_failure
                    else (
                        GITHUB_SYNC_PENDING
                        if retryable
                        else GITHUB_SYNC_FAILED_NON_RETRYABLE
                    )
                ),
                retryable=retryable,
                blocker_code=github_sync.get("blocker_code")
                or "github_sync_pending",
                last_error=github_sync.get("failure_reason"),
                candidate_commit=candidate_commit,
                deployment_run_id=run["id"],
                production_deployed=True,
            )

    return deployment_recovery_metadata(
        github_sync_status=GITHUB_SYNC_LOCAL_INCOMPLETE,
        retryable=False,
        blocker_code=None,
        last_error=run.get("error_message") or run.get("blocked_reason"),
        candidate_commit=candidate_commit,
        deployment_run_id=run["id"],
        production_deployed=False,
    )


def _allowed_actions(card: dict) -> list[str]:
    status = CardStatus(card["status"])
    scope = coerce_repository_scope(card["repository_scope"])
    actions: list[str] = []

    if status in {
        CardStatus.AWAITING_FEEDBACK,
        CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
        CardStatus.REVIEW_READY,
    }:
        actions.append("add_follow_up")

    if (
        status is CardStatus.AWAITING_IMPLEMENTATION_APPROVAL
        and scope in {RepositoryScope.BACKEND, RepositoryScope.ANDROID}
    ):
        actions.append("approve_implementation")

    if (
        status is CardStatus.REVIEW_READY
        and scope in {RepositoryScope.BACKEND, RepositoryScope.ANDROID}
    ):
        actions.append("approve_deployment")

    if status is CardStatus.FAILED:
        actions.append("retry")

    recovery = card.get("deployment_recovery")
    if (
        status is CardStatus.BLOCKED
        and isinstance(recovery, dict)
        and recovery.get("retryable") is True
        and recovery.get("github_sync_status")
        in {GITHUB_SYNC_PENDING, GITHUB_SYNC_FAILED_RETRYABLE}
    ):
        actions.append(GITHUB_SYNC_RETRY_ACTION)

    if CardStatus.CANCELLED in ALLOWED_CARD_TRANSITIONS[status]:
        actions.append("cancel")

    if CardStatus.CLOSED in ALLOWED_CARD_TRANSITIONS[status]:
        actions.append("close")

    return actions


def _decorate_card(card: dict, *, latest_run: dict | None = None) -> dict:
    result = dict(card)
    result["latest_run"] = _latest_run_summary(latest_run)
    result["deployment_recovery"] = _deployment_recovery_from_run(latest_run)
    result["allowed_actions"] = _allowed_actions(result)
    return result


def _request_deployment_worker(scope: RepositoryScope) -> None:
    try:
        trigger_deployment_worker(scope)
    except AgentDeploymentTriggerError:
        # Approval/retry has already committed. Keep the run queued so the
        # systemd fallback timer can claim it, while retaining an actionable
        # server-side error for operators.
        logger.exception(
            "Deployment worker immediate trigger failed; fallback timer will retry: scope=%s",
            scope.value,
        )



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


def _validate_deployment_implementation_result(
    result: dict | None,
    *,
    repository_scope: RepositoryScope,
) -> dict:
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
    return _validate_deployment_implementation_result(
        _row_to_dict(cur, cur.fetchone()),
        repository_scope=repository_scope,
    )


def _deployment_implementation_result_by_id(
    cur,
    *,
    card_id: str,
    card_revision: int,
    repository_scope: RepositoryScope,
    implementation_run_id: str,
) -> dict:
    cur.execute(
        """
        SELECT id, result_metadata
        FROM agent.runs
        WHERE id::text = %s
          AND card_id = %s
          AND phase = 'implementation'
          AND status = 'succeeded'
          AND card_revision = %s
        LIMIT 1
        """,
        (implementation_run_id, card_id, card_revision),
    )
    return _validate_deployment_implementation_result(
        _row_to_dict(cur, cur.fetchone()),
        repository_scope=repository_scope,
    )


def _deployment_retry_binding(
    cur,
    *,
    card_id: str,
    card_revision: int,
    failed_deployment_run_id: str,
    repository_scope: RepositoryScope,
) -> tuple[str, dict]:
    cur.execute(
        """
        SELECT payload
        FROM agent.events
        WHERE card_id = %s
          AND event_type IN (
              'card.deployment_approved',
              'card.deployment_retry_bound'
          )
          AND payload ->> 'run_id' = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (card_id, failed_deployment_run_id),
    )
    binding_row = _row_to_dict(cur, cur.fetchone())
    payload = binding_row.get("payload") if binding_row is not None else None
    if not isinstance(payload, dict):
        raise AgentStateConflictError(
            "Deployment retry is missing its exact approval binding"
        )
    approval_id = payload.get("approval_id")
    implementation_run_id = payload.get("implementation_run_id")
    if (
        not isinstance(approval_id, str)
        or not approval_id.strip()
        or not isinstance(implementation_run_id, str)
        or not implementation_run_id.strip()
    ):
        raise AgentStateConflictError(
            "Deployment retry has an invalid approval binding"
        )

    cur.execute(
        """
        SELECT 1
        FROM agent.approvals
        WHERE id::text = %s
          AND card_id = %s
          AND approval_type = 'deployment'
          AND decision = 'approved'
          AND card_revision = %s
        LIMIT 1
        """,
        (approval_id, card_id, card_revision),
    )
    if cur.fetchone() is None:
        raise AgentStateConflictError(
            "Deployment retry requires its existing exact approval"
        )

    implementation_result = _deployment_implementation_result_by_id(
        cur,
        card_id=card_id,
        card_revision=card_revision,
        repository_scope=repository_scope,
        implementation_run_id=implementation_run_id,
    )
    return approval_id, implementation_result


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

    latest_run = card["runs"][-1] if card["runs"] else None
    return _decorate_card(card, latest_run=latest_run)


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
            cards = _rows_to_dicts(cur, cur.fetchall())
            if not cards:
                return []

            card_ids = [card["id"] for card in cards]
            cur.execute(
                """
                SELECT DISTINCT ON (card_id)
                       id,
                       card_id,
                       phase,
                       status,
                       card_revision,
                       attempt_count,
                       blocked_reason,
                       error_message,
                       result_metadata,
                       created_at,
                       updated_at
                FROM agent.runs
                WHERE card_id = ANY(%s::uuid[])
                ORDER BY card_id, created_at DESC, id DESC
                """,
                (card_ids,),
            )
            latest_runs = {
                run["card_id"]: run
                for run in _rows_to_dicts(cur, cur.fetchall())
            }
            return [
                _decorate_card(card, latest_run=latest_runs.get(card["id"]))
                for card in cards
            ]
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
    except errors.UniqueViolation as exc:
        conn.rollback()
        raise _unique_violation_error(exc) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)

    _request_deployment_worker(repository_scope)
    return result


def retry_card(
    *,
    card_id: str,
    requested_by: str,
    notes: str | None = None,
) -> dict:
    notes = _optional_text(notes, field="notes", maximum=2000)

    deployment_trigger_scope: RepositoryScope | None = None
    deployment_approval_id: str | None = None
    deployment_implementation_run_id: str | None = None
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            card = _locked_card(cur, card_id)
            if CardStatus(card["status"]) is not CardStatus.FAILED:
                raise AgentStateConflictError(
                    "Retry is available only when the card status is failed"
                )

            cur.execute(
                """
                SELECT id, phase, card_revision, input_message_id
                FROM agent.runs
                WHERE card_id = %s
                  AND status = 'failed'
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT 1
                FOR UPDATE
                """,
                (card_id,),
            )
            failed_run = _row_to_dict(cur, cur.fetchone())
            if failed_run is None:
                raise AgentStateConflictError(
                    "The failed card has no failed run to retry"
                )

            phase = RunPhase(failed_run["phase"])
            target_status = queued_card_status_for_phase(phase)
            _require_transition(card["status"], target_status)

            if phase is RunPhase.IMPLEMENTATION:
                _require_implementation_scope(card, action="Implementation retry")
                cur.execute(
                    """
                    SELECT 1
                    FROM agent.approvals
                    WHERE card_id = %s
                      AND approval_type = 'implementation'
                      AND decision = 'approved'
                      AND card_revision = %s
                    LIMIT 1
                    """,
                    (card_id, failed_run["card_revision"]),
                )
                if cur.fetchone() is None:
                    raise AgentStateConflictError(
                        "Implementation retry requires an existing approval for this revision"
                    )

            if phase is RunPhase.DEPLOYMENT:
                repository_scope = _require_deployment_scope(
                    card, action="Deployment retry"
                )
                if repository_scope is RepositoryScope.ANDROID:
                    _require_android_deployment_ready()
                (
                    deployment_approval_id,
                    deployment_implementation_result,
                ) = _deployment_retry_binding(
                    cur,
                    card_id=card_id,
                    card_revision=failed_run["card_revision"],
                    failed_deployment_run_id=failed_run["id"],
                    repository_scope=repository_scope,
                )
                deployment_implementation_run_id = (
                    deployment_implementation_result["id"]
                )
                deployment_trigger_scope = repository_scope

            run_id = _insert_run(
                cur,
                card_id=card_id,
                phase=phase,
                card_revision=failed_run["card_revision"],
                requested_by=requested_by,
                input_message_id=(
                    failed_run["input_message_id"]
                    if phase is RunPhase.PLANNING
                    else None
                ),
            )
            _update_card_status(
                cur,
                card_id=card_id,
                status=target_status,
            )
            if phase is RunPhase.DEPLOYMENT:
                assert deployment_approval_id is not None
                assert deployment_implementation_run_id is not None
                _insert_event(
                    cur,
                    card_id=card_id,
                    event_type="card.deployment_retry_bound",
                    actor_type="user",
                    actor_user_id=requested_by,
                    payload={
                        "approval_id": deployment_approval_id,
                        "implementation_run_id": (
                            deployment_implementation_run_id
                        ),
                        "prior_deployment_run_id": failed_run["id"],
                        "revision": failed_run["card_revision"],
                        "run_id": run_id,
                    },
                )
            _insert_event(
                cur,
                card_id=card_id,
                event_type="card.retry_requested",
                actor_type="user",
                actor_user_id=requested_by,
                payload={
                    "failed_run_id": failed_run["id"],
                    "notes": notes,
                    "phase": phase.value,
                    "revision": failed_run["card_revision"],
                    "run_id": run_id,
                    "to_status": target_status.value,
                },
            )

        result = _card_detail(conn, card_id)
        conn.commit()
    except errors.UniqueViolation as exc:
        conn.rollback()
        raise _unique_violation_error(exc) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)

    if deployment_trigger_scope is not None:
        _request_deployment_worker(deployment_trigger_scope)
    return result


def retry_deployment_github_sync(
    *,
    card_id: str,
    deployment_run_id: str,
    requested_by: str,
    notes: str | None = None,
) -> dict:
    notes = _optional_text(notes, field="notes", maximum=2000)
    deployment_trigger_scope: RepositoryScope | None = None

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            card = _locked_card(cur, card_id)
            _require_backend_scope(card, action="GitHub synchronization retry")
            cur.execute(
                """
                SELECT id,
                       card_id,
                       phase,
                       status,
                       card_revision,
                       attempt_count,
                       blocked_reason,
                       error_message,
                       result_metadata,
                       created_at,
                       updated_at
                FROM agent.runs
                WHERE id::text = %s
                  AND card_id = %s
                  AND phase = 'deployment'
                LIMIT 1
                FOR UPDATE
                """,
                (deployment_run_id, card_id),
            )
            run = _row_to_dict(cur, cur.fetchone())
            if run is None:
                raise AgentStateConflictError(
                    "GitHub synchronization retry requires the exact deployment run"
                )
            recovery = _deployment_recovery_from_run(run)
            if (
                run["status"] == RunStatus.SUCCEEDED.value
                and isinstance(recovery, dict)
                and recovery.get("github_sync_status") == GITHUB_SYNC_SUCCEEDED
            ):
                _insert_event(
                    cur,
                    card_id=card_id,
                    event_type="card.github_sync_retry_noop",
                    actor_type="user",
                    actor_user_id=requested_by,
                    payload={
                        "deployment_run_id": deployment_run_id,
                        "notes": notes,
                    },
                )
                result = _card_detail(conn, card_id)
                conn.commit()
                return result

            if run["status"] != RunStatus.BLOCKED.value:
                raise AgentStateConflictError(
                    "GitHub synchronization retry requires a blocked deployment run"
                )
            if CardStatus(card["status"]) is not CardStatus.BLOCKED:
                raise AgentStateConflictError(
                    "GitHub synchronization retry requires a blocked card"
                )
            if run["card_revision"] != card["revision"]:
                raise AgentStateConflictError(
                    "GitHub synchronization retry requires the current card revision"
                )
            if (
                not isinstance(recovery, dict)
                or recovery.get("retryable") is not True
                or recovery.get("github_sync_status")
                not in {GITHUB_SYNC_PENDING, GITHUB_SYNC_FAILED_RETRYABLE}
                or recovery.get("production_deployed") is not True
            ):
                raise AgentStateConflictError(
                    "GitHub synchronization retry is not available for this deployment state"
                )

            metadata = _run_metadata(run)
            queued_recovery = dict(recovery)
            queued_recovery.update(
                {
                    "github_sync_status": GITHUB_SYNC_RUNNING,
                    "retryable": False,
                    "blocker_code": None,
                    "last_error": None,
                }
            )
            metadata["deployment_recovery"] = queued_recovery
            cur.execute(
                """
                UPDATE agent.runs
                SET available_at = CURRENT_TIMESTAMP,
                    blocked_reason = %s,
                    result_metadata = %s::jsonb
                WHERE id::text = %s
                  AND status = 'blocked'
                """,
                (
                    "Explicit GitHub synchronization retry requested",
                    json.dumps(metadata, sort_keys=True),
                    deployment_run_id,
                ),
            )
            if cur.rowcount != 1:
                raise AgentStateConflictError(
                    "GitHub synchronization retry lost the blocked deployment run"
                )
            cur.execute(
                """
                UPDATE agent.cards
                SET blocked_reason = %s,
                    blocked_until = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND status = 'blocked'
                """,
                (
                    "Explicit GitHub synchronization retry requested",
                    card_id,
                ),
            )
            if cur.rowcount != 1:
                raise AgentStateConflictError(
                    "GitHub synchronization retry lost the blocked card"
                )
            _insert_event(
                cur,
                card_id=card_id,
                event_type="card.github_sync_retry_requested",
                actor_type="user",
                actor_user_id=requested_by,
                payload={
                    "deployment_run_id": deployment_run_id,
                    "notes": notes,
                },
            )
            result = _card_detail(conn, card_id)
            deployment_trigger_scope = RepositoryScope.BACKEND
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)

    if deployment_trigger_scope is not None:
        _request_deployment_worker(deployment_trigger_scope)
    return result


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

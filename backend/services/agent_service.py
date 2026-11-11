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



def _safe_reimplementation_failure(card: dict, latest_run: dict | None) -> bool:
    """Only permit recovery from recognized pre-application QA failures."""
    if (
        card.get("status") != CardStatus.FAILED.value
        or card.get("repository_scope") != RepositoryScope.BACKEND.value
        or latest_run is None
        or latest_run.get("phase") != RunPhase.DEPLOYMENT.value
        or latest_run.get("status") != RunStatus.FAILED.value
        or latest_run.get("card_revision") != card.get("revision")
    ):
        return False

    metadata = _run_metadata(latest_run)

    # A completed QA handoff means this run may have entered
    # production. Never reopen that revision automatically.
    if "deployment_pipeline" in metadata:
        return False

    if metadata.get("environment") not in (None, "qa"):
        return False

    recovery = _deployment_recovery_from_run(latest_run)
    if isinstance(recovery, dict) and recovery.get("production_deployed"):
        return False

    error = latest_run.get("error_message") or ""
    if "rollback was incomplete" in error.lower():
        return False

    # Allow an exact pre-candidate baseline mismatch to return
    # to implementation. The checks above exclude production
    # handoffs and incomplete rollbacks.
    if error == (
        "AgentDeploymentError: Deployment target advanced before "
        "candidate creation"
    ):
        return True

    return any(marker in error for marker in (
        "Unable to apply the approved implementation patch",
        "Deployment candidate changed files do not match approval evidence",
    ))


def _retryable_failed_closed_review(card: dict, run: dict | None) -> bool:
    """Only a known fail-closed infrastructure outcome is eligible for re-execution.

    Candidate identity and contents are verified separately under the card lock.
    Legacy evidence must be constrained enough to recover the original v10 failure
    without opening arbitrary model-blocked or security-blocked reviews.
    """
    if (card.get("status") != CardStatus.BLOCKED.value
        or card.get("resume_status") != CardStatus.REVIEW_QUEUED.value
        or card.get("repository_scope") not in {"backend", "android"}
        or not isinstance(run, dict)
        or run.get("phase") != RunPhase.REVIEW.value
        or run.get("status") != RunStatus.SUCCEEDED.value
        or run.get("card_revision") != card.get("revision")
        or type(run.get("attempt_count")) is not int
        or not 1 <= run["attempt_count"] < 3):
        return False
    evidence = _run_metadata(run).get("independent_review")
    if not isinstance(evidence, dict) or evidence.get("verdict") != "blocked":
        return False
    if evidence.get("findings") != []:
        return False
    # A schema-invalid response can be returned by an otherwise successful
    # Codex turn. Its thread/turn IDs are evidence, not disqualifiers.
    if evidence.get("failure_code") == "review_output_invalid":
        return evidence.get("failure_type") == "ReviewOutputValidationError"
    # Only the narrowly documented legacy v10 failure had no turn identity.
    # Do not extend this heuristic to arbitrary older blocked reviews.
    if evidence.get("thread_id") is not None or evidence.get("turn_id") is not None:
        return False
    # Legacy reviewer: exact documented v10 error format, no successful turn.
    return (evidence.get("failure_code") is None
            and evidence.get("failure_type") == "ValueError"
            and evidence.get("summary") == "Review failed closed: ValueError")


def _allowed_actions(card: dict, *, latest_run: dict | None = None) -> list[str]:
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
        if _safe_reimplementation_failure(card, latest_run):
            actions.append("return_to_implementation")

    if _exhausted_review(card, latest_run):
        actions.append("request_revision")

    if _retryable_failed_closed_review(card, latest_run):
        actions.append("retry_review")

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
    result["allowed_actions"] = _allowed_actions(result, latest_run=latest_run)
    return result


def _request_deployment_worker(
    scope: RepositoryScope,
    *,
    deployment_environment: str | None = None,
) -> None:
    try:
        trigger_deployment_worker(
            scope,
            deployment_environment=deployment_environment,
        )
    except AgentDeploymentTriggerError:
        # Approval/retry has already committed. Keep the run queued so the
        # systemd fallback timer can claim it, while retaining an actionable
        # server-side error for operators.
        logger.exception(
            "Deployment worker immediate trigger failed; fallback timer will retry: "
            "scope=%s deployment_environment=%s",
            scope.value,
            deployment_environment,
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
            requested_by,
            review_cycle,
            review_attempt,
            result_metadata
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            run_id,
            card_id,
            phase.value,
            RunStatus.QUEUED.value,
            card_revision,
            input_message_id,
            requested_by,
            run_id if phase is RunPhase.IMPLEMENTATION else None,
            1 if phase is RunPhase.IMPLEMENTATION else None,
            json.dumps({"review_cycle": run_id, "review_attempt": 1}) if phase is RunPhase.IMPLEMENTATION else "{}",
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
    result = _validate_deployment_implementation_result(
        _row_to_dict(cur, cur.fetchone()), repository_scope=repository_scope)
    return _require_passing_independent_review(cur, result, card_id=card_id, card_revision=card_revision)


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
    result = _validate_deployment_implementation_result(
        _row_to_dict(cur, cur.fetchone()), repository_scope=repository_scope)
    return _require_passing_independent_review(cur, result, card_id=card_id, card_revision=card_revision)


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

    if repository_scope is RepositoryScope.BACKEND:
        _request_deployment_worker(repository_scope, deployment_environment="qa")
    else:
        _request_deployment_worker(repository_scope)
    return result



def return_to_implementation(
    *,
    card_id: str,
    requested_by: str,
    instructions: str,
) -> dict:
    """Create a fresh implementation revision after a safe QA failure."""
    instructions = _required_text(
        instructions, field="instructions", maximum=20000
    )

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            card = _locked_card(cur, card_id)

            _require_implementation_scope(
                card, action="Return to implementation"
            )

            cur.execute(
                """
                SELECT id, phase, status, card_revision, error_message,
                       result_metadata, created_at, updated_at
                FROM agent.runs
                WHERE card_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                FOR UPDATE
                """,
                (card_id,),
            )

            failed_run = _row_to_dict(cur, cur.fetchone())

            if not _safe_reimplementation_failure(card, failed_run):
                raise AgentStateConflictError(
                    "Return to implementation requires a verified "
                    "pre-application backend QA failure"
                )

            # Prevent reopening a revision that already passed QA.
            # A subsequent failure might belong to production.
            cur.execute(
                """
                SELECT 1
                FROM agent.runs
                WHERE card_id = %s
                  AND phase = 'deployment'
                  AND card_revision = %s
                  AND status = 'succeeded'
                LIMIT 1
                """,
                (card_id, card["revision"]),
            )

            if cur.fetchone() is not None:
                raise AgentStateConflictError(
                    "A deployment already succeeded for this revision; "
                    "manual recovery review is required"
                )

            target = CardStatus.IMPLEMENTATION_QUEUED
            _require_transition(card["status"], target)

            revision = card["revision"] + 1

            message_id = _insert_message(
                cur,
                card_id=card_id,
                author_type="user",
                content=instructions,
                created_by=requested_by,
                client_message_id=None,
            )

            run_id = _insert_run(
                cur,
                card_id=card_id,
                phase=RunPhase.IMPLEMENTATION,
                card_revision=revision,
                requested_by=requested_by,
                input_message_id=message_id,
            )

            _update_card_status(
                cur,
                card_id=card_id,
                status=target,
                revision=revision,
            )

            _insert_event(
                cur,
                card_id=card_id,
                event_type="card.deployment_returned_to_implementation",
                actor_type="user",
                actor_user_id=requested_by,
                payload={
                    "failed_deployment_run_id": failed_run["id"],
                    "old_revision": card["revision"],
                    "revision": revision,
                    "implementation_run_id": run_id,
                    "input_message_id": message_id,
                    "to_status": target.value,
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



# This narrowly defined recovery is NOT the ordinary review retry. It is for the
# documented backend-v10 finding that lacked Android model/parser source, and
# must not reopen reviews identifying implementation defects or other blockers.
_ANDROID_CLIENT_EVIDENCE_FINDING_PATH = (
    "Android client stats response model/parser (not supplied)"
)
_ANDROID_CLIENT_EVIDENCE_REPOSITORY = Path(
    "/opt/remihub-agent/planning/android"
)


def _retryable_android_client_evidence_review(card: dict, run: dict | None) -> bool:
    if (
        card.get("status") != CardStatus.BLOCKED.value
        or card.get("resume_status") != CardStatus.REVIEW_QUEUED.value
        or card.get("repository_scope") != RepositoryScope.BACKEND.value
        or not isinstance(run, dict)
        or run.get("phase") != RunPhase.REVIEW.value
        or run.get("status") != RunStatus.SUCCEEDED.value
        or run.get("card_revision") != card.get("revision")
        or type(run.get("attempt_count")) is not int
        or run["attempt_count"] != 2  # exactly one execution remains
        or run.get("review_attempt") != 1
    ):
        return False
    metadata = _run_metadata(run)
    evidence = metadata.get("independent_review")
    source = metadata.get("review_source")
    if not isinstance(evidence, dict) or not isinstance(source, dict):
        return False
    findings = evidence.get("findings")
    return (
        evidence.get("verdict") == "blocked"
        and evidence.get("failure_type") is None
        and evidence.get("review_attempt") == 1
        and evidence.get("card_revision") == card.get("revision")
        and evidence.get("review_cycle") == str(run.get("review_cycle"))
        and source.get("review_cycle") == evidence.get("review_cycle")
        and source.get("review_attempt") == 1
        and source.get("implementation_run_id") == evidence.get("implementation_run_id")
        and bool(evidence.get("thread_id") and evidence.get("turn_id"))
        and evidence.get("boundary") == "qualified-no-tools"
        and isinstance(findings, list) and len(findings) == 1
        and isinstance(findings[0], dict)
        and findings[0].get("kind") == "required"
        and findings[0].get("path") == _ANDROID_CLIENT_EVIDENCE_FINDING_PATH
        and "Android source and trusted compatibility-test evidence are absent"
            in findings[0].get("description", "")
        and "Supply the relevant Android response model and parsing code"
            in findings[0].get("correction", "")
        and "evidence_correction" not in source
    )


def _verify_android_context_commit(expected_commit: str) -> None:
    """Reject stale/unknown evidence before queuing; reviewer rechecks at execution."""
    import subprocess

    if (
        not isinstance(expected_commit, str)
        or len(expected_commit) != 40
        or any(character not in "0123456789abcdef" for character in expected_commit)
    ):
        raise ValueError("Android evidence commit must be a full 40-character Git SHA")
    repository = _ANDROID_CLIENT_EVIDENCE_REPOSITORY
    if not repository.is_dir() or not (repository / ".git").exists():
        raise AgentStateConflictError("Committed Android evidence repository unavailable")
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        result = subprocess.run(
            ["/usr/bin/git", "-c", "safe.directory=" + str(repository),
             "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
             "-C", str(repository), "rev-parse", "--verify", "HEAD"],
            check=True, capture_output=True, text=True, timeout=15,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentStateConflictError("Android evidence revision could not be verified") from exc
    if result.stdout.strip() != expected_commit:
        raise AgentStateConflictError("Android evidence commit is not the verified repository HEAD")


def retry_review_with_android_evidence(
    *, card_id: str, review_run_id: str, candidate_digest: str,
    android_commit: str, requested_by: str, notes: str,
) -> dict:
    """One bounded evidence-only re-review; same run/ordinal/digest, no approval."""
    from backend.core.codex_review import verify_candidate

    notes = _required_text(notes, field="notes", maximum=2000)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            card = _locked_card(cur, card_id)
            cur.execute("""SELECT * FROM agent.runs WHERE card_id = %s
                ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE""", (card_id,))
            review = _row_to_dict(cur, cur.fetchone())
            if (not _retryable_android_client_evidence_review(card, review)
                    or str(review["id"]) != str(review_run_id)):
                raise AgentStateConflictError(
                    "Evidence retry requires the latest Android-compatibility-only blocked review"
                )
            metadata = _run_metadata(review)
            source = metadata["review_source"]
            evidence = metadata["independent_review"]
            if (
                source.get("review_cycle") != str(review["review_cycle"])
                or source.get("review_attempt") != review["review_attempt"]
                or evidence.get("review_cycle") != source["review_cycle"]
                or evidence.get("review_attempt") != source["review_attempt"]
                or evidence.get("card_revision") != card["revision"]
                or not isinstance(source.get("implementation_metadata"), dict)
                or source["implementation_metadata"].get("candidate_digest") != candidate_digest
                or evidence.get("candidate_digest") != candidate_digest
            ):
                raise AgentStateConflictError("Review candidate identity mismatch")

            cur.execute("""SELECT id, result_metadata FROM agent.runs
                WHERE card_id = %s AND phase = 'implementation'
                  AND status = 'succeeded' AND card_revision = %s
                ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE""",
                (card_id, card["revision"]))
            implementation = _row_to_dict(cur, cur.fetchone())
            implementation_metadata = source["implementation_metadata"]
            if (
                implementation is None
                or str(implementation["id"]) != str(source["implementation_run_id"])
                or implementation["result_metadata"] != implementation_metadata
                or implementation_metadata.get("repository_scope") != "backend"
            ):
                raise AgentStateConflictError("Evidence retry is not bound to latest implementation")
            try:
                verify_candidate(implementation_metadata,
                                 run_id=str(implementation["id"]),
                                 revision=card["revision"], live=True)
            except Exception as exc:
                raise AgentStateConflictError("Candidate changed or is unavailable") from exc

            # No second evidence-correction retry for the same review run.
            cur.execute("""SELECT 1 FROM agent.events
                WHERE card_id = %s AND event_type = 'review.evidence_retry_requested'
                  AND payload->>'run_id' = %s LIMIT 1""", (card_id, str(review["id"])))
            if cur.fetchone() is not None:
                raise AgentStateConflictError("Evidence correction already attempted for this review")

            _verify_android_context_commit(android_commit)
            _require_transition(card["status"], CardStatus.REVIEW_QUEUED)
            pin = {
                "kind": "android_client_parser",
                "repository_scope": "android",
                "commit": android_commit,
                "requested_by": str(requested_by),
            }
            _insert_event(
                cur, card_id=card_id,
                event_type="review.evidence_retry_requested", actor_type="user",
                actor_user_id=requested_by,
                payload={
                    "run_id": str(review["id"]),
                    "implementation_run_id": str(implementation["id"]),
                    "candidate_digest": candidate_digest,
                    "review_cycle": source["review_cycle"],
                    "review_attempt": source["review_attempt"],
                    "evidence_correction": pin,
                    "requested_notes": notes,
                    "previous_review": evidence,
                    "previous_execution": {
                        "run_id": str(review["id"]),
                        "status": review["status"],
                        "attempt_count": review["attempt_count"],
                        "worker_id": review.get("worker_id"),
                        "started_at": str(review.get("started_at")),
                        "finished_at": str(review.get("finished_at")),
                        "blocked_reason": review.get("blocked_reason"),
                        "error_message": review.get("error_message"),
                        "result_message_id": review.get("result_message_id"),
                        "result_metadata": metadata,
                    },
                },
            )
            next_source = {**source, "evidence_correction": pin}
            cur.execute("""UPDATE agent.runs SET
                    status = 'queued', worker_id = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_heartbeat_at = NULL,
                    started_at = NULL, finished_at = NULL,
                    available_at = CURRENT_TIMESTAMP, blocked_reason = NULL,
                    error_message = NULL, result_message_id = NULL,
                    result_metadata = %s::jsonb
                WHERE id = %s AND status = 'succeeded' AND attempt_count = 2""",
                (json.dumps({"review_source": next_source}, sort_keys=True), review["id"]))
            if cur.rowcount != 1:
                raise AgentStateConflictError("Review evidence retry lost original execution")
            _update_card_status(cur, card_id=card_id, status=CardStatus.REVIEW_QUEUED)
        result = _card_detail(conn, card_id)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def retry_infrastructure_review(
    *, card_id: str, review_run_id: str, candidate_digest: str,
    requested_by: str, notes: str | None = None,
) -> dict:
    """Repeat the same frozen review execution; never create an extra ordinal.

    Previous verdict evidence stays in review.verdict and retry-requested events,
    and the original agent message is left intact. No approval is created.
    """
    from backend.core.codex_review import verify_candidate
    notes = _optional_text(notes, field="notes", maximum=2000)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            card = _locked_card(cur, card_id)
            cur.execute("""SELECT * FROM agent.runs WHERE card_id = %s
                ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE""", (card_id,))
            review = _row_to_dict(cur, cur.fetchone())
            if (not _retryable_failed_closed_review(card, review)
                or str(review["id"]) != str(review_run_id)):
                raise AgentStateConflictError(
                    "Review retry requires the exact latest infrastructure-blocked review"
                )
            metadata = _run_metadata(review)
            source = metadata.get("review_source")
            evidence = metadata["independent_review"]
            if (not isinstance(source, dict)
                or source.get("review_cycle") != str(review.get("review_cycle"))
                or source.get("review_attempt") != review.get("review_attempt")
                or evidence.get("review_cycle") != source.get("review_cycle")
                or evidence.get("review_attempt") != source.get("review_attempt")
                or evidence.get("card_revision") != card["revision"]
                or source.get("implementation_run_id") != evidence.get("implementation_run_id")
                or not isinstance(source.get("implementation_metadata"), dict)
                or source["implementation_metadata"].get("candidate_digest") != candidate_digest
                or evidence.get("candidate_digest") != candidate_digest):
                raise AgentStateConflictError("Review retry candidate identity mismatch")

            cur.execute("""SELECT id, result_metadata FROM agent.runs
                WHERE card_id = %s AND phase = 'implementation'
                  AND status = 'succeeded' AND card_revision = %s
                ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE""",
                (card_id, card["revision"]))
            implementation = _row_to_dict(cur, cur.fetchone())
            implementation_metadata = source["implementation_metadata"]
            if (implementation is None
                or str(implementation["id"]) != str(source["implementation_run_id"])
                or implementation["result_metadata"] != implementation_metadata
                or implementation_metadata.get("repository_scope") != card["repository_scope"]):
                raise AgentStateConflictError("Review retry is not bound to latest implementation")
            try:
                verify_candidate(implementation_metadata, run_id=str(implementation["id"]),
                                 revision=card["revision"], live=True)
            except Exception as exc:
                raise AgentStateConflictError("Review candidate changed or is unavailable") from exc

            _require_transition(card["status"], CardStatus.REVIEW_QUEUED)
            _insert_event(cur, card_id=card_id,
                          event_type="review.infrastructure_retry_requested",
                          actor_type="user", actor_user_id=requested_by,
                          payload={"run_id": str(review["id"]),
                                   "implementation_run_id": str(implementation["id"]),
                                   "candidate_digest": candidate_digest,
                                   "review_cycle": source["review_cycle"],
                                   "review_attempt": source["review_attempt"],
                                   "previous_attempt_count": review["attempt_count"],
                                   "previous_review": evidence,
                                   "previous_result_message_id": review.get("result_message_id"),
                                   "previous_finished_at": str(review.get("finished_at")),
                                   # Capture the execution fields that the queued
                                   # transition will overwrite, in the SAME DB
                                   # transaction as the run/card update. The
                                   # run ID is retained for the review ordinal;
                                   # this event identifies the prior execution.
                                   "previous_execution": {
                                       "run_id": str(review["id"]),
                                       "status": review["status"],
                                       "attempt_count": review["attempt_count"],
                                       "worker_id": review.get("worker_id"),
                                       "started_at": str(review.get("started_at")) if review.get("started_at") is not None else None,
                                       "finished_at": str(review.get("finished_at")) if review.get("finished_at") is not None else None,
                                       "error_message": review.get("error_message"),
                                       "blocked_reason": review.get("blocked_reason"),
                                       "result_message_id": review.get("result_message_id"),
                                       "result_metadata": metadata,
                                   },
                                   "requested_notes": notes})
            cur.execute("""UPDATE agent.runs SET
                    status = 'queued', worker_id = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_heartbeat_at = NULL,
                    started_at = NULL, finished_at = NULL,
                    available_at = CURRENT_TIMESTAMP, blocked_reason = NULL,
                    error_message = NULL, result_message_id = NULL,
                    result_metadata = %s::jsonb
                WHERE id = %s AND status = 'succeeded' AND attempt_count < 3""",
                (json.dumps({"review_source": source}, sort_keys=True), review["id"]))
            if cur.rowcount != 1:
                raise AgentStateConflictError("Review retry lost original execution")
            _update_card_status(cur, card_id=card_id, status=CardStatus.REVIEW_QUEUED)
        result = _card_detail(conn, card_id)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


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
                SELECT id, phase, card_revision, input_message_id,
                       review_cycle, review_attempt, result_metadata
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
            if phase is RunPhase.REVIEW:
                from backend.core.codex_review import verify_candidate

                cur.execute(
                    """
                    SELECT id FROM agent.runs
                    WHERE card_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1 FOR UPDATE
                    """,
                    (card_id,),
                )
                latest_run = _row_to_dict(cur, cur.fetchone())
                if latest_run is None or str(latest_run["id"]) != str(failed_run["id"]):
                    raise AgentStateConflictError(
                        "Review retry rejected: a newer run exists"
                    )

                metadata = _run_metadata(failed_run)
                review_source = metadata.get("review_source")
                cycle = failed_run.get("review_cycle")
                attempt = failed_run.get("review_attempt")

                if (
                    card["repository_scope"] not in {"backend", "android"}
                    or failed_run["card_revision"] != card["revision"]
                    or not isinstance(review_source, dict)
                    or not isinstance(cycle, (str, UUID))
                    or type(attempt) is not int
                    or not 1 <= attempt <= 3
                    or str(cycle) != review_source.get("review_cycle")
                    or attempt != review_source.get("review_attempt")
                    or not isinstance(
                        review_source.get("implementation_metadata"), dict
                    )
                ):
                    raise AgentStateConflictError(
                        "Review retry requires valid original review identity"
                    )

                implementation_id = review_source.get("implementation_run_id")
                implementation_metadata = review_source["implementation_metadata"]

                cur.execute(
                    """
                    SELECT id, result_metadata FROM agent.runs
                    WHERE card_id = %s
                      AND phase = 'implementation'
                      AND status = 'succeeded'
                      AND card_revision = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1 FOR UPDATE
                    """,
                    (card_id, card["revision"]),
                )
                implementation = _row_to_dict(cur, cur.fetchone())

                if (
                    implementation is None
                    or str(implementation["id"]) != str(implementation_id)
                    or implementation["result_metadata"] != implementation_metadata
                    or implementation_metadata.get("repository_scope")
                       != card["repository_scope"]
                    or not isinstance(
                        implementation_metadata.get("candidate_digest"), str
                    )
                ):
                    raise AgentStateConflictError(
                        "Review retry is not bound to the latest successful implementation"
                    )

                try:
                    verify_candidate(
                        implementation_metadata,
                        run_id=str(implementation_id),
                        revision=card["revision"],
                        live=True,
                    )
                except Exception as exc:
                    raise AgentStateConflictError(
                        "Review candidate changed or is unavailable"
                    ) from exc

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

            retry_message_id: str | None = None
            if notes is not None and phase in {
                RunPhase.PLANNING,
                RunPhase.IMPLEMENTATION,
            }:
                retry_message_id = _insert_message(
                    cur,
                    card_id=card_id,
                    author_type="user",
                    content=notes,
                    created_by=requested_by,
                    client_message_id=None,
                )

            if phase is RunPhase.REVIEW:
                # A review attempt is unique per card revision.
                # Requeue the existing execution rather than inserting
                # a duplicate logical review attempt.
                cur.execute(
                    """
                    SELECT attempt_count, error_message, finished_at,
                           worker_id, result_message_id
                    FROM agent.runs
                    WHERE id = %s AND status = 'failed'
                    FOR UPDATE
                    """,
                    (failed_run["id"],),
                )
                previous_execution = _row_to_dict(cur, cur.fetchone())
                if previous_execution is None:
                    raise AgentStateConflictError(
                        "Review retry requires an existing failed execution"
                    )

                if previous_execution["attempt_count"] >= 3:
                    raise AgentStateConflictError(
                        "Review retry rejected: worker execution limit reached"
                    )

                run_id = str(failed_run["id"])

                _insert_event(
                    cur,
                    card_id=card_id,
                    event_type="run.review_retry_requested",
                    actor_type="user",
                    actor_user_id=requested_by,
                    payload={
                        "run_id": run_id,
                        "review_cycle": str(cycle),
                        "review_attempt": attempt,
                        "previous_attempt_count": previous_execution["attempt_count"],
                        "previous_error": previous_execution["error_message"],
                        "previous_finished_at": (
                            str(previous_execution["finished_at"])
                            if previous_execution["finished_at"] else None
                        ),
                        "previous_worker_id": previous_execution["worker_id"],
                        "previous_result_message_id": (
                            str(previous_execution["result_message_id"])
                            if previous_execution["result_message_id"] else None
                        ),
                    },
                )

                cur.execute(
                    """
                    UPDATE agent.runs
                    SET status = 'queued',
                        worker_id = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_heartbeat_at = NULL,
                        available_at = CURRENT_TIMESTAMP,
                        blocked_reason = NULL
                    WHERE id = %s AND status = 'failed'
                    """,
                    (run_id,),
                )
                if cur.rowcount != 1:
                    raise AgentStateConflictError(
                        "Review retry could not requeue failed execution"
                    )
            else:
                run_id = _insert_run(
                    cur,
                    card_id=card_id,
                    phase=phase,
                    card_revision=failed_run["card_revision"],
                    requested_by=requested_by,
                    input_message_id=(
                        retry_message_id
                        if retry_message_id is not None
                        else (
                            failed_run["input_message_id"]
                            if phase is RunPhase.PLANNING
                            else None
                        )
                    ),
                )
            if phase is RunPhase.IMPLEMENTATION:
                cur.execute("""UPDATE agent.runs AS retry
                    SET result_metadata = prior.result_metadata,
                        review_cycle = prior.review_cycle, review_attempt = prior.review_attempt
                    FROM agent.runs AS prior WHERE retry.id = %s AND prior.id = %s""",
                    (run_id, failed_run["id"]))
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
                    "input_message_id": retry_message_id,
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
        if deployment_trigger_scope is RepositoryScope.BACKEND:
            _request_deployment_worker(
                deployment_trigger_scope,
                deployment_environment="qa",
            )
        else:
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
        if deployment_trigger_scope is RepositoryScope.BACKEND:
            _request_deployment_worker(
                deployment_trigger_scope,
                deployment_environment="production",
            )
        else:
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


def _require_passing_independent_review(cur, result, *, card_id, card_revision):
    from backend.core.codex_review import candidate_digest, verify_candidate, parse_verdict
    metadata = result["result_metadata"]
    try:
        digest = candidate_digest(metadata, run_id=result["id"], revision=card_revision)
        if digest != metadata.get("candidate_digest"):
            raise ValueError("Candidate identity changed")
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentStateConflictError("Deployment requires independent review of the exact candidate") from exc
    cur.execute("""SELECT id, result_metadata FROM agent.runs
        WHERE card_id = %s AND card_revision = %s AND phase = 'review'
          AND status = 'succeeded'
          AND result_metadata #>> '{independent_review,verdict}' = 'pass'
          AND result_metadata #>> '{independent_review,implementation_run_id}' = %s
          AND result_metadata #>> '{independent_review,candidate_digest}' = %s
        ORDER BY created_at DESC, id DESC LIMIT 1""",
        (card_id, card_revision, str(result["id"]), digest))
    reviewed = _row_to_dict(cur, cur.fetchone())
    evidence = reviewed.get("result_metadata", {}).get("independent_review", {}) if reviewed else {}
    if (evidence.get("verdict") != "pass" or evidence.get("candidate_digest") != digest
        or evidence.get("implementation_run_id") != str(result["id"])
        or evidence.get("card_revision") != card_revision
        or not evidence.get("thread_id") or not evidence.get("turn_id")
        or not evidence.get("validation_evidence")):
        raise AgentStateConflictError("Deployment requires passing independent review for this revision and digest")
    try:
        parse_verdict(json.dumps({k: evidence.get(k) for k in ("verdict", "summary", "findings")}))
        verify_candidate(metadata, run_id=result["id"], revision=card_revision, live=True)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AgentStateConflictError("Candidate changed or is unavailable after independent review") from exc
    # Include only database-backed verdict evidence in the deployment source.
    result = {**result, "result_metadata": {**metadata, "independent_review": evidence}}
    return result


def _exhausted_review(card, run):
    evidence = _run_metadata(run).get("independent_review", {}) if run else {}
    return (isinstance(evidence, dict) and card.get("status") == "blocked" and card.get("resume_status") == "review_queued"
            and card.get("repository_scope") in {"backend", "android"}
            and run is not None and run.get("phase") == "review"
            and run.get("status") == "succeeded" and run.get("card_revision") == card.get("revision")
            and evidence.get("verdict") == "changes_requested" and evidence.get("review_attempt") == 3)


def _review_decisions(evidence, decisions):
    from backend.core.codex_review import parse_verdict
    parse_verdict(
        json.dumps({k: evidence.get(k) for k in ("verdict", "summary", "findings")}),
        allow_legacy_findings=True,
    )
    if (not isinstance(decisions, list) or len(decisions) != len(evidence["findings"])
        or any(not isinstance(d, dict) or set(d) != {"finding_index", "decision"}
               or type(d["finding_index"]) is not int
               or d["decision"] not in {"fix", "defer_ignore"} for d in decisions)
        or sorted(d["finding_index"] for d in decisions) != list(range(len(evidence["findings"])))):
        raise ValueError("Choose fix or defer/ignore for every exact finding")
    return [{**d, "finding": evidence["findings"][d["finding_index"]]} for d in decisions]


def request_review_revision(*, card_id, requested_by, review_run_id, candidate_digest, notes, decisions):
    """Serialized on the card lock. Retries after success conflict; no duplicate cycle."""
    from backend.core.codex_review import verify_candidate
    notes = _required_text(notes, field="notes", maximum=2000)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            card = _locked_card(cur, card_id)
            cur.execute("""SELECT * FROM agent.runs WHERE card_id = %s
                ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE""", (card_id,))
            review = _row_to_dict(cur, cur.fetchone())
            if not _exhausted_review(card, review) or str(review["id"]) != review_run_id:
                raise AgentStateConflictError("Request Revision requires the exact latest exhausted third review")
            metadata = _run_metadata(review)
            evidence = metadata["independent_review"]
            source = metadata.get("review_source", {})
            if (evidence.get("candidate_digest") != candidate_digest
                or evidence.get("implementation_run_id") != source.get("implementation_run_id")
                or evidence.get("review_cycle") != str(review.get("review_cycle"))
                or review.get("review_attempt") != 3 or source.get("review_attempt") != 3
                or source.get("review_cycle") != evidence.get("review_cycle")
                or evidence.get("card_revision") != card["revision"]):
                raise AgentStateConflictError("Review candidate identity changed")
            bound_decisions = _review_decisions(evidence, decisions)
            cur.execute("""SELECT * FROM agent.runs WHERE card_id = %s AND card_revision = %s
                AND phase = 'implementation' AND status = 'succeeded'
                ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE""", (card_id, card["revision"]))
            implementation = _row_to_dict(cur, cur.fetchone())
            if implementation is None or str(implementation["id"]) != evidence["implementation_run_id"]:
                raise AgentStateConflictError("Review is not bound to the latest candidate")
            implementation_metadata = _run_metadata(implementation)
            if (implementation_metadata.get("candidate_digest") != candidate_digest
                or implementation_metadata.get("repository_scope") != card["repository_scope"]
                or not isinstance(source.get("implementation_metadata"), dict)
                or source["implementation_metadata"].get("candidate_digest") != candidate_digest):
                raise AgentStateConflictError("Candidate digest changed")
            try:
                verify_candidate(implementation_metadata, run_id=str(implementation["id"]), revision=card["revision"], live=True)
            except Exception as exc:
                raise AgentStateConflictError("Candidate changed or is unavailable") from exc
            revision = card["revision"] + 1
            handoff = {"actor_user_id": str(requested_by), "review_run_id": review_run_id,
                       "prior_revision": card["revision"], "review_cycle": evidence["review_cycle"],
                       "review_attempt": 3, "implementation_run_id": evidence["implementation_run_id"],
                       "candidate_digest": candidate_digest, "notes": notes, "decisions": bound_decisions}
            history = implementation_metadata.get("human_review_handoffs", []) + [handoff]
            message_id = _insert_message(cur, card_id=card_id, author_type="user", content=notes,
                                         created_by=requested_by, client_message_id=None)
            _insert_approval(cur, card_id=card_id, approval_type="implementation", card_revision=revision,
                             decided_by=requested_by, notes=notes)
            run_id = _insert_run(cur, card_id=card_id, phase=RunPhase.IMPLEMENTATION,
                                 card_revision=revision, requested_by=requested_by, input_message_id=message_id)
            next_metadata = {"review_cycle": run_id, "review_attempt": 1,
                             "human_review_handoffs": history,
                             "review_findings": [d["finding"] for d in bound_decisions if d["decision"] == "fix"]}
            cur.execute("UPDATE agent.runs SET result_metadata = %s::jsonb WHERE id = %s",
                        (json.dumps(next_metadata), run_id))
            _require_transition(card["status"], CardStatus.IMPLEMENTATION_QUEUED)
            _update_card_status(cur, card_id=card_id, status=CardStatus.IMPLEMENTATION_QUEUED, revision=revision)
            _insert_event(cur, card_id=card_id, event_type="review.human_revision_requested", actor_type="user",
                          actor_user_id=requested_by, payload={**handoff, "revision": revision, "run_id": run_id})
        result = _card_detail(conn, card_id)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)

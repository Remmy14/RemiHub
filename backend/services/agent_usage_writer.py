"""Best-effort worker-side telemetry writes. Never affect a card transition."""
from __future__ import annotations

import logging
import os
import threading
import time

from backend.core.agent_usage import normalize_turn_usage

logger = logging.getLogger("remihub.agent_usage")
_turn_write_slots = threading.BoundedSemaphore(8)
_TURN_WRITE_WAIT_SECONDS = 0.1
_TURN_DB_TIMEOUT_SECONDS = 5
_turn_writes_condition = threading.Condition()
_pending_turn_writes: set[threading.Thread] = set()
_turn_write_failures = 0


def flush_turn_writes(*, timeout_seconds: float = 30) -> bool:
    """Wait at worker shutdown for committed turn usage, with a finite bound."""
    global _turn_write_failures
    deadline = time.monotonic() + timeout_seconds
    with _turn_writes_condition:
        while _pending_turn_writes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _turn_writes_condition.wait(remaining)
        succeeded = _turn_write_failures == 0
        _turn_write_failures = 0
        return succeeded


def publish_stage_configuration(*, executor_name: str, worker_id: str,
                                model: str | None, reasoning_effort: str | None = None) -> None:
    """Publish only non-secret worker settings; failures never stop the worker."""
    if executor_name not in {"codex-planning", "codex-implementation", "codex-review"}:
        return
    from backend.services.agent_worker_service import get_db_conn, put_db_conn
    conn = None
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO agent.codex_stage_configuration
                    (stage, worker_id, configured_model, configured_reasoning_effort)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (stage, worker_id) DO UPDATE SET
                    configured_model = EXCLUDED.configured_model,
                    configured_reasoning_effort = EXCLUDED.configured_reasoning_effort,
                    reported_at = CURRENT_TIMESTAMP
            """, (executor_name.removeprefix("codex-"), worker_id, model, reasoning_effort))
        conn.commit()
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        logger.warning("Codex stage configuration unavailable: %s", type(exc).__name__)
    finally:
        if conn is not None:
            put_db_conn(conn)


def record_turn(claim, *, turn_id: str, usage: object, configured_model: str | None,
                actual_model: str | None = None, reasoning_effort: str | None = None,
                source: str = "codex_sdk_turn") -> None:
    global _turn_write_failures
    if os.environ.get("REMIHUB_AGENT_EXECUTOR") != "codex-" + claim.phase.value:
        return
    if not isinstance(turn_id, str) or not turn_id:
        return
    data = normalize_turn_usage(usage)
    data["usage_source"] = source
    if not _turn_write_slots.acquire(blocking=False):
        with _turn_writes_condition:
            _turn_write_failures += 1
        logger.warning("Codex turn telemetry capacity unavailable for run %s", claim.id)
        return

    def write() -> None:
        global _turn_write_failures
        succeeded = False
        try:
            succeeded = _write(claim, turn_id, data, configured_model, actual_model, reasoning_effort)
        finally:
            with _turn_writes_condition:
                if not succeeded:
                    _turn_write_failures += 1
                _pending_turn_writes.discard(threading.current_thread())
                _turn_writes_condition.notify_all()
            _turn_write_slots.release()

    thread = threading.Thread(target=write, name="agent-turn-usage", daemon=True)
    with _turn_writes_condition:
        _pending_turn_writes.add(thread)
    try:
        thread.start()
    except Exception:
        with _turn_writes_condition:
            _turn_write_failures += 1
            _pending_turn_writes.discard(thread)
            _turn_writes_condition.notify_all()
        _turn_write_slots.release()
        logger.exception("Could not start Codex turn telemetry for run %s", claim.id)
        return
    # Fast healthy writes usually finish before returning; a stalled pool or
    # query cannot hold up the card transition beyond this small deadline.
    thread.join(_TURN_WRITE_WAIT_SECONDS)


def ensure_attempt(claim, *, configured_model: str | None = None) -> bool:
    if os.environ.get("REMIHUB_AGENT_EXECUTOR") != "codex-" + claim.phase.value:
        return False
    if claim.phase.value == "deployment":
        return True  # No Codex invocation in the current deployment executor.
    source = "codex_app_server_notification" if claim.phase.value == "review" else "codex_sdk_turn"
    return _write(claim, "", {"usage_status": "unavailable", "usage_source": source},
                  configured_model, None, None)


def _write(claim, turn_id, data, configured_model, actual_model, reasoning_effort):
    from backend.services.agent_worker_service import get_db_conn, put_db_conn
    conn = None
    try:
        conn = get_db_conn(connect_timeout=_TURN_DB_TIMEOUT_SECONDS)
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '5s'")
            # Serialize the placeholder check/insert and turn replacement for
            # this attempt, even when their best-effort writers overlap.
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"{claim.id}:{claim.attempt_count}",))
            if not turn_id:
                cur.execute("SELECT 1 FROM agent.codex_execution_usage WHERE run_id = %s AND attempt_count = %s LIMIT 1",
                            (claim.id, claim.attempt_count))
                if cur.fetchone():
                    conn.rollback()  # End the read transaction before pooling.
                    return True
            else:
                cur.execute("DELETE FROM agent.codex_execution_usage "
                            "WHERE run_id = %s AND attempt_count = %s AND turn_id = ''",
                            (claim.id, claim.attempt_count))
            cur.execute("""
                INSERT INTO agent.codex_execution_usage
                    (run_id, attempt_count, turn_id, card_id, card_revision, stage,
                     review_attempt, configured_model, actual_model, reasoning_effort,
                     input_tokens, output_tokens, cached_input_tokens, reasoning_tokens,
                     total_tokens, usage_status, usage_source, started_at, finished_at)
                SELECT r.id, %s, %s, r.card_id, r.card_revision, r.phase,
                       r.review_attempt, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       r.started_at, CURRENT_TIMESTAMP
                FROM agent.runs r WHERE r.id = %s
                ON CONFLICT (run_id, attempt_count, turn_id) DO NOTHING
            """, (claim.attempt_count, turn_id, configured_model, actual_model, reasoning_effort,
                  data.get("input_tokens"), data.get("output_tokens"),
                  data.get("cached_input_tokens"), data.get("reasoning_tokens"),
                  data.get("total_tokens"), data["usage_status"], data["usage_source"], claim.id))
        conn.commit()
        return True
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        logger.warning("Codex telemetry unavailable for run %s: %s", claim.id, type(exc).__name__)
        return False
    finally:
        if conn is not None:
            put_db_conn(conn)

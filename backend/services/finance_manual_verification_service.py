"""Durable manual execution around the existing Finance verification engine."""

from datetime import datetime, timedelta, timezone
import json
import logging
from uuid import UUID, uuid4

from psycopg2.extras import Json

from backend.database.database import get_db_conn, put_db_conn
from backend.services import finance_health_service, finance_monitor_service
from backend.services import finance_verification_service as verification


logger = logging.getLogger(__name__)
COOLDOWN = timedelta(minutes=30)
ERROR_CODE = "VERIFICATION_EXECUTION_FAILED"


def _row(cur):
    columns = [column[0] for column in cur.description]
    values = cur.fetchone()
    return dict(zip(columns, values)) if values else None


def _response(row: dict, *, reused: bool = False, retry_at=None) -> dict:
    return {
        "execution_id": str(row["id"]) if row.get("id") else None,
        "trigger": row.get("run_trigger", row.get("trigger")), "status": row["status"],
        "requested_at": row.get("requested_at"), "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"), "reused": reused,
        "retry_at": retry_at, "summary": row.get("summary") or {},
        "results": row.get("results") or {"connections": [], "accounts": []},
        "error_code": row.get("error_code"),
    }


def _safe_account_label(account: dict) -> str:
    institution = str(account.get("institution_label") or "Financial account")
    mask = account.get("mask")
    suffix = f" ••••{mask}" if isinstance(mask, str) and mask.isdigit() and len(mask) <= 4 else ""
    return institution + suffix


def request_run() -> tuple[str, dict, str | None]:
    """Atomically claim the cross-process lease or report active/cooldown state."""
    _retry_scheduled_monitoring()
    now = datetime.now(timezone.utc)
    owner = str(uuid4())
    run_id = uuid4()
    interrupted_run_id = None
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.finance_plaid_verification_lease (id)
                VALUES (true) ON CONFLICT (id) DO NOTHING
            """)
            cur.execute("""
                SELECT owner, expires_at, run_trigger, run_id, started_at
                FROM public.finance_plaid_verification_lease WHERE id = true FOR UPDATE
            """)
            active = cur.fetchone()
            if active[0] and active[1] and active[1] > now:
                if active[2] == "manual" and active[3]:
                    cur.execute("""
                        SELECT * FROM public.finance_verification_runs WHERE id = %s
                    """, (active[3],))
                    row = _row(cur)
                    if row:
                        conn.commit()
                        return "existing", _response(row, reused=True), None
                conn.commit()
                return "existing", _response({
                    "id": None, "trigger": "scheduled", "status": "running",
                    "started_at": active[4],
                }, reused=True), None
            if active[3]:
                interrupted_run_id = active[3]
                cur.execute("""
                    UPDATE public.finance_verification_runs
                    SET status = CASE WHEN jsonb_array_length(results->'connections') > 0
                                      THEN 'partially_completed' ELSE 'failed' END,
                        completed_at = %s, error_code = 'EXECUTION_INTERRUPTED'
                    WHERE id = %s AND status IN ('pending', 'running')
                """, (now, active[3]))
            cur.execute("""
                SELECT requested_at FROM public.finance_verification_runs
                ORDER BY requested_at DESC LIMIT 1
            """)
            latest = cur.fetchone()
            if latest and latest[0] + COOLDOWN > now:
                retry_at = latest[0] + COOLDOWN
                conn.commit()
                return "cooldown", _response({
                    "id": None, "trigger": "manual", "status": "failed"
                }, retry_at=retry_at), None
            cur.execute("""
                INSERT INTO public.finance_verification_runs
                    (id, run_trigger, status, requested_at)
                VALUES (%s, 'manual', 'pending', %s)
            """, (run_id, now))
            cur.execute("""
                UPDATE public.finance_plaid_verification_lease
                SET owner = %s, expires_at = %s, run_trigger = 'manual', run_id = %s
                WHERE id = true
            """, (owner, now + verification.LEASE, run_id))
        conn.commit()
        return "new", _response({"id": run_id, "trigger": "manual",
                                  "status": "pending", "requested_at": now}), owner
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)
        if interrupted_run_id:
            _reconcile_unmonitored_run(interrupted_run_id)


def _update(run_id: UUID, owner: str, **fields) -> bool:
    """Update only while this run still owns its live lease."""
    allowed = {"status", "started_at", "completed_at", "summary", "results",
               "error_code", "monitoring_reconciled"}
    if not fields or set(fields) - allowed:
        raise ValueError("invalid execution update")
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT owner FROM public.finance_plaid_verification_lease
                WHERE id = true AND owner = %s AND run_id = %s
                  AND expires_at > clock_timestamp() FOR UPDATE
            """, (owner, run_id))
            if cur.fetchone() is None:
                conn.commit()
                return False
            sql = ", ".join(f"{key} = %s" for key in fields)
            values = [Json(value, dumps=lambda data: json.dumps(data, default=str))
                      if key in {"summary", "results"} else value
                      for key, value in fields.items()]
            cur.execute(f"UPDATE public.finance_verification_runs SET {sql} WHERE id = %s",
                        (*values, run_id))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _release_and_replay(owner: str) -> None:
    try:
        scheduled_pending = verification._release(owner)
    except Exception:
        logger.error("Manual Finance verification lease release failed")
        return
    if scheduled_pending:
        _run_deferred_scheduled_check()


def _recover_scheduled_handoff() -> None:
    """Pick up a due worker check left behind by an interrupted handoff."""
    conn = None
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT scheduled_pending, owner, expires_at
                FROM public.finance_plaid_verification_lease WHERE id = true
            """)
            row = cur.fetchone()
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        logger.error("Deferred scheduled Finance handoff lookup failed")
        return
    finally:
        if conn is not None:
            put_db_conn(conn)
    if row and row[0] and (not row[1] or not row[2] or row[2] <= datetime.now(timezone.utc)):
        _run_deferred_scheduled_check()


def _run_deferred_scheduled_check() -> None:
    """The shared scheduled claim atomically marks monitoring before Item work."""
    claim = {}
    try:
        verification.verify_enabled_items(replay_generation_out=claim,
                                          require_pending=True)
    except Exception:
        # A later Item can fail after earlier evidence was committed.
        logger.error("Deferred scheduled Finance verification failed")
    generation = claim.get("generation")
    if generation is None:
        return
    try:
        _finish_scheduled_monitoring_attempt(generation)
    except Exception:
        # The pending marker remains recoverable after its bounded lease.
        logger.error("Deferred scheduled Finance monitoring state could not be updated")
    _retry_scheduled_monitoring()


def _finish_scheduled_monitoring_attempt(generation: int) -> None:
    """Make a completed replay immediately eligible for reconciliation."""
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE public.finance_plaid_verification_lease
                SET monitoring_retry_at = NULL
                WHERE id = true AND monitoring_pending = true
                  AND monitoring_generation = %s
            """, (generation,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _retry_scheduled_monitoring() -> None:
    """Reconcile a deferred worker check; never send reminders from this path."""
    conn = None
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT monitoring_pending, monitoring_generation, monitoring_retry_at
                FROM public.finance_plaid_verification_lease WHERE id = true
            """)
            row = cur.fetchone()
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        logger.error("Deferred scheduled Finance monitoring lookup failed")
        return
    finally:
        if conn is not None:
            put_db_conn(conn)
    if not row or not row[0] or (row[2] is not None and row[2] > datetime.now(timezone.utc)):
        return
    try:
        finance_monitor_service.assess_and_notify(send_reminders=False)
    except Exception:
        logger.error("Deferred scheduled Finance monitoring reconciliation failed")
        return
    conn = None
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE public.finance_plaid_verification_lease
                SET monitoring_pending = false, monitoring_retry_at = NULL
                WHERE id = true AND monitoring_pending = true
                  AND monitoring_generation = %s
                  AND (monitoring_retry_at IS NULL OR
                       monitoring_retry_at <= clock_timestamp())
            """, (row[1],))
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        logger.error("Deferred scheduled Finance monitoring state could not be stored")
    finally:
        if conn is not None:
            put_db_conn(conn)


def _reconcile_unmonitored_run(run_id: UUID) -> None:
    """Retry committed evidence after a crash or monitoring failure."""
    conn = None
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT status, error_code, started_at, monitoring_reconciled
                FROM public.finance_verification_runs WHERE id = %s
            """, (run_id,))
            row = cur.fetchone()
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        logger.error("Finance verification reconciliation lookup failed")
        return
    finally:
        if conn is not None:
            put_db_conn(conn)
    # A process can stop after _persist commits and before progress records
    # the public result, so a started run needs reconciliation even if its
    # execution result array is still empty.
    if (not row or row[0] not in {"completed", "partially_completed", "failed"} or
            row[2] is None or row[3]):
        return
    try:
        finance_monitor_service.assess_and_notify(send_reminders=False)
    except Exception:
        logger.error("Finance verification reconciliation failed")
        return
    conn = None
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE public.finance_verification_runs
                SET monitoring_reconciled = true
                WHERE id = %s AND status IN ('completed', 'partially_completed', 'failed')
                  AND monitoring_reconciled = false
            """, (run_id,))
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        logger.error("Finance verification reconciliation state could not be stored")
    finally:
        if conn is not None:
            put_db_conn(conn)


def run_manual(run_id: UUID, owner: str) -> None:
    """FastAPI background task; all final state is persisted for polling."""
    now = datetime.now(timezone.utc)
    try:
        started = _update(run_id, owner, status="running", started_at=now)
    except Exception:
        logger.error("Manual Finance verification could not start")
        _release_and_replay(owner)
        return
    if not started:
        return
    results = []
    considered = 0
    monitoring_reconciled = False

    def progress(row: dict, total: int) -> None:
        nonlocal considered
        considered = total
        results.append(row)
        if not _update(run_id, owner, results={"connections": results, "accounts": []}):
            raise RuntimeError("Finance verification lease lost")

    try:
        execution = verification._run_items(owner, started_at=now, on_result=progress)
        considered = execution["enabled_connections"]
        # The established monitor owns issue transitions; manual work never
        # emits reminders, including within the three-day/one-day windows.
        finance_monitor_service.assess_and_notify(send_reminders=False)
        monitoring_reconciled = True
        assessment = finance_health_service.assess_finance_health()
        evidence = finance_health_service.read_evidence()
        if evidence["errors"]:
            raise RuntimeError("Finance verification evidence unavailable")
        persisted = {str(item["id"]): item for item in evidence["items"]}
        enabled_by_label = {}
        for item in evidence["items"]:
            if item.get("enabled"):
                enabled_by_label.setdefault(item.get("label"), []).append(item)
        evaluated_ids = {str(account["id"]) for account in evidence["accounts"]
                         if account.get("source") == "plaid"
                         and account.get("include_in_net_worth")
                         and len(enabled_by_label.get(account.get("institution_label"), [])) == 1}
        account_evidence = {str(account["id"]): account for account in evidence["accounts"]}
        for result in results:
            item = persisted.get(result["connection_id"])
            if item:
                result["connection_status"] = item.get("connection_status") or "UNKNOWN"
                result["inventory_status"] = item.get("inventory_status") or "UNKNOWN"
                if result["connection_status"] == "AUTH_REQUIRED":
                    result["issue_code"] = "PLAID_REAUTH_REQUIRED"
                    result["explanation"] = "A confirmed authentication failure remains active."
                    result["recommended_action"] = "Reconnect this institution."
                elif result["inventory_status"] == "MISSING" and not result["issue_code"]:
                    result["issue_code"] = "PLAID_ACCOUNT_MISSING"
                    result["explanation"] = "A previously confirmed account-access issue remains active."
                    result["recommended_action"] = "Review the institution's account access."
        accounts = [{
            "account_id": str(a["account_id"]),
            "display_name": _safe_account_label(account_evidence[str(a["account_id"])]),
            "health": a["health"], "verification": a.get("verification"),
            "issues": a["issues"], "recommended_action": a.get("recommended_action"),
        } for a in assessment["accounts"] if a["source_type"] == "plaid"
             and str(a["account_id"]) in evaluated_ids]
        checked = sum(r["outcome"] in {"VERIFIED", "AUTH_REQUIRED"} for r in results)
        problems = sum(r["connection_status"] == "AUTH_REQUIRED" or
                       r["inventory_status"] == "MISSING" or r["missing_accounts"] > 0
                       or r["invalid_cached_balances"] > 0 for r in results)
        uncertain = sum(r["outcome"] in {"TRANSIENT", "UNKNOWN"} for r in results)
        summary = {
            "enabled_connections": considered, "connections_checked": checked,
            "connections_with_problems": problems,
            "connections_uncertain": uncertain + considered - len(results),
            "included_accounts": execution["included_accounts"],
            "accounts_with_actionable_issues": sum(bool(a["issues"]) for a in accounts),
            "methods_used": sorted({r["method"] for r in results}),
            "live_balance_tested": False, "complete": execution["complete"],
        }
        status = ("completed" if execution["complete"] and uncertain == 0 else
                  "partially_completed" if results else "failed")
        if not _update(run_id, owner, status=status,
                       completed_at=datetime.now(timezone.utc), summary=summary,
                       results={"connections": results, "accounts": accounts},
                       monitoring_reconciled=True):
            return
    except Exception:
        logger.error("Manual Finance verification execution failed")
        # Earlier Item results have already been committed by the shared
        # verifier. Reconcile that evidence even when a later Item fails.
        if not monitoring_reconciled:
            try:
                finance_monitor_service.assess_and_notify(send_reminders=False)
                monitoring_reconciled = True
            except Exception:
                logger.error("Manual Finance partial verification reconciliation failed")
        try:
            _update(run_id, owner,
                    status="partially_completed" if results else "failed",
                    completed_at=datetime.now(timezone.utc),
                    summary={"enabled_connections": considered,
                             "connections_uncertain": max(0, considered - len(results)),
                             "live_balance_tested": False, "complete": False},
                    results={"connections": results, "accounts": []},
                    error_code=ERROR_CODE,
                    monitoring_reconciled=monitoring_reconciled)
        except Exception:
            logger.error("Manual Finance verification failure state could not be stored")
    finally:
        _release_and_replay(owner)


def get_run(run_id: UUID) -> dict | None:
    now = datetime.now(timezone.utc)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM public.finance_verification_runs WHERE id = %s", (run_id,))
            row = _row(cur)
            if row and row["status"] in {"pending", "running"}:
                cur.execute("""
                    SELECT expires_at FROM public.finance_plaid_verification_lease
                    WHERE id = true AND run_id = %s AND owner IS NOT NULL
                """, (run_id,))
                lease = cur.fetchone()
                if not lease or not lease[0] or lease[0] <= now:
                    cur.execute("""
                        UPDATE public.finance_verification_runs
                        SET status = CASE WHEN jsonb_array_length(results->'connections') > 0
                                          THEN 'partially_completed' ELSE 'failed' END,
                            completed_at = %s, error_code = 'EXECUTION_INTERRUPTED'
                        WHERE id = %s AND status IN ('pending', 'running')
                        RETURNING *
                    """, (now, run_id))
                    row = _row(cur) or row
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)
    if row:
        if row["status"] in {"completed", "partially_completed", "failed"}:
            _reconcile_unmonitored_run(run_id)
        _retry_scheduled_monitoring()
    return _response(row) if row else None

"""Durable Finance health transitions observed by the existing Finance worker.

This service reads stored evidence only. It never collects accounts or creates
snapshots. Notification rows and monitoring decisions share one transaction.
"""

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
from zoneinfo import ZoneInfo

from psycopg2.extras import Json

from backend.database.database import get_db_conn, put_db_conn
from backend.notifications.notifications import Notification, insert_notification
from backend.services import finance_health_service


logger = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
CRITICAL_CODES = frozenset({
    "PLAID_CONNECTION_MISSING", "PLAID_REAUTH_REQUIRED",
    "PLAID_ACCOUNT_MISSING", "PLAID_BALANCE_MISSING",
    "MANUAL_VALUE_MISSING", "VALUATION_VALUE_MISSING",
})
WARNING_CODES = frozenset({
    "PLAID_DATA_STALE", "MANUAL_REVIEW_DUE", "VALUATION_FALLBACK_INVALID",
})


def upcoming_month(local_day: date) -> date:
    return date(local_day.year + (local_day.month == 12),
                local_day.month % 12 + 1, 1)


def reminder_interval(local_day: date) -> int | None:
    """Three-day notices may catch up one day; one-day notices do not."""
    remaining = (upcoming_month(local_day) - local_day).days
    if remaining in (3, 2):
        return 3
    if remaining == 1:
        return 1
    return None


def _rows(cur) -> list[dict]:
    columns = [item[0] for item in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _fingerprint(evidence: dict) -> str:
    value = json.dumps(evidence, sort_keys=True, default=str)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _admin_ids(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id FROM public.remihub_users
            WHERE role = 'admin' AND is_active = true ORDER BY id
        """)
        return [str(row[0]) for row in cur.fetchall()]


def _event(conn, *, key: str, kind: str, notice: Notification | None = None,
           issue_key: str | None = None, target_month: date | None = None,
           days_before: int | None = None, user_id: str | None = None) -> bool:
    """Insert one logical event; the unique key is the final concurrency guard."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.finance_health_monitor_events
                (event_key, event_type, issue_key, target_month, days_before, user_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_key) DO NOTHING RETURNING event_key
        """, (key, kind, issue_key, target_month, days_before, user_id))
        inserted = cur.fetchone() is not None
    if inserted and notice is not None:
        notification_id = insert_notification(notice, conn=conn)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE public.finance_health_monitor_events
                SET notification_id = %s WHERE event_key = %s
            """, (notification_id, key))
    return inserted


def _actionable(assessment: dict, evidence: dict) -> dict[str, dict]:
    accounts = {str(row.get("id")): row for row in evidence.get("accounts") or []}
    connections: dict[str, list[dict]] = {}
    for item in evidence.get("items") or []:
        if item.get("enabled"):
            connections.setdefault(str(item.get("label")), []).append(item)
    selected = {}
    for account in assessment["accounts"]:
        if not account["included_in_net_worth"]:
            continue
        for issue in account["issues"]:
            code = issue["code"]
            if code not in CRITICAL_CODES | WARNING_CODES:
                continue
            if code == "PLAID_DATA_STALE" and not account["last_successful_update"]:
                # Item-wide Plaid success is not account collection evidence.
                continue
            source_type = account["source_type"]
            source_id = str(account["account_id"])
            if code == "PLAID_REAUTH_REQUIRED":
                owner = accounts.get(source_id)
                matches = connections.get(str(owner.get("institution_label"))) if owner else []
                if matches and len(matches) == 1:
                    source_type, source_id = "plaid_connection", str(matches[0]["id"])
            key = f"{source_type}:{source_id}:{code}"
            selected[key] = {
                "issue_key": key, "source_type": source_type, "source_id": source_id,
                "code": code,
                "severity": "critical" if code in CRITICAL_CODES else "warning",
            }
    return selected


def _verified_resolution(issue: dict, assessment: dict, evidence: dict) -> tuple[bool, bool]:
    """Return (close, recovered) from evidence for this issue's own code.

    Another issue on the same account must neither prevent a confirmed fix nor
    make an uncertain condition look recovered. Exclusion closes silently.
    """
    if issue["source_type"] == "plaid_connection":
        connection = next((row for row in evidence.get("items") or []
                           if str(row.get("id")) == issue["source_id"]), None)
        if not connection:
            return False, False
        if evidence.get("accounts") is None:
            return False, False
        dependents = [row for row in evidence["accounts"] if (
            row.get("source") == "plaid"
            and row.get("include_in_net_worth")
            and row.get("institution_label") == connection.get("label")
        )]
        if not dependents:
            return True, False
        if not connection.get("enabled"):
            # A disabled item is no longer the collection path for these
            # accounts. Retire its stale error. Claim recovery only when one
            # enabled replacement and every included account have independent
            # successful collection evidence after this issue was opened.
            replacements = [row for row in evidence.get("items") or []
                            if row.get("enabled") and
                            row.get("label") == connection.get("label")]
            if len(replacements) != 1 or replacements[0].get("last_error"):
                return True, False
            replacement_success = finance_health_service._datetime(
                replacements[0].get("last_success_at"))
            if not replacement_success or replacement_success <= issue["opened_at"]:
                return True, False
            assessed = {str(row["account_id"]): row for row in assessment["accounts"]
                        if row["source_type"] == "plaid"}
            for dependent in dependents:
                account = assessed.get(str(dependent.get("id")))
                if account is None:
                    return True, False
                account_success = finance_health_service._datetime(
                    account.get("last_successful_update"))
                if (not account_success or account_success < replacement_success
                        or account["freshness"] != "CURRENT"
                        or any(problem["code"] in {
                            "PLAID_ACCOUNT_MISSING", "PLAID_BALANCE_MISSING",
                        } for problem in account["issues"])):
                    return True, False
            return True, True
        success = finance_health_service._datetime(connection.get("last_success_at"))
        return bool(not connection.get("last_error") and success and
                    success > issue["opened_at"]), True
    account = next((row for row in assessment["accounts"]
                    if row["source_type"] == issue["source_type"] and
                    str(row["account_id"]) == issue["source_id"]), None)
    if account is None:
        if (issue["source_type"] in {"manual", "vehicle"}
                and issue["code"] in {"MANUAL_VALUE_MISSING", "MANUAL_REVIEW_DUE",
                                      "VALUATION_FALLBACK_INVALID"}
                and evidence.get("manual") is not None
                and not any(str(asset.get("id")) == issue["source_id"]
                            for asset in evidence["manual"])):
            # The manual evidence query covers the active inventory only. A
            # successful read without this asset confirms it was archived or
            # removed; it is no longer actionable, but has not recovered.
            return True, False
        return False, False
    if not account["included_in_net_worth"]:
        return True, False
    code = issue["code"]
    if code in {item["code"] for item in account["issues"]}:
        return False, False
    if code in {"MANUAL_VALUE_MISSING", "VALUATION_FALLBACK_INVALID"}:
        asset = next((row for row in evidence.get("manual") or []
                      if str(row.get("id")) == issue["source_id"]), None)
        if asset is None:
            return False, False
        try:
            # Reuse the snapshot's value validator rather than declaring a
            # corrected balance from unrelated account health.
            finance_health_service.worker._snapshot_value(asset.get("current_value"))
        except (TypeError, ValueError):
            return False, False
        return True, True
    if code == "MANUAL_REVIEW_DUE":
        return account["freshness"] == "CURRENT", True
    if code in {"PLAID_ACCOUNT_MISSING", "PLAID_BALANCE_MISSING",
                "PLAID_DATA_STALE", "VALUATION_VALUE_MISSING"}:
        last_update = finance_health_service._datetime(account.get("last_successful_update"))
        return bool(account["freshness"] == "CURRENT" and last_update
                    and last_update > issue["opened_at"]), True
    if code == "PLAID_CONNECTION_MISSING":
        # A new enabled item without a verified account collection is not yet
        # proof that the account can supply its required balance.
        last_update = finance_health_service._datetime(account.get("last_successful_update"))
        return bool(last_update and last_update > issue["opened_at"]), True
    return False, False


def _notice(problem: dict, *, kind: str, user_id: str, after_collection: bool) -> Notification:
    critical = problem["severity"] == "critical"
    if kind == "resolved":
        title = "Finance issue resolved"
        body = "A previously reported Finance issue has been resolved. Review Finance for any remaining problems."
        priority = 0
    elif critical:
        title = "Finance account needs attention"
        body = ("A problem was found during the monthly collection. Review your Finance accounts."
                if after_collection else
                "Current account evidence needs attention before the next monthly collection.")
        priority = 1
    else:
        title = "Finance account review needed"
        body = "Current account evidence suggests a review before the next monthly collection."
        priority = 0
    return Notification(title=title, body=body, module="Finance", priority=priority,
                        data={"type": "finance_health", "action": "view_finance",
                              "destination": "finance", "issue_code": problem["code"],
                              "source_type": problem["source_type"],
                              "source_id": problem["source_id"], "user_id": user_id,
                              "evidence_phase": ("collection_attempt" if after_collection
                                                 else "advance")})


def _reminder_notice(*, user_id: str, days_before: int, target: date,
                     assessment: dict, problems: dict) -> Notification:
    critical = sum(row["severity"] == "critical" for row in problems.values())
    warnings = len(problems) - critical
    if critical:
        body = f"{critical} current Finance account problem(s) may affect the upcoming collection."
    elif warnings:
        body = f"{warnings} Finance account(s) need review before the upcoming collection."
    elif assessment["readiness"] == "BLOCKED":
        body = "Current snapshot evidence has a verified blocker; the upcoming collection is not yet verified."
    else:
        body = "Current evidence cannot confirm readiness for the upcoming collection."
    return Notification(
        title=("Finance snapshot preparation" if days_before == 3
               else "Finance snapshot readiness warning"),
        body=body, module="Finance", priority=1 if critical else 0,
        data={"type": "finance_health_reminder", "action": "view_finance",
              "destination": "finance", "user_id": user_id,
              "target_month": target.isoformat(), "days_before": str(days_before),
              "evidence_phase": "advance",
              "readiness": assessment["readiness"]},
    )


def _summary(assessment: dict, problems: dict) -> dict:
    return {
        "readiness": assessment["readiness"],
        "snapshot_month": assessment["snapshot"]["snapshot_month"].isoformat(),
        "counts": assessment["counts"],
        "actionable_count": len(problems),
        "insufficient_evidence": assessment["readiness"] == "UNKNOWN",
    }


def assess_and_notify(*, now: datetime | None = None,
                      after_collection: bool = False) -> bool:
    """Assess on first local cycle and whenever committed evidence changes.

    A failed source read is operational failure, not a healthy transition.
    A singleton row lock holds the assessment and notification transaction.
    """
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("Finance monitoring requires a timezone-aware clock")
    local_day = instant.astimezone(NEW_YORK).date()
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            # Schema migrations remain DDL-only. This insert is safe when two
            # worker cycles race to initialize the singleton on first use.
            cur.execute("""
                INSERT INTO public.finance_health_monitor_state (id)
                VALUES (true) ON CONFLICT (id) DO NOTHING
            """)
            cur.execute("""
                SELECT assessed_on, evidence_hash, readiness
                FROM public.finance_health_monitor_state WHERE id = true FOR UPDATE
            """)
            state = cur.fetchone()
        if state is None:
            raise RuntimeError("Finance monitoring migration is not installed")
        evidence = finance_health_service.read_evidence()
        if evidence["errors"]:
            raise RuntimeError("Finance health evidence read failed")
        evidence_hash = _fingerprint(evidence)
        if state[0] == local_day and state[1] == evidence_hash:
            conn.commit()
            return False
        assessment = finance_health_service.assess_evidence(
            evidence, now=instant, calendar_date=local_day)
        if any(issue["code"] == "COMPLETENESS_UNAVAILABLE"
               for issue in assessment["snapshot"]["warnings"]):
            raise RuntimeError("Finance health completeness assessment failed")
        problems = _actionable(assessment, evidence)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM public.finance_health_monitor_issues FOR UPDATE")
            previous = {row["issue_key"]: row for row in _rows(cur)}
        admins = _admin_ids(conn)
        opened_critical = False
        for key, problem in problems.items():
            old = previous.get(key)
            if old and old["active"]:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE public.finance_health_monitor_issues
                        SET last_seen_at = %s WHERE issue_key = %s
                    """, (instant, key))
                # An admin may have been added after an unnotified occurrence.
                occurrence = old["occurrence"]
                need_notice = not old["notified"]
            else:
                occurrence = old["occurrence"] + 1 if old else 1
                need_notice = True
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO public.finance_health_monitor_issues
                            (issue_key, source_type, source_id, code, severity,
                             active, occurrence, opened_at, last_seen_at, resolved_at, notified)
                        VALUES (%s, %s, %s, %s, %s, true, %s, %s, %s, NULL, false)
                        ON CONFLICT (issue_key) DO UPDATE SET
                            severity = EXCLUDED.severity, active = true,
                            occurrence = EXCLUDED.occurrence, opened_at = EXCLUDED.opened_at,
                            last_seen_at = EXCLUDED.last_seen_at, resolved_at = NULL,
                            notified = false
                    """, (key, problem["source_type"], problem["source_id"],
                          problem["code"], problem["severity"], occurrence,
                          instant, instant))
            if need_notice:
                for user_id in admins:
                    event_key = f"issue:{key}:{occurrence}:opened:{user_id}"
                    if _event(conn, key=event_key, kind="opened", issue_key=key,
                              user_id=user_id,
                              notice=_notice(problem, kind="opened", user_id=user_id,
                                             after_collection=after_collection)):
                        if problem["severity"] == "critical":
                            opened_critical = True
                if admins:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE public.finance_health_monitor_issues
                            SET notified = true WHERE issue_key = %s
                        """, (key,))
        for key, old in previous.items():
            if not old["active"] or key in problems:
                continue
            close, recovered = _verified_resolution(old, assessment, evidence)
            if not close:
                continue
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE public.finance_health_monitor_issues
                    SET active = false, resolved_at = %s WHERE issue_key = %s
                """, (instant, key))
            if recovered and old["notified"]:
                for user_id in admins:
                    _event(conn, key=f"issue:{key}:{old['occurrence']}:resolved:{user_id}",
                           kind="resolved", issue_key=key, user_id=user_id,
                           notice=_notice(old, kind="resolved", user_id=user_id,
                                          after_collection=after_collection))
        interval = reminder_interval(local_day)
        if interval is not None and not after_collection:
            target = upcoming_month(local_day)
            window_key = f"reminder:{target.isoformat()}:{interval}"
            if opened_critical or (not problems and assessment["readiness"] in {"READY", "WARNING"}):
                _event(conn, key=window_key, kind="suppressed",
                       target_month=target, days_before=interval)
            else:
                # A global event marks the window processed, including a run
                # without currently active admin recipients.
                if _event(conn, key=window_key, kind="reminder",
                          target_month=target, days_before=interval):
                    for user_id in admins:
                        _event(conn, key=f"{window_key}:{user_id}", kind="reminder",
                               target_month=target, days_before=interval,
                               user_id=user_id,
                               notice=_reminder_notice(user_id=user_id,
                                                       days_before=interval,
                                                       target=target,
                                                       assessment=assessment,
                                                       problems=problems))
        summary = _summary(assessment, problems)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE public.finance_health_monitor_state
                SET assessed_on = %s, assessed_at = %s, evidence_hash = %s,
                    previous_readiness = readiness, readiness = %s, summary = %s
                WHERE id = true
            """, (local_day, instant, evidence_hash, assessment["readiness"],
                  Json(summary)))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def get_monitoring_status() -> dict:
    """Return bounded, sanitized history for the admin-only Finance route."""
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT assessed_on, assessed_at, readiness, previous_readiness, summary
                FROM public.finance_health_monitor_state WHERE id = true
            """)
            state = cur.fetchone()
            cur.execute("""
                SELECT issue_key, source_type, source_id, code, severity,
                       occurrence, opened_at, last_seen_at
                FROM public.finance_health_monitor_issues WHERE active = true
                ORDER BY severity, opened_at DESC
            """)
            issues = _rows(cur)
            cur.execute("""
                SELECT event_type, issue_key, target_month, days_before, created_at
                FROM public.finance_health_monitor_events
                ORDER BY created_at DESC LIMIT 50
            """)
            events = _rows(cur)
        conn.commit()
        return {
            "assessed_on": state[0] if state else None,
            "assessed_at": state[1] if state else None,
            "readiness": state[2] if state else None,
            "previous_readiness": state[3] if state else None,
            "summary": state[4] if state else {},
            "outstanding_issues": issues,
            "history": events,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)

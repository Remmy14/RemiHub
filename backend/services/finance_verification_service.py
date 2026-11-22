"""Bounded, verification-only Plaid checks shared by scheduled and manual runs.

Only cached inventory and Item status are checked. No account or snapshot writer
is called, and a cached response never proves a fresh balance can be obtained.
"""

from datetime import date, datetime, timedelta, timezone
import json
import logging
from uuid import uuid4
from zoneinfo import ZoneInfo

from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.item_get_request import ItemGetRequest
from psycopg2.extras import Json

from backend.database.database import get_db_conn, put_db_conn
from backend.tasks import finance_worker as worker


logger = logging.getLogger(__name__)
INTERVAL = timedelta(hours=5, minutes=30)
LEASE = timedelta(minutes=20)
REQUEST_TIMEOUT = 15
MAX_RUN = timedelta(minutes=15)
AUTH_CODES = frozenset({"ITEM_LOGIN_REQUIRED", "INVALID_ACCESS_TOKEN"})
NEW_YORK = ZoneInfo("America/New_York")


def _claim(now: datetime, owner: str, *, reminder_day: bool = False,
           local_day: date | None = None,
           replay_generation_out: dict | None = None,
           require_pending: bool = False) -> bool:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.finance_plaid_verification_lease (id)
                VALUES (true) ON CONFLICT (id) DO NOTHING
            """)
            claim_sql = """
                UPDATE public.finance_plaid_verification_lease
                SET owner = %s, expires_at = %s, started_at = %s,
                    run_trigger = 'scheduled', run_id = NULL,
                    monitoring_pending = monitoring_pending OR scheduled_pending,
                    monitoring_generation = monitoring_generation +
                        CASE WHEN scheduled_pending THEN 1 ELSE 0 END,
                    monitoring_retry_at = CASE WHEN scheduled_pending THEN %s
                        ELSE monitoring_retry_at END
                WHERE id = true AND (expires_at IS NULL OR expires_at <= %s)
                  AND (NOT %s OR scheduled_pending)
                  AND (scheduled_pending OR started_at IS NULL OR started_at <= %s OR
                       (%s AND (started_at AT TIME ZONE 'America/New_York')::date < %s))
                RETURNING scheduled_pending, monitoring_generation
            """
            claim_args = (owner, now + LEASE, now, now + LEASE, now, require_pending,
                          now - INTERVAL,
                          reminder_day, local_day)
            cur.execute(claim_sql, claim_args)
            claim_row = cur.fetchone()
            claimed = claim_row is not None
            if not claimed:
                # A due worker cycle that collides with a manual check is
                # replayed after that check; manual work does not consume it.
                cur.execute("""
                    UPDATE public.finance_plaid_verification_lease
                    SET scheduled_pending = true
                    WHERE id = true AND run_trigger = 'manual'
                      AND owner IS NOT NULL AND expires_at > %s
                      AND (started_at IS NULL OR started_at <= %s OR
                           (%s AND (started_at AT TIME ZONE 'America/New_York')::date < %s))
                """, (now, now - INTERVAL, reminder_day, local_day))
                if cur.rowcount == 0:
                    # The manual owner may have released between the two
                    # statements; retry once before leaving this worker cycle.
                    cur.execute(claim_sql, claim_args)
                    claim_row = cur.fetchone()
                    claimed = claim_row is not None
        conn.commit()
        if claim_row and claim_row[0] and replay_generation_out is not None:
            replay_generation_out["generation"] = claim_row[1]
        return claimed
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _release(owner: str) -> bool:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT scheduled_pending, run_trigger FROM public.finance_plaid_verification_lease
                WHERE id = true AND owner = %s FOR UPDATE
            """, (owner,))
            row = cur.fetchone()
            cur.execute("""
                UPDATE public.finance_plaid_verification_lease
                SET owner = NULL, expires_at = NULL, run_trigger = NULL, run_id = NULL,
                    scheduled_pending = CASE WHEN run_trigger = 'scheduled'
                        THEN false ELSE scheduled_pending END
                WHERE id = true AND owner = %s
            """, (owner,))
        conn.commit()
        return bool(row and row[0] and row[1] == "manual")
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _error_code(exc: Exception) -> str | None:
    # Plaid ApiException.body is structured JSON. Never persist or log the
    # exception text: it can contain provider details or credentials.
    try:
        payload = json.loads(getattr(exc, "body", "") or "{}")
        return payload.get("error_code") if isinstance(payload, dict) else None
    except (TypeError, ValueError):
        return None


def _check_item(item: dict, expected: set[str], *, check_item_status: bool = False) -> dict:
    result = {"outcome": "UNKNOWN", "connection_status": "UNKNOWN",
              "inventory_status": "UNKNOWN", "available_account_ids": [],
              "missing_account_ids": [], "invalid_balance_ids": [],
              "method": "item_get+accounts_get" if check_item_status else "accounts_get"}
    try:
        client = worker.get_plaid_client(item.get("environment") or "production")
        token = worker.decrypt_access_token(item["access_token_encrypted"])
        if check_item_status:
            result["method"] = "item_get"
            response = client.item_get(ItemGetRequest(access_token=token),
                                       _request_timeout=REQUEST_TIMEOUT).to_dict()
            status = response.get("item") if isinstance(response, dict) else None
            if not isinstance(status, dict):
                result["outcome"] = "TRANSIENT"
                return result
            reported = status.get("error")
            if isinstance(reported, dict) and reported.get("error_code") in AUTH_CODES:
                result.update(outcome="AUTH_REQUIRED", connection_status="AUTH_REQUIRED")
                return result
            if reported:
                result["outcome"] = "TRANSIENT"
                return result
            result["connection_status"] = "VERIFIED"
            result["method"] = "item_get+accounts_get"
        accounts = client.accounts_get(AccountsGetRequest(access_token=token),
                                       _request_timeout=REQUEST_TIMEOUT).to_dict()
        inventory = accounts.get("accounts") if isinstance(accounts, dict) else None
        if not isinstance(inventory, list) or any(not isinstance(a, dict) or
                not isinstance(a.get("account_id"), str) or not a["account_id"] for a in inventory):
            result["outcome"] = "TRANSIENT"
            return result
        ids = [a["account_id"] for a in inventory]
        if len(ids) != len(set(ids)):
            result["outcome"] = "TRANSIENT"
            return result
        result["available_account_ids"] = ids
        result["missing_account_ids"] = sorted(expected - set(ids))
        for account in inventory:
            if account["account_id"] not in expected:
                continue
            try:
                worker._required_plaid_balance(account)
            except ValueError:
                result["invalid_balance_ids"].append(account["account_id"])
        result["inventory_status"] = "MISSING" if result["missing_account_ids"] else "AVAILABLE"
        result["outcome"] = "VERIFIED"
    except Exception as exc:
        if _error_code(exc) in AUTH_CODES:
            result.update(outcome="AUTH_REQUIRED", connection_status="AUTH_REQUIRED")
        else:
            result["outcome"] = "TRANSIENT"
        logger.warning("Plaid lightweight verification failed: classification=%s", result["outcome"])
    return result


def _renew(owner: str) -> bool:
    """Fence a live executor and extend its lease without holding it over I/O."""
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE public.finance_plaid_verification_lease
                SET expires_at = clock_timestamp() + interval '20 minutes'
                WHERE id = true AND owner = %s AND expires_at > clock_timestamp()
                RETURNING id
            """, (owner,))
            active = cur.fetchone() is not None
        conn.commit()
        return active
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _persist(item_id: str, result: dict, now: datetime, *, owner: str | None = None) -> None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            if owner is not None:
                cur.execute("""
                    SELECT owner FROM public.finance_plaid_verification_lease
                    WHERE id = true AND owner = %s AND expires_at > clock_timestamp()
                    FOR UPDATE
                """, (owner,))
                if cur.fetchone() is None:
                    raise RuntimeError("Finance verification lease lost")
            cur.execute("""
                INSERT INTO public.finance_plaid_verification
                    (plaid_item_id, attempted_at, succeeded_at, method, outcome,
                     connection_status, inventory_status, available_account_ids,
                     missing_account_ids, invalid_balance_ids, failure_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (plaid_item_id) DO UPDATE SET
                    attempted_at = EXCLUDED.attempted_at,
                    method = EXCLUDED.method,
                    succeeded_at = COALESCE(EXCLUDED.succeeded_at,
                                             public.finance_plaid_verification.succeeded_at),
                    outcome = EXCLUDED.outcome,
                    connection_status = CASE WHEN EXCLUDED.outcome = 'TRANSIENT'
                            OR (public.finance_plaid_verification.connection_status = 'AUTH_REQUIRED'
                                AND EXCLUDED.method = 'accounts_get'
                                AND EXCLUDED.connection_status = 'UNKNOWN')
                        THEN public.finance_plaid_verification.connection_status
                        ELSE EXCLUDED.connection_status END,
                    inventory_status = CASE WHEN EXCLUDED.outcome = 'TRANSIENT'
                        THEN public.finance_plaid_verification.inventory_status
                        ELSE EXCLUDED.inventory_status END,
                    available_account_ids = CASE WHEN EXCLUDED.outcome = 'TRANSIENT'
                        THEN public.finance_plaid_verification.available_account_ids
                        ELSE EXCLUDED.available_account_ids END,
                    missing_account_ids = CASE WHEN EXCLUDED.outcome = 'TRANSIENT'
                        THEN public.finance_plaid_verification.missing_account_ids
                        ELSE EXCLUDED.missing_account_ids END,
                    invalid_balance_ids = CASE WHEN EXCLUDED.outcome = 'TRANSIENT'
                        THEN public.finance_plaid_verification.invalid_balance_ids
                        ELSE EXCLUDED.invalid_balance_ids END,
                    failure_at = CASE WHEN public.finance_plaid_verification.connection_status = 'AUTH_REQUIRED'
                            AND EXCLUDED.method = 'accounts_get'
                            AND EXCLUDED.connection_status = 'UNKNOWN'
                        THEN public.finance_plaid_verification.failure_at
                        ELSE EXCLUDED.failure_at END
            """, (str(item_id), now, now if result["outcome"] == "VERIFIED" else None,
                  result["method"], result["outcome"], result["connection_status"],
                  result["inventory_status"], Json(result["available_account_ids"]),
                  Json(result["missing_account_ids"]), Json(result["invalid_balance_ids"]),
                  now if result["outcome"] != "VERIFIED" else None))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _run_items(owner: str, *, started_at: datetime, on_result=None) -> dict:
    """Use the scheduled checker for both triggers; report only sanitized facts."""
    items = worker.get_enabled_plaid_items()
    configured = worker._get_configured_plaid_accounts()
    check_item_status = str(worker.finance_config.get(
        "health_plaid_item_get_enabled", "false")).lower() == "true"
    by_label = {}
    for item in items:
        by_label.setdefault(item["label"], []).append(item)
    evaluated_accounts = sum(
        1 for account in configured if account["include_in_net_worth"]
        and len(by_label.get(account["institution_label"], [])) == 1
    )
    results = []
    for item in items:
        if datetime.now(timezone.utc) >= started_at + MAX_RUN:
            break
        if not _renew(owner):
            raise RuntimeError("Finance verification lease lost")
        matching = by_label[item["label"]]
        expected = {a["source_account_id"] for a in configured
                    if a["include_in_net_worth"] and a["institution_label"] == item["label"]}
        result = _check_item(item, expected if len(matching) == 1 else set(),
                             check_item_status=check_item_status)
        if len(matching) != 1:
            result.update(outcome="UNKNOWN", inventory_status="UNKNOWN",
                          available_account_ids=[], missing_account_ids=[])
        checked_at = datetime.now(timezone.utc)
        _persist(item["id"], result, checked_at, owner=owner)
        if result["connection_status"] == "AUTH_REQUIRED":
            issue_code, explanation, action = (
                "PLAID_REAUTH_REQUIRED", "The connection requires authentication.",
                "Reconnect this institution.")
        elif result["missing_account_ids"]:
            issue_code, explanation, action = (
                "PLAID_ACCOUNT_MISSING", "An included account is absent from cached inventory.",
                "Review the institution's account access.")
        elif result["invalid_balance_ids"]:
            issue_code, explanation, action = (
                "PLAID_BALANCE_MISSING", "A cached balance is unavailable for an included account.",
                "Review the account before the next snapshot.")
        elif result["outcome"] in {"TRANSIENT", "UNKNOWN"}:
            issue_code, explanation, action = (
                None, "The permitted check could not establish current availability.",
                "Retry after the cooldown or check the institution.")
        else:
            issue_code, explanation, action = (
                None, "Cached account inventory was checked; live balance collection was not tested.",
                None)
        public = {
            "connection_id": str(item["id"]), "label": str(item["label"]),
            "outcome": result["outcome"], "connection_status": result["connection_status"],
            "inventory_status": result["inventory_status"], "method": result["method"],
            "verification_depth": "item_status_and_cached_inventory" if
                "item_get" in result["method"] else "cached_inventory",
            "checked_at": checked_at.isoformat(), "included_accounts": len(expected),
            "available_accounts": len(expected & set(result["available_account_ids"])),
            "missing_accounts": len(result["missing_account_ids"]),
            "invalid_cached_balances": len(result["invalid_balance_ids"]),
            "live_balance_tested": False,
            "issue_code": issue_code, "explanation": explanation,
            "recommended_action": action,
        }
        results.append(public)
        if on_result is not None:
            on_result(public, len(items))
    return {"enabled_connections": len(items), "results": results,
            "included_accounts": evaluated_accounts,
            "complete": len(results) == len(items)}


def verify_enabled_items(*, now: datetime | None = None,
                         replay_generation_out: dict | None = None,
                         require_pending: bool = False) -> bool:
    """Check each enabled Item once; scheduled eligibility is independent of manual runs."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("verification clock must be timezone aware")
    local_day = now.astimezone(NEW_YORK).date()
    next_month = date(local_day.year + (local_day.month == 12),
                      local_day.month % 12 + 1, 1)
    reminder_day = (next_month - local_day).days in (3, 1)
    owner = str(uuid4())
    claim_options = ({"replay_generation_out": replay_generation_out}
                     if replay_generation_out is not None else {})
    if require_pending:
        claim_options["require_pending"] = True
    if not _claim(now, owner, reminder_day=reminder_day, local_day=local_day,
                  **claim_options):
        return False
    try:
        _run_items(owner, started_at=datetime.now(timezone.utc))
        return True
    finally:
        _release(owner)

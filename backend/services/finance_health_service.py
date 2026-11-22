"""Read-only Finance health assessment from stored evidence.

Plaid connection success is item-wide. Only a completed snapshot establishes
which accounts and balances were actually collected; this service never calls
Plaid, KBB, Zillow, or any Finance writer.
"""

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import logging
import re

from backend.database.database import get_db_conn, put_db_conn
from backend.services import finance_service
from backend.tasks import finance_worker as worker


logger = logging.getLogger(__name__)

# Defaults are intentionally source-specific. Existing Finance INI configuration
# can override them without requiring new settings or a migration.
DEFAULT_FRESHNESS_DAYS = {"plaid": 7, "manual": 90, "vehicle": 180}
CONFIG_KEYS = {
    "plaid": "health_plaid_freshness_days",
    "manual": "health_manual_review_days",
    "vehicle": "health_vehicle_review_days",
}


def _issue(code: str, description: str, action: str | None = None) -> dict:
    return {"code": code, "description": description, "action": action}


def _read_rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [column[0] for column in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        put_db_conn(conn)


def read_evidence() -> dict:
    """Fetch independent evidence sources so one unavailable table is reported."""
    evidence = {"errors": []}
    sources = {
        "accounts": finance_service.get_finance_accounts,
        "items": lambda: _read_rows("""
            SELECT id, label, enabled, last_success_at, last_error
            FROM finance_plaid_items
        """),
        "manual": lambda: _read_rows("""
            SELECT id, label, asset_category, current_value, as_of_date,
                   updated_at, include_in_net_worth, active, valuation_key
            FROM finance_manual_assets WHERE active = true
        """),
        "snapshot": lambda: _read_rows("""
            SELECT id, snapshot_month, status, created_at
            FROM finance_snapshots ORDER BY snapshot_month DESC LIMIT 1
        """),
    }
    for name, load in sources.items():
        try:
            evidence[name] = load()
        except Exception:
            logger.exception("Finance health evidence read failed: source=%s", name)
            evidence[name] = None
            evidence["errors"].append(_issue("EVIDENCE_UNAVAILABLE", f"{name} evidence is unavailable.", "Retry preflight or investigate storage."))
    snapshot = evidence["snapshot"]
    evidence["snapshot"] = snapshot[0] if snapshot else None
    evidence["snapshot_items"] = None
    if evidence["snapshot"]:
        try:
            evidence["snapshot_items"] = _read_rows("""
                SELECT source, source_account_id, valuation_key, label, value,
                       asset_category, include_in_net_worth
                FROM finance_snapshot_items WHERE snapshot_id = %s
                  AND include_in_net_worth = true
            """, (evidence["snapshot"]["id"],))
        except Exception:
            logger.exception("Finance health snapshot items read failed")
            evidence["errors"].append(_issue("EVIDENCE_UNAVAILABLE", "Snapshot item evidence is unavailable.", "Retry preflight or investigate storage."))
    return evidence


def _policy(config: dict | None) -> dict[str, int]:
    config = config if config is not None else worker.finance_config
    policy = {}
    for source, default in DEFAULT_FRESHNESS_DAYS.items():
        raw = config.get(CONFIG_KEYS[source], default)
        try:
            days = int(raw)
            if days < 1:
                raise ValueError("days must be positive")
        except (TypeError, ValueError):
            logger.warning("Invalid Finance health freshness setting: source=%s", source)
            days = default
        policy[source] = days
    return policy


def _datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _result(*, identifier: str, name: str, category: str | None, source: str,
            included: bool, health: str, freshness: str, last_update,
            issues: list[dict]) -> dict:
    return {
        "account_id": identifier, "display_name": name, "category": category,
        "source_type": source, "included_in_net_worth": included,
        "health": health, "freshness": freshness,
        "last_successful_update": last_update, "issues": issues,
        "explanation": issues[0]["description"] if issues else "Available evidence is current and no known issue exists.",
        "recommended_action": next((issue["action"] for issue in issues if issue["action"]), None),
    }


def _worker_account_failure(connection: dict, account_id: str | None) -> dict | None:
    """Recognize only account failures emitted by the monthly worker.

    A failed accounts_get also causes the worker to append a missing-account
    message. That message alone does not prove an account was absent, so a
    preceding provider/API error must leave the result indeterminate.
    """
    if not isinstance(account_id, str) or not account_id:
        return None
    prefix = f"{connection.get('label')} (connection {connection.get('id')}): "
    error = connection.get("last_error")
    if not isinstance(error, str) or not error.startswith(prefix):
        return None
    detail = error[len(prefix):]
    missing_pattern = r"missing included account [^ ;]+ \([^;]*\)"
    parts = detail.split("; ")
    if all(re.fullmatch(missing_pattern, part) for part in parts):
        own_pattern = rf"missing included account {re.escape(account_id)} \([^;]*\)"
        if any(re.fullmatch(own_pattern, part) for part in parts):
            return _issue("PLAID_ACCOUNT_MISSING", "Required account was absent from the last Plaid collection.", "Review missing account.")
    balance_pattern = (rf"included account {re.escape(account_id)} \([^;]*\): "
                       r"(?:missing|invalid|non-finite) balances\.current")
    if re.fullmatch(balance_pattern, detail):
        return _issue("PLAID_BALANCE_MISSING", "Required account balance was missing or invalid in the last Plaid collection.", "Refresh account data.")
    return None


def assess_evidence(evidence: dict, *, now: datetime | None = None,
                    config: dict | None = None, completeness_check=None,
                    calendar_date: date | None = None) -> dict:
    """Classify supplied stored evidence. Safe for callers outside HTTP.

    `completeness_check` is the worker's existing read-only completeness
    validator in production. Tests may inject it with in-memory inventory.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    policy = _policy(config)
    completeness_check = completeness_check or worker._complete_snapshot_items
    assessment_date = calendar_date or now.date()
    month = worker.get_snapshot_month(assessment_date)
    snapshot = evidence.get("snapshot")
    snapshot_current = bool(snapshot and _date(snapshot.get("snapshot_month")) == month)
    snapshot_success = snapshot_current and snapshot.get("status") == "success"
    snapshot_items = evidence.get("snapshot_items")
    snapshot_created = _datetime(snapshot.get("created_at")) if snapshot else None
    current_items = snapshot_items if snapshot_current and snapshot_items is not None else None
    plaid_snapshot = {row.get("source_account_id"): row for row in current_items or [] if row.get("source") == "plaid"}
    live_valuation_keys = {row.get("valuation_key") for row in current_items or []
                           if row.get("source") in ("kbb", "zillow") and row.get("valuation_key")}
    configured_valuation_keys = worker._configured_valuation_keys()

    items = evidence.get("items")
    accounts = evidence.get("accounts")
    manual = evidence.get("manual")
    enabled_by_label: dict[str, list[dict]] = {}
    for item in items or []:
        if item.get("enabled"):
            enabled_by_label.setdefault(item.get("label"), []).append(item)

    results = []
    blockers = []
    warnings = list(evidence.get("errors") or [])
    unknown_evidence = bool(warnings) or any(evidence.get(key) is None for key in ("accounts", "items", "manual"))
    for account in accounts or []:
        if account.get("source") != "plaid":
            continue
        identifier = str(account.get("id") or account.get("source_account_id") or "unknown-plaid-account")
        name = account.get("display_name") or account.get("name") or "Unknown Plaid account"
        included = bool(account.get("include_in_net_worth"))
        matches = enabled_by_label.get(account.get("institution_label"), [])
        connection = matches[0] if len(matches) == 1 else None
        connection_success = _datetime(connection.get("last_success_at")) if connection else None
        last_success = None
        if snapshot_success and account.get("source_account_id") in plaid_snapshot and snapshot_created:
            # The successful snapshot proves this account was collected, even
            # when legacy item metadata has no per-connection timestamp.
            last_success = snapshot_created
        # An item can succeed during discovery while a specific included
        # account has not supplied a usable balance. Item success therefore
        # cannot make account freshness CURRENT. It can only establish that
        # the connection itself is stale when no account proof exists.
        if last_success:
            freshness = "STALE" if now - last_success > timedelta(days=policy["plaid"]) else "CURRENT"
        elif connection_success and now - connection_success > timedelta(days=policy["plaid"]):
            freshness = "STALE"
        else:
            freshness = "UNKNOWN"
        issues = []
        health = "HEALTHY"
        if included and items is not None and not matches:
            issues.append(_issue("PLAID_CONNECTION_MISSING", "Required account has no enabled Plaid connection.", "Review missing account or reconnect the institution."))
            health = "BLOCKED"
        elif len(matches) > 1 or not account.get("source_account_id") or not account.get("institution_label"):
            issues.append(_issue("PLAID_OWNERSHIP_UNKNOWN", "Account connection ownership cannot be established.", "Review account inventory."))
            health = "UNKNOWN"
        elif connection and connection.get("last_error"):
            # The stored field is free-form text; expose only a safe classification.
            if "ITEM_LOGIN_REQUIRED" in str(connection["last_error"]).upper():
                issues.append(_issue("PLAID_REAUTH_REQUIRED", "Plaid connection requires reauthorization.", "Reauthorize Plaid connection."))
                health = "BLOCKED" if included else "WARNING"
            elif included and (worker_failure := _worker_account_failure(connection, account.get("source_account_id"))):
                issues.append(worker_failure)
                health = "BLOCKED"
            else:
                issues.append(_issue("PLAID_COLLECTION_ERROR", "Plaid connection has a collection error; its cause is not known to be authentication.", "Investigate collection error."))
                health = "WARNING"
        if included and current_items is not None:
            collected = plaid_snapshot.get(account.get("source_account_id"))
            if collected is None:
                issues.append(_issue("PLAID_ACCOUNT_MISSING", "Required account is absent from the latest current-period collection.", "Review missing account."))
                health = "BLOCKED"
            else:
                try:
                    worker._snapshot_value(collected.get("value"))
                except ValueError:
                    issues.append(_issue("PLAID_BALANCE_MISSING", "Required account balance is missing or invalid.", "Refresh account data."))
                    health = "BLOCKED"
        elif health == "HEALTHY" and account.get("source_account_id") not in plaid_snapshot:
            issues.append(_issue("PLAID_ACCOUNT_UNVERIFIED", "Stored data does not verify this account's current balance or presence.", "Refresh account data when available."))
            health = "UNKNOWN"
        if freshness == "STALE":
            description = ("Verified account collection is older than its freshness policy."
                           if last_success else "Plaid connection has not succeeded within its freshness policy.")
            issues.append(_issue("PLAID_DATA_STALE", description, "Refresh account data."))
            if health == "HEALTHY" or (health == "UNKNOWN" and
                                       all(issue["code"] == "PLAID_ACCOUNT_UNVERIFIED" for issue in issues[:-1])):
                health = "WARNING"
        elif freshness == "UNKNOWN" and health == "HEALTHY":
            issues.append(_issue("PLAID_FRESHNESS_UNKNOWN", "Account collection freshness cannot be established from stored evidence.", "Review connection history."))
            health = "UNKNOWN"
        if health == "BLOCKED" and included:
            blockers.extend(issue for issue in issues if issue["code"] in {
                "PLAID_CONNECTION_MISSING", "PLAID_REAUTH_REQUIRED", "PLAID_ACCOUNT_MISSING", "PLAID_BALANCE_MISSING"})
        elif health in ("WARNING", "UNKNOWN"):
            warnings.extend(issues)
        results.append(_result(identifier=identifier, name=name,
                               category=account.get("asset_category") or account.get("type"),
                               source="plaid", included=included, health=health,
                               freshness=freshness, last_update=last_success, issues=issues))

    for asset in manual or []:
        identifier = str(asset.get("id") or "unknown-manual-account")
        source = "vehicle" if asset.get("asset_category") == "vehicle" else "manual"
        as_of = _date(asset.get("as_of_date"))
        freshness = "UNKNOWN" if as_of is None or as_of > assessment_date else ("STALE" if (assessment_date - as_of).days > policy[source] else "CURRENT")
        included = bool(asset.get("include_in_net_worth"))
        issues = []
        health = "HEALTHY"
        try:
            worker._snapshot_value(asset.get("current_value"))
        except ValueError:
            key = asset.get("valuation_key")
            if key and key in configured_valuation_keys and (not snapshot_success or key in live_valuation_keys):
                # A configured live source can replace this fallback. Its
                # availability at the next collection is not known here.
                issues.append(_issue("VALUATION_FALLBACK_INVALID", "Manual valuation fallback is invalid; a configured live source may replace it.", "Update manual valuation fallback."))
                health = "WARNING"
            else:
                issues.append(_issue("MANUAL_VALUE_MISSING", "Manual valuation is missing or invalid.", "Update manual valuation."))
                health = "BLOCKED" if included else "WARNING"
        if freshness == "STALE":
            issues.append(_issue("MANUAL_REVIEW_DUE", "Manual valuation is due for review.", "Review or update manual valuation."))
            if health == "HEALTHY":
                health = "WARNING"
        elif freshness == "UNKNOWN":
            issues.append(_issue("MANUAL_FRESHNESS_UNKNOWN", "Valuation date is unavailable; freshness cannot be determined.", "Review manual valuation date."))
            if health == "HEALTHY":
                health = "UNKNOWN"
        if health == "BLOCKED" and included:
            blockers.extend(issue for issue in issues if issue["code"] == "MANUAL_VALUE_MISSING")
        elif health in ("WARNING", "UNKNOWN"):
            warnings.extend(issues)
        results.append(_result(identifier=identifier, name=asset.get("label") or "Unknown manual account",
                               category=asset.get("asset_category"), source=source,
                               included=included, health=health, freshness=freshness,
                               last_update=as_of, issues=issues))

    fallback_keys = {asset.get("valuation_key") for asset in manual or []
                     if asset.get("valuation_key") and asset.get("include_in_net_worth")}
    usable_fallback_keys = set()
    for asset in manual or []:
        if not asset.get("valuation_key") or not asset.get("include_in_net_worth"):
            continue
        try:
            worker._snapshot_value(asset.get("current_value"))
            usable_fallback_keys.add(asset["valuation_key"])
        except ValueError:
            pass
    for value in current_items or []:
        if value.get("source") not in ("kbb", "zillow") or value.get("valuation_key") in fallback_keys:
            continue
        key = value.get("valuation_key") or value.get("source_account_id")
        category = value.get("asset_category")
        source = "vehicle" if category == "vehicle" else "valuation"
        issues = []
        health = "HEALTHY" if snapshot_success and snapshot_created else "UNKNOWN"
        freshness = "CURRENT" if health == "HEALTHY" else "UNKNOWN"
        try:
            worker._snapshot_value(value.get("value"))
        except ValueError:
            issues.append(_issue("VALUATION_VALUE_MISSING", "Recorded valuation is missing or invalid.", "Review asset valuation."))
            health = "BLOCKED"
            blockers.extend(issues)
        if health == "UNKNOWN":
            issues.append(_issue("VALUATION_FRESHNESS_UNKNOWN", "A complete current valuation cannot be established from stored evidence.", "Review valuation history."))
            warnings.extend(issues)
        results.append(_result(identifier=str(key or "unknown-valuation"),
                               name=value.get("label") or str(key or "Unknown valuation"), category=category,
                               source=source, included=True, health=health,
                               freshness=freshness,
                               last_update=snapshot_created if snapshot_success else None,
                               issues=issues))

    # A complete current-month snapshot is the only persisted proof of Plaid
    # account/balance coverage. Recheck it against today's inclusion and manual
    # fallback inventory using the worker's existing completeness validator.
    complete = False
    required_inventory = (any(a.get("include_in_net_worth") for a in accounts or [] if a.get("source") == "plaid")
                          or any(a.get("include_in_net_worth") for a in manual or [])
                          or bool(configured_valuation_keys) or bool(live_valuation_keys))
    can_check = required_inventory and not unknown_evidence and not blockers and (
        (snapshot_success and current_items is not None) or
        (not any(a.get("include_in_net_worth") for a in accounts or [] if a.get("source") == "plaid")
         and not enabled_by_label and configured_valuation_keys <= usable_fallback_keys)
    )
    if can_check:
        plaid_candidates = [dict(row) for row in current_items or [] if row.get("source") == "plaid"]
        valuation_candidates = [dict(row) for row in current_items or [] if row.get("source") in ("kbb", "zillow")]
        try:
            completeness_check(plaid_candidates, valuation_candidates)
            complete = True
        except ValueError:
            # Worker messages can include account IDs or provider detail.
            blockers.append(_issue("SNAPSHOT_INCOMPLETE", "Current evidence fails the existing snapshot completeness rules.", "Review account inventory and valuations."))
        except Exception:
            logger.exception("Finance health completeness validation failed")
            unknown_evidence = True
            warnings.append(_issue("COMPLETENESS_UNAVAILABLE", "Snapshot completeness could not be evaluated.", "Retry preflight or investigate storage."))
    if snapshot_current and snapshot and snapshot.get("status") != "success":
        warnings.append(_issue("SNAPSHOT_NOT_SUCCESSFUL", "Current-period snapshot is not marked successful.", "Review snapshot collection history."))
    if not complete and not blockers:
        warnings.append(_issue("CURRENT_COLLECTION_UNVERIFIED", "Current account and valuation coverage cannot be verified without a fresh collection.", "Review preflight after the next successful snapshot."))

    readiness = "BLOCKED" if blockers else ("UNKNOWN" if not complete else ("WARNING" if warnings else "READY"))
    counts = Counter(row["health"].lower() for row in results)
    data_as_of = snapshot_created if snapshot_current else None
    if not results:
        readiness = "UNKNOWN" if not blockers else readiness
        complete = False
    explanation = {
        "READY": "Current-period evidence satisfies the existing snapshot completeness rules.",
        "WARNING": "Completeness is established, with nonblocking freshness or maintenance concerns.",
        "BLOCKED": "Verified conditions prevent a complete snapshot.",
        "UNKNOWN": "Stored evidence is insufficient to establish current snapshot readiness.",
    }[readiness]
    return {
        "readiness": readiness, "assessed_at": now, "data_as_of": data_as_of,
        "sufficient_evidence": readiness != "UNKNOWN",
        "counts": {key: counts[key] for key in ("healthy", "warning", "blocked", "unknown")},
        "actionable_issues": blockers + warnings,
        "accounts": results,
        "snapshot": {"readiness": readiness, "snapshot_month": month,
                     "blocking_conditions": blockers, "warnings": warnings,
                     "explanation": explanation},
    }


def assess_finance_health() -> dict:
    return assess_evidence(read_evidence())

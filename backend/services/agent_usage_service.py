"""Administrator-facing, read-only Codex usage queries."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _connection():
    from backend.services.agent_service import get_db_conn, put_db_conn
    return get_db_conn(), put_db_conn


def _release_read_connection(conn, release) -> None:
    # A SELECT starts a transaction on the pooled psycopg connection.
    try:
        conn.rollback()
    finally:
        release(conn)


def _rows(cur):
    names = [column[0] for column in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _unavailable_quota(error_code=None) -> dict:
    return {"status": "unavailable", "source": "codex_app_server_account_rateLimits_read",
            "collected_at": None, "last_attempt_at": None, "freshness_seconds": None,
            "error_code": error_code, "account_id": None, "buckets": [],
            "ordinary_usage_allowed": None, "available_reset_credits": None,
            "metric_statuses": {"absolute_remaining_allocation": "unsupported",
                                "window_start": "unsupported", "windows": "unavailable",
                                "reset_credits": "unavailable", "credits": "unavailable",
                                "plan_type": "unavailable"}}


def _has_usable_window(buckets: list[dict]) -> bool:
    for bucket in buckets:
        for window in bucket.get("windows", []):
            used = window.get("used_percent")
            if window.get("status") == "available" and type(used) is int and 0 <= used <= 100:
                return True
    return False


def _has_usable_credits(buckets: list[dict]) -> bool:
    for bucket in buckets:
        credits = bucket.get("credits")
        if not isinstance(credits, dict):
            continue
        balance = credits.get("balance")
        if (isinstance(balance, str) and bool(balance.strip())) or any(
            type(credits.get(field)) is bool for field in ("has_credits", "unlimited")
        ):
            return True
    return False


def quota() -> dict:
    conn, release = _connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT source, status, payload, collected_at, last_attempt_at, error_code "
                        "FROM agent.codex_quota_snapshots WHERE slot = 'worker'")
            row = _rows(cur)
    except Exception as exc:
        if getattr(exc, "pgcode", None) != "42501":
            raise
        return _unavailable_quota("DatabasePermissionUnavailable")
    finally:
        _release_read_connection(conn, release)
    if not row:
        return _unavailable_quota()
    item = row[0]
    payload = item["payload"] if isinstance(item["payload"], dict) else {}
    collected = item["collected_at"]
    age = max(0, int((datetime.now(timezone.utc) - collected).total_seconds())) if collected else None
    status = item["status"]
    if status == "available" and (age is None or age > 900):
        status = "stale"
    present = "stale" if status in {"stale", "collection_failed"} and collected else status
    if status == "collection_failed" and not collected:
        present = "collection_failed"
    absent = "collection_failed" if status == "collection_failed" else "unavailable"
    buckets = payload.get("buckets", [])
    return {"status": status, "source": item["source"],
            "collected_at": collected, "last_attempt_at": item["last_attempt_at"],
            "freshness_seconds": age, "error_code": item["error_code"],
            "account_id": payload.get("account_id"), "buckets": buckets,
            "ordinary_usage_allowed": payload.get("ordinary_usage_allowed"),
            "available_reset_credits": payload.get("available_reset_credits"),
            "metric_statuses": {"absolute_remaining_allocation": "unsupported",
                                "window_start": "unsupported",
                                "windows": present if _has_usable_window(buckets) else absent,
                                "reset_credits": present if payload.get("available_reset_credits") is not None else absent,
                                "credits": present if _has_usable_credits(buckets) else absent,
                                "plan_type": present if any(b.get("plan_type") for b in buckets) else absent}}


def period(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    end = end or now
    start = start or end - timedelta(days=30)
    if start.tzinfo is None or end.tzinfo is None or start >= end or end - start > timedelta(days=366):
        raise ValueError("Use timezone-aware from/to timestamps spanning at most 366 days")
    return start, end


_TOTALS = """COUNT(*) AS execution_count,
 COUNT(*) FILTER (WHERE usage_status = 'available') AS available_count,
 COUNT(*) FILTER (WHERE usage_status <> 'available') AS unavailable_count,
 SUM(input_tokens) FILTER (WHERE usage_status = 'available') AS input_tokens,
 SUM(output_tokens) FILTER (WHERE usage_status = 'available') AS output_tokens,
 SUM(cached_input_tokens) FILTER (WHERE usage_status = 'available') AS cached_input_tokens,
 SUM(reasoning_tokens) FILTER (WHERE usage_status = 'available') AS reasoning_tokens,
 SUM(total_tokens) FILTER (WHERE usage_status = 'available') AS total_tokens"""


def summary(start: datetime, end: datetime) -> dict:
    conn, release = _connection()
    try:
        with conn.cursor() as cur:
            result = {}
            groups = {"overall": (), "by_stage": ("stage",), "by_card": ("card_id",),
                      "by_model": ("actual_model", "reasoning_effort")}
            for name, columns in groups.items():
                select = ", ".join(columns)
                prefix = select + ", " if select else ""
                group_by = " GROUP BY " + select if select else ""
                cur.execute("SELECT " + prefix + _TOTALS +
                            " FROM agent.codex_execution_usage WHERE recorded_at >= %s AND recorded_at < %s" + group_by,
                            (start, end))
                rows = _rows(cur)
                result[name] = rows[0] if name == "overall" and rows else (rows if name != "overall" else {})
    finally:
        _release_read_connection(conn, release)
    return {"period_start": start, "period_end": end, "token_accounting": result}


def executions(start: datetime, end: datetime, *, card_id=None, limit=50, offset=0) -> dict:
    conn, release = _connection()
    try:
        with conn.cursor() as cur:
            where = "recorded_at >= %s AND recorded_at < %s"
            values = [start, end]
            if card_id is not None:
                where += " AND card_id = %s"
                values.append(card_id)
            cur.execute("SELECT COUNT(*) FROM agent.codex_execution_usage WHERE " + where, values)
            count = cur.fetchone()[0]
            cur.execute("SELECT run_id, attempt_count, turn_id, card_id, card_revision, stage, "
                        "review_attempt, configured_model, actual_model, reasoning_effort, "
                        "input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, total_tokens, "
                        "usage_status, usage_source, started_at, finished_at, recorded_at "
                        "FROM agent.codex_execution_usage WHERE " + where +
                        " ORDER BY recorded_at DESC, run_id DESC, attempt_count DESC, turn_id DESC LIMIT %s OFFSET %s",
                        [*values, limit, offset])
            rows = _rows(cur)
    finally:
        _release_read_connection(conn, release)
    return {"total": count, "items": rows, "limit": limit, "offset": offset}


def configuration() -> list[dict]:
    conn, release = _connection()
    now = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT ON (stage) stage, "
                        "recorded_at FROM agent.codex_execution_usage "
                        "ORDER BY stage, recorded_at DESC, run_id DESC, attempt_count DESC, turn_id DESC")
            latest_attempt = {row["stage"]: row for row in _rows(cur)}
            cur.execute("SELECT DISTINCT ON (stage) stage, actual_model, reasoning_effort, "
                        "recorded_at FROM agent.codex_execution_usage "
                        "WHERE actual_model IS NOT NULL "
                        "ORDER BY stage, recorded_at DESC, run_id DESC, attempt_count DESC, turn_id DESC")
            latest_model = {row["stage"]: row for row in _rows(cur)}
            try:
                cur.execute("SELECT DISTINCT ON (stage) stage, configured_model, "
                            "configured_reasoning_effort, reported_at "
                            "FROM agent.codex_stage_configuration "
                            "ORDER BY stage, reported_at DESC")
                reported = {row["stage"]: row for row in _rows(cur)}
                cur.execute("SELECT DISTINCT ON (stage) stage, configured_model, "
                            "configured_reasoning_effort, reported_at "
                            "FROM agent.codex_stage_configuration "
                            "WHERE reported_at <= %s AND reported_at >= %s "
                            "ORDER BY stage, reported_at DESC",
                            (now, now - timedelta(seconds=180)))
                current = {row["stage"]: row for row in _rows(cur)}
            except Exception as exc:
                if getattr(exc, "pgcode", None) != "42501":
                    raise
                reported = {}
                current = {}
    finally:
        _release_read_connection(conn, release)
    result = []
    for stage in ("planning", "implementation", "review", "deployment"):
        config = reported.get(stage, {})
        timestamp = config.get("reported_at")
        live = current.get(stage, {})
        fresh = bool(live)
        result.append({"stage": stage,
                       "configured_model": live.get("configured_model"),
                       "configured_reasoning_effort": live.get("configured_reasoning_effort"),
                       "last_reported_configured_model": config.get("configured_model"),
                       "last_reported_reasoning_effort": config.get("configured_reasoning_effort"),
                       "configuration_source": "worker_settings" if config else None,
                       "configuration_reported_at": live.get("reported_at") if fresh else timestamp,
                       "actual_model_last_execution": latest_model.get(stage, {}).get("actual_model"),
                       "reasoning_effort_last_execution": latest_model.get(stage, {}).get("reasoning_effort"),
                       "observed_at": latest_attempt.get(stage, {}).get("recorded_at"),
                       "actual_model_observed_at": latest_model.get(stage, {}).get("recorded_at"),
                       "status": "unsupported" if stage == "deployment" else
                                 ("available" if fresh else "stale" if config else "unavailable")})
    return result

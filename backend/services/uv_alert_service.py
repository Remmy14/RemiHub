from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from backend.database.database import get_db_conn, put_db_conn
from backend.models.uv_alert_models import (
    UV_ACTION_IGNORE_TODAY,
    UV_ACTION_NOT_POOL_DAY,
    UV_ACTION_SNOOZE_1_HOUR,
    UV_ACTION_SUNSCREEN_APPLIED,
    VALID_UV_ALERT_ACTIONS,
    UvAlertActionResult,
)

LOCAL_TZ = ZoneInfo("America/New_York")

DEFAULT_ALERT_START_HOUR = 8
DEFAULT_ALERT_END_HOUR = 20


def _now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def _tomorrow_at(hour: int = 7) -> datetime:
    now = _now_local()
    tomorrow = now.date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(hour=hour), tzinfo=LOCAL_TZ)


def ensure_uv_alert_tables(conn=None) -> None:
    new_conn = False
    if conn is None:
        new_conn = True
        conn = get_db_conn()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS uv_alert_settings (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    profile_name TEXT NOT NULL DEFAULT 'default',
                    alert_start_hour INTEGER NOT NULL DEFAULT 8,
                    alert_end_hour INTEGER NOT NULL DEFAULT 20,
                    last_alert_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uv_alert_settings_singleton CHECK (id = 1)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS uv_alert_state (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    suppress_until TIMESTAMPTZ,
                    suppress_reason TEXT,
                    sunscreen_applied_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uv_alert_state_singleton CHECK (id = 1)
                );
            """)
            cur.execute("""
                INSERT INTO uv_alert_settings (id)
                VALUES (1)
                ON CONFLICT (id) DO NOTHING;
            """)
            cur.execute("""
                INSERT INTO uv_alert_state (id)
                VALUES (1)
                ON CONFLICT (id) DO NOTHING;
            """)

        conn.commit()
    finally:
        if new_conn:
            put_db_conn(conn)


def get_uv_alert_settings(conn=None) -> dict:
    new_conn = False
    if conn is None:
        new_conn = True
        conn = get_db_conn()

    try:
        ensure_uv_alert_tables(conn)

        with conn.cursor() as cur:
            cur.execute("""
                SELECT enabled, profile_name, alert_start_hour, alert_end_hour, last_alert_at
                FROM uv_alert_settings
                WHERE id = 1;
            """)
            row = cur.fetchone()

        return {
            "enabled": row[0],
            "profileName": row[1],
            "alertStartHour": row[2],
            "alertEndHour": row[3],
            "lastAlertAt": row[4].isoformat() if row[4] else None,
        }
    finally:
        if new_conn:
            put_db_conn(conn)


def update_uv_alert_settings(
    enabled: bool | None = None,
    profile_name: str | None = None,
    alert_start_hour: int | None = None,
    alert_end_hour: int | None = None,
) -> dict:
    conn = get_db_conn()

    try:
        ensure_uv_alert_tables(conn)
        current = get_uv_alert_settings(conn)

        next_enabled = current["enabled"] if enabled is None else enabled
        next_profile_name = current["profileName"] if profile_name is None else profile_name
        next_alert_start_hour = current["alertStartHour"] if alert_start_hour is None else alert_start_hour
        next_alert_end_hour = current["alertEndHour"] if alert_end_hour is None else alert_end_hour

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE uv_alert_settings
                SET enabled = %s,
                    profile_name = %s,
                    alert_start_hour = %s,
                    alert_end_hour = %s,
                    updated_at = NOW()
                WHERE id = 1;
            """, (
                next_enabled,
                next_profile_name,
                next_alert_start_hour,
                next_alert_end_hour,
            ))

        conn.commit()
        return get_uv_alert_settings(conn)
    finally:
        put_db_conn(conn)


def get_uv_alert_state(conn=None) -> dict:
    new_conn = False
    if conn is None:
        new_conn = True
        conn = get_db_conn()

    try:
        ensure_uv_alert_tables(conn)

        with conn.cursor() as cur:
            cur.execute("""
                SELECT suppress_until, suppress_reason, sunscreen_applied_at
                FROM uv_alert_state
                WHERE id = 1;
            """)
            row = cur.fetchone()

        return {
            "suppressUntil": row[0].isoformat() if row[0] else None,
            "suppressReason": row[1],
            "sunscreenAppliedAt": row[2].isoformat() if row[2] else None,
        }
    finally:
        if new_conn:
            put_db_conn(conn)


def apply_uv_alert_action(action: str) -> dict:
    if action not in VALID_UV_ALERT_ACTIONS:
        raise ValueError(f"Unsupported UV alert action: {action}")

    now = _now_local()
    suppress_until = None
    suppress_reason = action
    sunscreen_applied_at = None

    if action == UV_ACTION_NOT_POOL_DAY:
        suppress_until = _tomorrow_at(7)
    elif action == UV_ACTION_IGNORE_TODAY:
        suppress_until = _tomorrow_at(7)
    elif action == UV_ACTION_SNOOZE_1_HOUR:
        suppress_until = now + timedelta(hours=1)
    elif action == UV_ACTION_SUNSCREEN_APPLIED:
        suppress_until = now + timedelta(minutes=90)
        sunscreen_applied_at = now

    conn = get_db_conn()

    try:
        ensure_uv_alert_tables(conn)

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE uv_alert_state
                SET suppress_until = %s,
                    suppress_reason = %s,
                    sunscreen_applied_at = COALESCE(%s, sunscreen_applied_at),
                    updated_at = NOW()
                WHERE id = 1;
            """, (suppress_until, suppress_reason, sunscreen_applied_at))

        conn.commit()
        state = get_uv_alert_state(conn)

        return UvAlertActionResult(
            action=action,
            suppressUntil=state["suppressUntil"],
            suppressReason=state["suppressReason"],
            sunscreenAppliedAt=state["sunscreenAppliedAt"],
        ).model_dump()
    finally:
        put_db_conn(conn)


def clear_uv_alert_suppression() -> dict:
    conn = get_db_conn()

    try:
        ensure_uv_alert_tables(conn)

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE uv_alert_state
                SET suppress_until = NULL,
                    suppress_reason = NULL,
                    updated_at = NOW()
                WHERE id = 1;
            """)

        conn.commit()
        return get_uv_alert_state(conn)
    finally:
        put_db_conn(conn)


def mark_uv_alert_sent(conn=None) -> None:
    new_conn = False
    if conn is None:
        new_conn = True
        conn = get_db_conn()

    try:
        ensure_uv_alert_tables(conn)

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE uv_alert_settings
                SET last_alert_at = NOW(),
                    updated_at = NOW()
                WHERE id = 1;
            """)

        if new_conn:
            conn.commit()
    finally:
        if new_conn:
            put_db_conn(conn)
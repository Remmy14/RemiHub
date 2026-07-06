from datetime import datetime, time
import logging
import time as time_module
from zoneinfo import ZoneInfo

from backend.database.database import get_db_conn, put_db_conn
from backend.models.uv_alert_models import UvAlertDecision
from backend.notifications.notifications import Notification, insert_notification
from backend.services import uv_alert_service, weather_service

LOCAL_TZ = ZoneInfo("America/New_York")
MODULE_NAME = "UV Monitor"
UV_ALERT_CHECK_INTERVAL_SECONDS = 300

logger = logging.getLogger(__name__)


def _now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def _today_at(hour: int, minute: int = 0) -> datetime:
    now = _now_local()
    return datetime.combine(now.date(), time(hour=hour, minute=minute), tzinfo=LOCAL_TZ)


def _dose_threshold_for_uv(uv_index: float, profile_name: str) -> float | None:
    if uv_index < 3:
        return None

    multiplier = 1.0
    if profile_name == "pool_day":
        multiplier = 0.75
    elif profile_name == "low_alert":
        multiplier = 1.25

    if uv_index >= 8:
        return 25 * multiplier
    if uv_index >= 6:
        return 40 * multiplier
    return 55 * multiplier


def _minimum_repeat_minutes(profile_name: str, uv_index: float) -> int:
    if uv_index >= 8:
        return 30
    if profile_name == "pool_day":
        return 45
    return 60


def get_uv_alert_decision() -> dict:
    uv_alert_service.ensure_uv_alert_tables()

    settings = uv_alert_service.get_uv_alert_settings()
    state = uv_alert_service.get_uv_alert_state()
    now = _now_local()

    if not settings["enabled"]:
        return UvAlertDecision(
            shouldAlert=False,
            reason="disabled",
            profileName=settings["profileName"],
        ).model_dump()

    alert_start = _today_at(settings["alertStartHour"])
    alert_end = _today_at(settings["alertEndHour"])

    if now < alert_start or now > alert_end:
        return UvAlertDecision(
            shouldAlert=False,
            reason="outside_alert_window",
            profileName=settings["profileName"],
        ).model_dump()

    suppress_until = datetime.fromisoformat(state["suppressUntil"]) if state["suppressUntil"] else None
    if suppress_until and now < suppress_until:
        return UvAlertDecision(
            shouldAlert=False,
            reason=f"suppressed:{state['suppressReason']}",
            profileName=settings["profileName"],
            suppressedUntil=suppress_until.isoformat(),
        ).model_dump()

    reading = weather_service.get_latest_weather_reading()
    if not reading or reading.get("uv") is None:
        return UvAlertDecision(
            shouldAlert=False,
            reason="no_uv_reading",
            profileName=settings["profileName"],
        ).model_dump()

    uv_index = float(reading["uv"])
    threshold = _dose_threshold_for_uv(uv_index, settings["profileName"])

    if threshold is None:
        return UvAlertDecision(
            shouldAlert=False,
            reason="uv_below_minimum",
            uv=uv_index,
            profileName=settings["profileName"],
        ).model_dump()

    last_alert_at = datetime.fromisoformat(settings["lastAlertAt"]) if settings["lastAlertAt"] else None
    if last_alert_at:
        minutes_since_last_alert = max((now - last_alert_at).total_seconds() / 60, 0)
    else:
        minutes_since_last_alert = _minimum_repeat_minutes(settings["profileName"], uv_index)

    dose_points = uv_index * minutes_since_last_alert
    min_repeat_minutes = _minimum_repeat_minutes(settings["profileName"], uv_index)

    if minutes_since_last_alert < min_repeat_minutes and uv_index < 8:
        return UvAlertDecision(
            shouldAlert=False,
            reason="cooldown_active",
            uv=uv_index,
            dosePoints=round(dose_points, 2),
            threshold=threshold,
            profileName=settings["profileName"],
        ).model_dump()

    if dose_points < threshold:
        return UvAlertDecision(
            shouldAlert=False,
            reason="dose_below_threshold",
            uv=uv_index,
            dosePoints=round(dose_points, 2),
            threshold=threshold,
            profileName=settings["profileName"],
        ).model_dump()

    return UvAlertDecision(
        shouldAlert=True,
        reason="threshold_met",
        uv=uv_index,
        dosePoints=round(dose_points, 2),
        threshold=threshold,
        profileName=settings["profileName"],
    ).model_dump()


def run_uv_alert_check() -> dict:
    decision = get_uv_alert_decision()

    if not decision["shouldAlert"]:
        return {
            "notificationCreated": False,
            "decision": decision,
        }

    uv_index = decision["uv"]

    notification = Notification(
        title="High UV Alert",
        body=f"Current UV Index is {uv_index:.1f}. Sunburn risk is elevated. Use sunscreen, shade, hats, or limit direct sun.",
        module=MODULE_NAME,
        priority=1,
    )

    conn = get_db_conn()

    try:
        uv_alert_service.ensure_uv_alert_tables(conn)
        insert_notification(notification, conn=conn)
        uv_alert_service.mark_uv_alert_sent(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)

    return {
        "notificationCreated": True,
        "decision": decision,
    }


def uv_alert_loop() -> None:
    while True:
        try:
            result = run_uv_alert_check()
            logger.info("UV alert check result: %s", result)
        except Exception:
            logger.exception("Unexpected error in UV alert loop")

        time_module.sleep(UV_ALERT_CHECK_INTERVAL_SECONDS)


def run_uv_alert_monitor() -> None:
    logger.info("Starting UV alert monitor")
    uv_alert_loop()


if __name__ == "__main__":
    run_uv_alert_monitor()
    
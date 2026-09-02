from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg2.extras import Json

from backend.database.database import get_db_conn, put_db_conn
from backend.notifications.notifications import Notification, insert_notification


logger = logging.getLogger("fitness_notification_worker")

FITNESS_NOTIFICATION_MODULE = "Fitness"
FITNESS_TIMEZONE_ENV = "REMIHUB_FITNESS_TIMEZONE"
FITNESS_MORNING_HOUR_ENV = "REMIHUB_FITNESS_MORNING_HOUR"
FITNESS_EVENING_HOUR_ENV = "REMIHUB_FITNESS_EVENING_HOUR"
DEFAULT_FITNESS_TIMEZONE = "America/New_York"
DEFAULT_MORNING_HOUR = 8
DEFAULT_EVENING_HOUR = 20


def fitness_timezone() -> ZoneInfo:
    name = os.environ.get(FITNESS_TIMEZONE_ENV, DEFAULT_FITNESS_TIMEZONE).strip()
    try:
        return ZoneInfo(name or DEFAULT_FITNESS_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid {FITNESS_TIMEZONE_ENV}: {name}") from exc


def fitness_date(now: datetime | None = None) -> tuple:
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    tz = fitness_timezone()
    local_now = reference.astimezone(tz)
    return local_now.date(), str(tz)


def configured_phase_hour(phase: str) -> int:
    if phase == "morning":
        env_name = FITNESS_MORNING_HOUR_ENV
        default = DEFAULT_MORNING_HOUR
    elif phase == "evening":
        env_name = FITNESS_EVENING_HOUR_ENV
        default = DEFAULT_EVENING_HOUR
    else:
        raise ValueError("phase must be morning or evening")
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        hour = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid {env_name}: {raw}") from exc
    if hour < 0 or hour > 23:
        raise ValueError(f"Invalid {env_name}: {raw}")
    return hour


def eligible_phase(now: datetime | None = None) -> str | None:
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    local_hour = reference.astimezone(fitness_timezone()).hour
    morning_hour = configured_phase_hour("morning")
    evening_hour = configured_phase_hour("evening")
    if local_hour >= evening_hour:
        return "evening"
    if local_hour >= morning_hour:
        return "morning"
    return None


def _rows_to_dicts(cur, rows) -> list[dict]:
    columns = [desc[0] for desc in cur.description]
    return [
        {column: value for column, value in zip(columns, row)}
        for row in rows
    ]


def get_users_with_planned_workouts(conn, *, target_date) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT user_id
            FROM public.fitness_scheduled_workouts
            WHERE scheduled_date = %s
              AND status = 'PLANNED'
            ORDER BY user_id
            """,
            (target_date,),
        )
        return [str(row[0]) for row in cur.fetchall()]


def get_planned_workouts(conn, *, user_id: str, target_date) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT scheduled.id,
                   scheduled.scheduled_date,
                   scheduled.planned_distance_miles,
                   scheduled.planned_duration_seconds,
                   template.name,
                   template.workout_type,
                   (
                       SELECT count(*)
                       FROM public.fitness_lifting_template_exercises AS member
                       WHERE member.template_id = template.id
                   ) AS exercise_count
            FROM public.fitness_scheduled_workouts AS scheduled
            JOIN public.fitness_workout_templates AS template
              ON template.id = scheduled.workout_template_id
            WHERE scheduled.user_id = %s
              AND scheduled.scheduled_date = %s
              AND scheduled.status = 'PLANNED'
            ORDER BY scheduled.created_at, scheduled.id
            """,
            (user_id, target_date),
        )
        return _rows_to_dicts(cur, cur.fetchall())


def workout_summary(workout: dict) -> str:
    if workout["workout_type"] == "RUNNING":
        distance = workout.get("planned_distance_miles")
        return f"{workout['name']} - {float(distance):g} mi"
    if workout["workout_type"] == "CYCLING":
        duration = workout.get("planned_duration_seconds")
        minutes = int(duration or 0) // 60
        return f"{workout['name']} - {minutes:g} min"
    return f"{workout['name']} - {workout.get('exercise_count') or 0} exercises"


def build_notification(*, phase: str, target_date, timezone_name: str, user_id: str, workouts: list[dict]) -> Notification:
    summaries = [workout_summary(workout) for workout in workouts]
    if phase == "morning":
        title = "Today's workout" if len(workouts) == 1 else "Today's workouts"
        body = "; ".join(summaries)
        notification_type = "fitness_daily_workouts"
    else:
        if len(workouts) == 1:
            title = "Workout still incomplete"
            body = f"{workouts[0]['name']} has not been completed."
        else:
            title = f"{len(workouts)} workouts still incomplete"
            body = "; ".join(summaries)
        notification_type = "fitness_incomplete_workouts"
    return Notification(
        title=title,
        body=body,
        module=FITNESS_NOTIFICATION_MODULE,
        priority=1 if phase == "evening" else 0,
        data={
            "type": notification_type,
            "phase": phase,
            "fitness_date": target_date.isoformat(),
            "timezone": timezone_name,
            "user_id": str(user_id),
            "scheduled_workout_ids": ",".join(str(workout["id"]) for workout in workouts),
        },
    )


def process_fitness_notifications_for_user(
    conn,
    *,
    user_id: str,
    target_date,
    timezone_name: str,
    phase: str,
) -> bool:
    if phase not in {"morning", "evening"}:
        raise ValueError("phase must be morning or evening")

    workouts = get_planned_workouts(conn, user_id=user_id, target_date=target_date)
    workout_ids = [str(workout["id"]) for workout in workouts]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.fitness_notification_runs (
                user_id,
                fitness_date,
                phase,
                status,
                scheduled_workout_ids,
                timezone
            )
            VALUES (%s, %s, %s, %s, %s::uuid[], %s)
            ON CONFLICT (user_id, fitness_date, phase) DO NOTHING
            """,
            (
                user_id,
                target_date,
                phase,
                "inserted" if workouts else "no_workouts",
                workout_ids,
                timezone_name,
            ),
        )
        cur.execute(
            """
            SELECT id, status, notification_id
            FROM public.fitness_notification_runs
            WHERE user_id = %s
              AND fitness_date = %s
              AND phase = %s
            FOR UPDATE
            """,
            (user_id, target_date, phase),
        )
        run_id, status, notification_id = cur.fetchone()
        # Existing terminal rows make processing idempotent. New rows inserted
        # in this transaction for workouts have status inserted but no
        # notification_id yet, so they continue to notification insertion.
        if notification_id or status == "no_workouts":
            return False

    if not workouts:
        return False

    notice = build_notification(
        phase=phase,
        target_date=target_date,
        timezone_name=timezone_name,
        user_id=user_id,
        workouts=workouts,
    )
    notification_id = insert_notification(notice, conn=conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.fitness_notification_runs
            SET status = 'inserted',
                notification_id = %s,
                scheduled_workout_ids = %s::uuid[],
                metadata = %s,
                completed_at = CURRENT_TIMESTAMP,
                updated_at = now()
            WHERE id = %s
            """,
            (
                notification_id,
                workout_ids,
                Json({"count": len(workouts)}),
                run_id,
            ),
        )
    return True


def process_fitness_notifications_once(
    *,
    phase: str,
    now: datetime | None = None,
) -> int:
    target_date, timezone_name = fitness_date(now)
    conn = get_db_conn()
    inserted = 0
    try:
        user_ids = get_users_with_planned_workouts(conn, target_date=target_date)
        for user_id in user_ids:
            if process_fitness_notifications_for_user(
                conn,
                user_id=user_id,
                target_date=target_date,
                timezone_name=timezone_name,
                phase=phase,
            ):
                inserted += 1
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        logger.exception("Fitness notification worker error")
        raise
    finally:
        put_db_conn(conn)


def run_fitness_notification_worker():
    logger.info("Fitness notification worker started")
    while True:
        try:
            now = datetime.now(timezone.utc)
            local_date, timezone_name = fitness_date(now)
            phase = eligible_phase(now)
            if phase:
                process_fitness_notifications_once(phase=phase, now=now)
            logger.debug("Fitness notification check complete for %s", local_date)
        except Exception:
            logger.exception("Failed to process Fitness notifications")
        time.sleep(60 * 30)


if __name__ == "__main__":
    run_fitness_notification_worker()

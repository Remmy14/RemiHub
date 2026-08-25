from __future__ import annotations

import os
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.database.database import get_db_conn, put_db_conn
from backend.models.fitness_models import FITNESS_RECURRENCE_MAX_WEEKS

RUNNING_RESULT_COLUMNS = (
    "planned_distance_miles",
    "completed_distance_miles",
    "duration_seconds",
    "notes",
    "created_at",
    "updated_at",
)
FITNESS_TIMEZONE_ENV = "REMIHUB_FITNESS_TIMEZONE"
DEFAULT_FITNESS_TIMEZONE = "America/New_York"


class FitnessNotFoundError(ValueError):
    pass


class FitnessValidationError(ValueError):
    pass


class FitnessConflictError(ValueError):
    pass


def _serialize_value(value):
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _rows_to_dicts(cur, rows) -> list[dict]:
    columns = [desc[0] for desc in cur.description]
    return [
        {column: _serialize_value(value) for column, value in zip(columns, row)}
        for row in rows
    ]


def _row_to_dict(cur, row) -> dict | None:
    if not row:
        return None
    return _rows_to_dicts(cur, [row])[0]


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FitnessValidationError("Invalid decimal value") from exc


def fitness_timezone() -> ZoneInfo:
    name = os.environ.get(FITNESS_TIMEZONE_ENV, DEFAULT_FITNESS_TIMEZONE).strip()
    try:
        return ZoneInfo(name or DEFAULT_FITNESS_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise FitnessValidationError(f"Invalid {FITNESS_TIMEZONE_ENV}: {name}") from exc


def current_fitness_date(now: datetime | None = None) -> date:
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return reference.astimezone(fitness_timezone()).date()


def _normalize_weekdays(weekdays: list[int]) -> list[int]:
    normalized = sorted(int(day) for day in weekdays)
    if len(normalized) == 0:
        raise FitnessValidationError("At least one weekday is required")
    if len(set(normalized)) != len(normalized):
        raise FitnessValidationError("weekdays must be unique")
    if any(day < 1 or day > 7 for day in normalized):
        raise FitnessValidationError("weekdays must use ISO values 1 through 7")
    return normalized


def _recurrence_dates(
    *,
    start_date: date,
    weekdays: list[int],
    duration_weeks: int | None = None,
    end_date: date | None = None,
) -> tuple[date, list[int], int | None, list[date]]:
    normalized_weekdays = _normalize_weekdays(weekdays)
    if duration_weeks is None and end_date is None:
        raise FitnessValidationError("duration_weeks or end_date is required")
    if duration_weeks is not None and duration_weeks < 1:
        raise FitnessValidationError("duration_weeks must be at least 1")
    if duration_weeks is not None and duration_weeks > FITNESS_RECURRENCE_MAX_WEEKS:
        raise FitnessValidationError(
            f"duration_weeks cannot exceed {FITNESS_RECURRENCE_MAX_WEEKS}"
        )
    canonical_end = end_date
    if duration_weeks is not None:
        canonical_end = start_date + timedelta(days=(7 * duration_weeks) - 1)
        if end_date is not None and end_date != canonical_end:
            raise FitnessValidationError("end_date conflicts with duration_weeks")
    if canonical_end is None or canonical_end < start_date:
        raise FitnessValidationError("end_date must be on or after start_date")
    max_end = start_date + timedelta(days=(7 * FITNESS_RECURRENCE_MAX_WEEKS) - 1)
    if canonical_end > max_end:
        raise FitnessValidationError(
            f"Recurring schedule cannot exceed {FITNESS_RECURRENCE_MAX_WEEKS} weeks"
        )
    dates = []
    current = start_date
    while current <= canonical_end:
        if current.isoweekday() in normalized_weekdays:
            dates.append(current)
        current += timedelta(days=1)
    if not dates:
        raise FitnessValidationError("Recurring schedule would create zero workouts")
    return canonical_end, normalized_weekdays, duration_weeks, dates


def _recurrence_fingerprint(
    *,
    workout_template_id: str,
    start_date: date,
    end_date: date,
    duration_weeks: int | None,
    weekdays: list[int],
) -> str:
    return json.dumps(
        {
            "duration_weeks": duration_weeks,
            "end_date": end_date.isoformat(),
            "start_date": start_date.isoformat(),
            "weekdays": weekdays,
            "workout_template_id": str(workout_template_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _workout_template_select() -> str:
    return """
        SELECT template.id,
               template.user_id,
               template.name,
               template.workout_type AS type,
               template.notes,
               template.active,
               running.planned_distance_miles,
               template.created_at,
               template.updated_at
        FROM public.fitness_workout_templates AS template
        LEFT JOIN public.fitness_running_workout_templates AS running
          ON running.template_id = template.id
    """


def _scheduled_select() -> str:
    return """
        SELECT scheduled.id,
               scheduled.user_id,
               scheduled.workout_template_id,
               scheduled.plan_instance_id,
               scheduled.scheduled_date,
               scheduled.original_scheduled_date,
               scheduled.status,
               scheduled.replacement_scheduled_workout_id,
               scheduled.recurring_series_id,
               scheduled.planned_distance_miles,
               template.name AS workout_name,
               template.workout_type AS type,
               series.weekdays AS recurring_series_weekdays,
               series.status AS recurring_series_status,
               plan_template.name AS plan_template_name,
               result.planned_distance_miles AS result_planned_distance_miles,
               result.completed_distance_miles,
               result.duration_seconds,
               result.notes AS result_notes,
               result.created_at AS result_created_at,
               result.updated_at AS result_updated_at,
               scheduled.created_at,
               scheduled.updated_at
        FROM public.fitness_scheduled_workouts AS scheduled
        JOIN public.fitness_workout_templates AS template
          ON template.id = scheduled.workout_template_id
        LEFT JOIN public.fitness_recurring_schedule_series AS series
          ON series.id = scheduled.recurring_series_id
        LEFT JOIN public.fitness_training_plan_instances AS plan_instance
          ON plan_instance.id = scheduled.plan_instance_id
        LEFT JOIN public.fitness_training_plan_templates AS plan_template
          ON plan_template.id = plan_instance.plan_template_id
        LEFT JOIN public.fitness_running_workout_results AS result
          ON result.scheduled_workout_id = scheduled.id
    """


def _with_running_result(workout: dict) -> dict:
    result = None
    if any(
        workout.get(key) is not None
        for key in (
            "result_planned_distance_miles",
            "completed_distance_miles",
            "duration_seconds",
            "result_notes",
            "result_created_at",
            "result_updated_at",
        )
    ):
        result = {
            "planned_distance_miles": workout.get("result_planned_distance_miles"),
            "completed_distance_miles": workout.get("completed_distance_miles"),
            "duration_seconds": workout.get("duration_seconds"),
            "notes": workout.get("result_notes"),
            "created_at": workout.get("result_created_at"),
            "updated_at": workout.get("result_updated_at"),
        }
    cleaned = {
        key: value
        for key, value in workout.items()
        if key
        not in {
            "result_planned_distance_miles",
            "completed_distance_miles",
            "duration_seconds",
            "result_notes",
            "result_created_at",
            "result_updated_at",
        }
    }
    source_type = "INDIVIDUAL"
    source_label = "Individually scheduled"
    if cleaned.get("recurring_series_id"):
        source_type = "RECURRING_SERIES"
        source_label = f"{cleaned.get('workout_name')} series"
    elif cleaned.get("plan_instance_id"):
        source_type = "TRAINING_PLAN"
        source_label = cleaned.get("plan_template_name") or "Training plan"
    if cleaned.get("scheduled_date") != cleaned.get("original_scheduled_date"):
        source_type = "RESCHEDULE_REPLACEMENT"
        source_label = f"Rescheduled from {cleaned.get('original_scheduled_date')}"
    cleaned["source"] = {
        "type": source_type,
        "label": source_label,
        "recurring_series_id": cleaned.get("recurring_series_id"),
        "plan_instance_id": cleaned.get("plan_instance_id"),
        "plan_template_name": cleaned.get("plan_template_name"),
        "recurring_series_weekdays": cleaned.get("recurring_series_weekdays"),
    }
    cleaned["running_result"] = result
    return cleaned


def _scheduled_rows_to_dicts(cur, rows) -> list[dict]:
    return [_with_running_result(row) for row in _rows_to_dicts(cur, rows)]


def _get_workout_template(cur, *, user_id: str, template_id: str) -> dict:
    cur.execute(
        _workout_template_select()
        + """
        WHERE template.id = %s
          AND template.user_id = %s
        """,
        (template_id, user_id),
    )
    template = _row_to_dict(cur, cur.fetchone())
    if not template:
        raise FitnessNotFoundError(f"Workout template not found: {template_id}")
    return template


def _assert_active_workout_template(cur, *, user_id: str, template_id: str) -> dict:
    template = _get_workout_template(cur, user_id=user_id, template_id=template_id)
    if not template["active"]:
        raise FitnessValidationError("Workout template is archived")
    return template


def _validate_lifting_exercises(cur, *, user_id: str, exercises: list[dict]) -> None:
    exercise_ids = [str(item["exercise_id"]) for item in exercises]
    if len(set(exercise_ids)) != len(exercise_ids):
        raise FitnessValidationError("Lifting template exercises must be unique")
    if not exercise_ids:
        raise FitnessValidationError("Lifting templates require at least one exercise")
    cur.execute(
        """
        SELECT id
        FROM public.weightlifting_exercises
        WHERE user_id = %s
          AND id = ANY(%s::uuid[])
        """,
        (user_id, exercise_ids),
    )
    found = {str(row[0]) for row in cur.fetchall()}
    missing = sorted(set(exercise_ids) - found)
    if missing:
        raise FitnessValidationError("Lifting template exercises must belong to the user")


def _replace_lifting_exercises(
    cur,
    *,
    user_id: str,
    template_id: str,
    exercises: list[dict],
) -> None:
    template = _get_workout_template(cur, user_id=user_id, template_id=template_id)
    if template["type"] != "LIFTING":
        raise FitnessValidationError("Only LIFTING templates can have exercises")
    _validate_lifting_exercises(cur, user_id=user_id, exercises=exercises)
    cur.execute(
        "DELETE FROM public.fitness_lifting_template_exercises WHERE template_id = %s",
        (template_id,),
    )
    for item in exercises:
        cur.execute(
            """
            INSERT INTO public.fitness_lifting_template_exercises (
                template_id,
                exercise_id,
                display_order
            )
            VALUES (%s, %s, %s)
            """,
            (template_id, str(item["exercise_id"]), item["display_order"]),
        )


def _template_details(cur, template: dict) -> dict:
    if template["type"] != "LIFTING":
        return {**template, "exercises": []}
    cur.execute(
        """
        SELECT member.exercise_id,
               exercise.name,
               exercise.active,
               member.display_order
        FROM public.fitness_lifting_template_exercises AS member
        JOIN public.weightlifting_exercises AS exercise
          ON exercise.id = member.exercise_id
        WHERE member.template_id = %s
        ORDER BY member.display_order, exercise.name
        """,
        (template["id"],),
    )
    return {**template, "exercises": _rows_to_dicts(cur, cur.fetchall())}


def list_workout_templates(*, user_id: str, include_archived: bool = False) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            sql = _workout_template_select() + " WHERE template.user_id = %s"
            params = [user_id]
            if not include_archived:
                sql += " AND template.active = true"
            sql += " ORDER BY template.active DESC, template.name"
            cur.execute(sql, tuple(params))
            return _rows_to_dicts(cur, cur.fetchall())
    finally:
        put_db_conn(conn)


def create_workout_template(
    *,
    user_id: str,
    name: str,
    workout_type: str,
    notes: str | None = None,
    planned_distance_miles=None,
    exercises: list[dict] | None = None,
) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.fitness_workout_templates (
                    user_id,
                    name,
                    workout_type,
                    notes
                )
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (user_id, name, workout_type, notes),
            )
            template_id = str(cur.fetchone()[0])
            if workout_type == "RUNNING":
                cur.execute(
                    """
                    INSERT INTO public.fitness_running_workout_templates (
                        template_id,
                        planned_distance_miles
                    )
                    VALUES (%s, %s)
                    """,
                    (template_id, _decimal(planned_distance_miles)),
                )
            elif workout_type == "LIFTING":
                _replace_lifting_exercises(
                    cur,
                    user_id=user_id,
                    template_id=template_id,
                    exercises=exercises or [],
                )
            else:
                raise FitnessValidationError("Unsupported workout type")
            template = _get_workout_template(cur, user_id=user_id, template_id=template_id)
            detailed = _template_details(cur, template)
        conn.commit()
        return detailed
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def get_workout_template(*, user_id: str, template_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            return _template_details(
                cur,
                _get_workout_template(cur, user_id=user_id, template_id=template_id),
            )
    finally:
        put_db_conn(conn)


def update_workout_template(user_id: str, template_id: str, **fields) -> dict:
    allowed = {"name", "notes"}
    updates = []
    values = []
    for key, value in fields.items():
        if key in allowed:
            updates.append(f"{key} = %s")
            values.append(value)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            if updates:
                updates.append("updated_at = now()")
                values.extend([template_id, user_id])
                cur.execute(
                    f"""
                    UPDATE public.fitness_workout_templates
                    SET {", ".join(updates)}
                    WHERE id = %s
                      AND user_id = %s
                    """,
                    tuple(values),
                )
                if cur.rowcount != 1:
                    raise FitnessNotFoundError(f"Workout template not found: {template_id}")
            if "planned_distance_miles" in fields:
                template = _get_workout_template(
                    cur,
                    user_id=user_id,
                    template_id=template_id,
                )
                if template["type"] != "RUNNING":
                    raise FitnessValidationError("Only RUNNING templates have planned distance")
                cur.execute(
                    """
                    UPDATE public.fitness_running_workout_templates
                    SET planned_distance_miles = %s,
                        updated_at = now()
                    WHERE template_id = %s
                    """,
                    (_decimal(fields["planned_distance_miles"]), template_id),
                )
            template = _template_details(
                cur,
                _get_workout_template(cur, user_id=user_id, template_id=template_id),
            )
        conn.commit()
        return template
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def set_workout_template_active(*, user_id: str, template_id: str, active: bool) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.fitness_workout_templates
                SET active = %s,
                    updated_at = now()
                WHERE id = %s
                  AND user_id = %s
                """,
                (active, template_id, user_id),
            )
            if cur.rowcount != 1:
                raise FitnessNotFoundError(f"Workout template not found: {template_id}")
            template = _template_details(
                cur,
                _get_workout_template(cur, user_id=user_id, template_id=template_id),
            )
        conn.commit()
        return template
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def replace_lifting_template_exercises(
    *,
    user_id: str,
    template_id: str,
    exercises: list[dict],
) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _replace_lifting_exercises(
                cur,
                user_id=user_id,
                template_id=template_id,
                exercises=exercises,
            )
            template = _template_details(
                cur,
                _get_workout_template(cur, user_id=user_id, template_id=template_id),
            )
        conn.commit()
        return template
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def list_plan_templates(*, user_id: str, include_archived: bool = False) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT id, user_id, name, notes, active, created_at, updated_at
                FROM public.fitness_training_plan_templates
                WHERE user_id = %s
            """
            if not include_archived:
                sql += " AND active = true"
            sql += " ORDER BY active DESC, name"
            cur.execute(sql, (user_id,))
            return _rows_to_dicts(cur, cur.fetchall())
    finally:
        put_db_conn(conn)


def _get_plan_template(cur, *, user_id: str, plan_template_id: str) -> dict:
    cur.execute(
        """
        SELECT id, user_id, name, notes, active, created_at, updated_at
        FROM public.fitness_training_plan_templates
        WHERE id = %s
          AND user_id = %s
        """,
        (plan_template_id, user_id),
    )
    plan = _row_to_dict(cur, cur.fetchone())
    if not plan:
        raise FitnessNotFoundError(f"Training plan template not found: {plan_template_id}")
    cur.execute(
        """
        SELECT item.id,
               item.workout_template_id,
               item.day_offset,
               item.display_order,
               workout.name AS workout_name,
               workout.workout_type AS type
        FROM public.fitness_training_plan_template_items AS item
        JOIN public.fitness_workout_templates AS workout
          ON workout.id = item.workout_template_id
        WHERE item.plan_template_id = %s
        ORDER BY item.day_offset, item.display_order, item.id
        """,
        (plan_template_id,),
    )
    return {**plan, "items": _rows_to_dicts(cur, cur.fetchall())}


def _replace_plan_template_items(
    cur,
    *,
    user_id: str,
    plan_template_id: str,
    items: list[dict],
) -> None:
    _get_plan_template(cur, user_id=user_id, plan_template_id=plan_template_id)
    for item in items:
        _assert_active_workout_template(
            cur,
            user_id=user_id,
            template_id=str(item["workout_template_id"]),
        )
    cur.execute(
        "DELETE FROM public.fitness_training_plan_template_items WHERE plan_template_id = %s",
        (plan_template_id,),
    )
    for item in items:
        cur.execute(
            """
            INSERT INTO public.fitness_training_plan_template_items (
                plan_template_id,
                workout_template_id,
                day_offset,
                display_order
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                plan_template_id,
                str(item["workout_template_id"]),
                item["day_offset"],
                item["display_order"],
            ),
        )


def create_plan_template(
    *,
    user_id: str,
    name: str,
    notes: str | None = None,
    items: list[dict] | None = None,
) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.fitness_training_plan_templates (user_id, name, notes)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (user_id, name, notes),
            )
            plan_template_id = str(cur.fetchone()[0])
            _replace_plan_template_items(
                cur,
                user_id=user_id,
                plan_template_id=plan_template_id,
                items=items or [],
            )
            plan = _get_plan_template(
                cur,
                user_id=user_id,
                plan_template_id=plan_template_id,
            )
        conn.commit()
        return plan
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def get_plan_template(*, user_id: str, plan_template_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            return _get_plan_template(
                cur,
                user_id=user_id,
                plan_template_id=plan_template_id,
            )
    finally:
        put_db_conn(conn)


def update_plan_template(user_id: str, plan_template_id: str, **fields) -> dict:
    allowed = {"name", "notes"}
    updates = []
    values = []
    for key, value in fields.items():
        if key in allowed:
            updates.append(f"{key} = %s")
            values.append(value)
    if not updates:
        raise FitnessValidationError("No plan template fields supplied for update")
    updates.append("updated_at = now()")
    values.extend([plan_template_id, user_id])
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE public.fitness_training_plan_templates
                SET {", ".join(updates)}
                WHERE id = %s
                  AND user_id = %s
                """,
                tuple(values),
            )
            if cur.rowcount != 1:
                raise FitnessNotFoundError(f"Training plan template not found: {plan_template_id}")
            plan = _get_plan_template(
                cur,
                user_id=user_id,
                plan_template_id=plan_template_id,
            )
        conn.commit()
        return plan
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def set_plan_template_active(
    *,
    user_id: str,
    plan_template_id: str,
    active: bool,
) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.fitness_training_plan_templates
                SET active = %s,
                    updated_at = now()
                WHERE id = %s
                  AND user_id = %s
                """,
                (active, plan_template_id, user_id),
            )
            if cur.rowcount != 1:
                raise FitnessNotFoundError(f"Training plan template not found: {plan_template_id}")
            plan = _get_plan_template(
                cur,
                user_id=user_id,
                plan_template_id=plan_template_id,
            )
        conn.commit()
        return plan
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def replace_plan_template_items(
    *,
    user_id: str,
    plan_template_id: str,
    items: list[dict],
) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _replace_plan_template_items(
                cur,
                user_id=user_id,
                plan_template_id=plan_template_id,
                items=items,
            )
            plan = _get_plan_template(
                cur,
                user_id=user_id,
                plan_template_id=plan_template_id,
            )
        conn.commit()
        return plan
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _planned_distance_snapshot(template: dict):
    return _decimal(template["planned_distance_miles"]) if template["type"] == "RUNNING" else None


def _insert_scheduled_workout(
    cur,
    *,
    user_id: str,
    workout_template_id: str,
    scheduled_date: date,
    plan_instance_id: str | None = None,
    recurring_series_id: str | None = None,
    original_scheduled_date: date | None = None,
    planned_distance_miles=None,
) -> str:
    cur.execute(
        """
        INSERT INTO public.fitness_scheduled_workouts (
            user_id,
            workout_template_id,
            plan_instance_id,
            recurring_series_id,
            scheduled_date,
            original_scheduled_date,
            planned_distance_miles
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            user_id,
            workout_template_id,
            plan_instance_id,
            recurring_series_id,
            scheduled_date,
            original_scheduled_date or scheduled_date,
            planned_distance_miles,
        ),
    )
    return str(cur.fetchone()[0])


def create_scheduled_workout(
    *,
    user_id: str,
    workout_template_id: str,
    scheduled_date: date,
) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            template = _assert_active_workout_template(
                cur,
                user_id=user_id,
                template_id=workout_template_id,
            )
            scheduled_id = _insert_scheduled_workout(
                cur,
                user_id=user_id,
                workout_template_id=workout_template_id,
                scheduled_date=scheduled_date,
                planned_distance_miles=_planned_distance_snapshot(template),
            )
            workout = _get_scheduled_workout(
                cur,
                user_id=user_id,
                scheduled_workout_id=scheduled_id,
            )
        conn.commit()
        return workout
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _recurring_series_select() -> str:
    return """
        SELECT series.id,
               series.user_id,
               series.workout_template_id,
               template.name AS workout_name,
               template.workout_type AS type,
               series.start_date,
               series.end_date,
               series.duration_weeks,
               series.weekdays,
               series.status,
               series.idempotency_key,
               series.created_at,
               series.updated_at,
               series.stopped_at
        FROM public.fitness_recurring_schedule_series AS series
        JOIN public.fitness_workout_templates AS template
          ON template.id = series.workout_template_id
    """


def _get_recurring_series(cur, *, user_id: str, series_id: str) -> dict:
    cur.execute(
        _recurring_series_select()
        + """
        WHERE series.id = %s
          AND series.user_id = %s
        """,
        (series_id, user_id),
    )
    series = _row_to_dict(cur, cur.fetchone())
    if not series:
        raise FitnessNotFoundError(f"Recurring series not found: {series_id}")
    return series


def preview_recurring_series(
    *,
    user_id: str,
    workout_template_id: str,
    start_date: date,
    weekdays: list[int],
    duration_weeks: int | None = None,
    end_date: date | None = None,
) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            template = _assert_active_workout_template(
                cur,
                user_id=user_id,
                template_id=workout_template_id,
            )
            canonical_end, normalized_weekdays, normalized_duration, dates = _recurrence_dates(
                start_date=start_date,
                weekdays=weekdays,
                duration_weeks=duration_weeks,
                end_date=end_date,
            )
            return {
                "workout_template_id": workout_template_id,
                "workout_name": template["name"],
                "type": template["type"],
                "start_date": start_date.isoformat(),
                "end_date": canonical_end.isoformat(),
                "duration_weeks": normalized_duration,
                "weekdays": normalized_weekdays,
                "dates": [item.isoformat() for item in dates],
                "count": len(dates),
            }
    finally:
        put_db_conn(conn)


def create_recurring_series(
    *,
    user_id: str,
    workout_template_id: str,
    start_date: date,
    weekdays: list[int],
    duration_weeks: int | None = None,
    end_date: date | None = None,
    idempotency_key: str | None = None,
) -> dict:
    canonical_end, normalized_weekdays, normalized_duration, dates = _recurrence_dates(
        start_date=start_date,
        weekdays=weekdays,
        duration_weeks=duration_weeks,
        end_date=end_date,
    )
    fingerprint = _recurrence_fingerprint(
        workout_template_id=workout_template_id,
        start_date=start_date,
        end_date=canonical_end,
        duration_weeks=normalized_duration,
        weekdays=normalized_weekdays,
    )
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            template = _assert_active_workout_template(
                cur,
                user_id=user_id,
                template_id=workout_template_id,
            )
            inserted_series = True
            if idempotency_key:
                cur.execute(
                    """
                    INSERT INTO public.fitness_recurring_schedule_series (
                        user_id,
                        workout_template_id,
                        start_date,
                        end_date,
                        duration_weeks,
                        weekdays,
                        idempotency_key,
                        request_fingerprint
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET updated_at = public.fitness_recurring_schedule_series.updated_at
                    RETURNING id, request_fingerprint, (xmax = 0) AS inserted
                    """,
                    (
                        user_id,
                        workout_template_id,
                        start_date,
                        canonical_end,
                        normalized_duration,
                        normalized_weekdays,
                        idempotency_key,
                        fingerprint,
                    ),
                )
                series_row = cur.fetchone()
                series_id = str(series_row[0])
                inserted_series = bool(series_row[2])
                if series_row[1] != fingerprint:
                    raise FitnessConflictError(
                        "Idempotency key was already used for different recurrence inputs"
                    )
                if not inserted_series:
                    series = _get_recurring_series(
                        cur,
                        user_id=user_id,
                        series_id=series_id,
                    )
                    cur.execute(
                        _scheduled_select()
                        + """
                        WHERE scheduled.user_id = %s
                          AND scheduled.recurring_series_id = %s
                        ORDER BY scheduled.scheduled_date, scheduled.created_at, scheduled.id
                        """,
                        (user_id, series_id),
                    )
                    workouts = _scheduled_rows_to_dicts(cur, cur.fetchall())
                    conn.commit()
                    return {
                        **series,
                        "scheduled_workout_ids": [workout["id"] for workout in workouts],
                        "scheduled_workouts": workouts,
                        "dates": [workout["scheduled_date"] for workout in workouts],
                        "count": len(workouts),
                    }
            else:
                cur.execute(
                    """
                    INSERT INTO public.fitness_recurring_schedule_series (
                        user_id,
                        workout_template_id,
                        start_date,
                        end_date,
                        duration_weeks,
                        weekdays,
                        idempotency_key,
                        request_fingerprint
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        user_id,
                        workout_template_id,
                        start_date,
                        canonical_end,
                        normalized_duration,
                        normalized_weekdays,
                        idempotency_key,
                        fingerprint,
                    ),
                )
                series_id = str(cur.fetchone()[0])
            scheduled_ids = []
            if inserted_series:
                for scheduled_date in dates:
                    scheduled_ids.append(
                        _insert_scheduled_workout(
                            cur,
                            user_id=user_id,
                            workout_template_id=workout_template_id,
                            recurring_series_id=series_id,
                            scheduled_date=scheduled_date,
                            planned_distance_miles=_planned_distance_snapshot(template),
                        )
                    )
            series = _get_recurring_series(cur, user_id=user_id, series_id=series_id)
            cur.execute(
                _scheduled_select()
                + """
                WHERE scheduled.user_id = %s
                  AND scheduled.recurring_series_id = %s
                ORDER BY scheduled.scheduled_date, scheduled.created_at, scheduled.id
                """,
                (user_id, series_id),
            )
            workouts = _scheduled_rows_to_dicts(cur, cur.fetchall())
        conn.commit()
        return {
            **series,
            "scheduled_workout_ids": scheduled_ids,
            "scheduled_workouts": workouts,
            "dates": [item.isoformat() for item in dates],
            "count": len(dates),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def get_recurring_series(*, user_id: str, series_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            series = _get_recurring_series(cur, user_id=user_id, series_id=series_id)
            cur.execute(
                _scheduled_select()
                + """
                WHERE scheduled.user_id = %s
                  AND scheduled.recurring_series_id = %s
                ORDER BY scheduled.scheduled_date, scheduled.created_at, scheduled.id
                """,
                (user_id, series_id),
            )
            return {
                **series,
                "scheduled_workouts": _scheduled_rows_to_dicts(cur, cur.fetchall()),
            }
    finally:
        put_db_conn(conn)


def instantiate_plan_template(
    *,
    user_id: str,
    plan_template_id: str,
    start_date: date,
) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            plan = _get_plan_template(
                cur,
                user_id=user_id,
                plan_template_id=plan_template_id,
            )
            if not plan["active"]:
                raise FitnessValidationError("Training plan template is archived")
            cur.execute(
                """
                INSERT INTO public.fitness_training_plan_instances (
                    user_id,
                    plan_template_id,
                    start_date
                )
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (user_id, plan_template_id, start_date),
            )
            instance_id = str(cur.fetchone()[0])
            scheduled_ids = []
            for item in plan["items"]:
                template = _assert_active_workout_template(
                    cur,
                    user_id=user_id,
                    template_id=item["workout_template_id"],
                )
                scheduled_ids.append(
                    _insert_scheduled_workout(
                        cur,
                        user_id=user_id,
                        workout_template_id=item["workout_template_id"],
                        plan_instance_id=instance_id,
                        scheduled_date=start_date + timedelta(days=item["day_offset"]),
                        planned_distance_miles=_planned_distance_snapshot(template),
                    )
                )
            instance = _get_plan_instance(cur, user_id=user_id, instance_id=instance_id)
            instance["scheduled_workout_ids"] = scheduled_ids
        conn.commit()
        return instance
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _get_plan_instance(cur, *, user_id: str, instance_id: str, include_workouts: bool = False) -> dict:
    cur.execute(
        """
        SELECT instance.id,
               instance.user_id,
               instance.plan_template_id,
               template.name AS plan_template_name,
               instance.start_date,
               instance.status,
               instance.stopped_at,
               instance.created_at,
               instance.updated_at
        FROM public.fitness_training_plan_instances AS instance
        JOIN public.fitness_training_plan_templates AS template
          ON template.id = instance.plan_template_id
        WHERE instance.id = %s
          AND instance.user_id = %s
        """,
        (instance_id, user_id),
    )
    instance = _row_to_dict(cur, cur.fetchone())
    if not instance:
        raise FitnessNotFoundError(f"Training plan instance not found: {instance_id}")
    instance["planning_status"] = "STOPPED" if instance.get("stopped_at") else "ACTIVE"
    if include_workouts:
        cur.execute(
            _scheduled_select()
            + """
            WHERE scheduled.user_id = %s
              AND scheduled.plan_instance_id = %s
            ORDER BY scheduled.scheduled_date, scheduled.created_at, scheduled.id
            """,
            (user_id, instance_id),
        )
        instance["scheduled_workouts"] = _scheduled_rows_to_dicts(cur, cur.fetchall())
    return instance


def get_plan_instance(*, user_id: str, instance_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            return _get_plan_instance(
                cur,
                user_id=user_id,
                instance_id=instance_id,
                include_workouts=True,
            )
    finally:
        put_db_conn(conn)


def complete_plan_instance(*, user_id: str, instance_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _get_plan_instance(cur, user_id=user_id, instance_id=instance_id)
            cur.execute(
                """
                UPDATE public.fitness_training_plan_instances
                SET status = 'COMPLETED',
                    updated_at = now()
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'ACTIVE'
                """,
                (instance_id, user_id),
            )
            if cur.rowcount != 1:
                raise FitnessConflictError("Only active plan instances can be completed")
            instance = _get_plan_instance(
                cur,
                user_id=user_id,
                instance_id=instance_id,
                include_workouts=True,
            )
        conn.commit()
        return instance
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def list_plan_instances(*, user_id: str) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT instance.id,
                       instance.user_id,
                       instance.plan_template_id,
                       template.name AS plan_template_name,
                       instance.start_date,
                       instance.status,
                       instance.stopped_at,
                       instance.created_at,
                       instance.updated_at
                FROM public.fitness_training_plan_instances AS instance
                JOIN public.fitness_training_plan_templates AS template
                  ON template.id = instance.plan_template_id
                WHERE instance.user_id = %s
                ORDER BY instance.start_date DESC, instance.created_at DESC
                """,
                (user_id,),
            )
            instances = _rows_to_dicts(cur, cur.fetchall())
            for instance in instances:
                instance["planning_status"] = "STOPPED" if instance.get("stopped_at") else "ACTIVE"
            return instances
    finally:
        put_db_conn(conn)


def get_current_plan_instance(*, user_id: str) -> dict | None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT instance.id,
                       instance.user_id,
                       instance.plan_template_id,
                       template.name AS plan_template_name,
                       instance.start_date,
                       instance.status,
                       instance.stopped_at,
                       instance.created_at,
                       instance.updated_at
                FROM public.fitness_training_plan_instances AS instance
                JOIN public.fitness_training_plan_templates AS template
                  ON template.id = instance.plan_template_id
                WHERE instance.user_id = %s
                  AND instance.status = 'ACTIVE'
                  AND instance.stopped_at IS NULL
                ORDER BY instance.start_date DESC, instance.created_at DESC
                LIMIT 1
                """,
                (user_id,),
            )
            instance = _row_to_dict(cur, cur.fetchone())
            if instance:
                instance["planning_status"] = "ACTIVE"
            return instance
    finally:
        put_db_conn(conn)


def _get_scheduled_workout(
    cur,
    *,
    user_id: str,
    scheduled_workout_id: str,
    lock: bool = False,
) -> dict:
    lock_clause = " FOR UPDATE OF scheduled" if lock else ""
    cur.execute(
        _scheduled_select()
        + """
        WHERE scheduled.id = %s
          AND scheduled.user_id = %s
        """
        + lock_clause,
        (scheduled_workout_id, user_id),
    )
    workout = _row_to_dict(cur, cur.fetchone())
    if not workout:
        raise FitnessNotFoundError(f"Scheduled workout not found: {scheduled_workout_id}")
    return _with_running_result(workout)


def get_scheduled_workout(*, user_id: str, scheduled_workout_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            return _get_scheduled_workout(
                cur,
                user_id=user_id,
                scheduled_workout_id=scheduled_workout_id,
            )
    finally:
        put_db_conn(conn)


def list_scheduled_workouts(
    *,
    user_id: str,
    start_date: date,
    end_date: date,
) -> list[dict]:
    if end_date < start_date:
        raise FitnessValidationError("end_date must be on or after start_date")
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                _scheduled_select()
                + """
                WHERE scheduled.user_id = %s
                  AND scheduled.scheduled_date BETWEEN %s AND %s
                ORDER BY scheduled.scheduled_date, scheduled.created_at, scheduled.id
                """,
                (user_id, start_date, end_date),
            )
            return _scheduled_rows_to_dicts(cur, cur.fetchall())
    finally:
        put_db_conn(conn)


def list_workout_history(
    *,
    user_id: str,
    start_date: date,
    end_date: date,
) -> list[dict]:
    if end_date < start_date:
        raise FitnessValidationError("end_date must be on or after start_date")
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                _scheduled_select()
                + """
                WHERE scheduled.user_id = %s
                  AND scheduled.scheduled_date BETWEEN %s AND %s
                  AND scheduled.status <> 'PLANNED'
                ORDER BY scheduled.scheduled_date DESC, scheduled.updated_at DESC, scheduled.id
                """,
                (user_id, start_date, end_date),
            )
            return _scheduled_rows_to_dicts(cur, cur.fetchall())
    finally:
        put_db_conn(conn)


def today_workouts(*, user_id: str, target_date: date) -> list[dict]:
    return list_scheduled_workouts(
        user_id=user_id,
        start_date=target_date,
        end_date=target_date,
    )


def _assert_no_weightlifting_entries(cur, *, user_id: str, scheduled_workout_id: str) -> None:
    cur.execute(
        """
        SELECT 1
        FROM public.weightlifting_entries
        WHERE user_id = %s
          AND fitness_scheduled_workout_id = %s
        LIMIT 1
        """,
        (user_id, scheduled_workout_id),
    )
    if cur.fetchone():
        raise FitnessConflictError("Workout has linked Weightlifting entries")


def _assert_not_reschedule_replacement(cur, *, user_id: str, scheduled_workout_id: str) -> None:
    cur.execute(
        """
        SELECT 1
        FROM public.fitness_scheduled_workouts
        WHERE user_id = %s
          AND status = 'RESCHEDULED'
          AND replacement_scheduled_workout_id = %s
        LIMIT 1
        """,
        (user_id, scheduled_workout_id),
    )
    if cur.fetchone():
        raise FitnessConflictError("Undo the reschedule before removing its replacement")


def _assert_safe_planned_removal(cur, *, user_id: str, workout: dict) -> None:
    if workout["status"] != "PLANNED":
        raise FitnessConflictError("Only planned workouts can be removed")
    if workout.get("running_result"):
        raise FitnessConflictError("Workout has Running result data")
    _assert_no_weightlifting_entries(
        cur,
        user_id=user_id,
        scheduled_workout_id=workout["id"],
    )
    _assert_not_reschedule_replacement(
        cur,
        user_id=user_id,
        scheduled_workout_id=workout["id"],
    )


def remove_scheduled_workout(*, user_id: str, scheduled_workout_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            workout = _get_scheduled_workout(
                cur,
                user_id=user_id,
                scheduled_workout_id=scheduled_workout_id,
                lock=True,
            )
            _assert_safe_planned_removal(cur, user_id=user_id, workout=workout)
            cur.execute(
                """
                DELETE FROM public.fitness_scheduled_workouts
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'PLANNED'
                """,
                (scheduled_workout_id, user_id),
            )
            if cur.rowcount != 1:
                raise FitnessConflictError("Workout could not be removed safely")
        conn.commit()
        return {"removed_scheduled_workout_id": scheduled_workout_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def replace_scheduled_workout_template(
    *,
    user_id: str,
    scheduled_workout_id: str,
    workout_template_id: str,
) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            workout = _get_scheduled_workout(
                cur,
                user_id=user_id,
                scheduled_workout_id=scheduled_workout_id,
                lock=True,
            )
            if workout["status"] != "PLANNED":
                raise FitnessConflictError("Only planned workouts can change templates")
            if workout.get("running_result"):
                raise FitnessConflictError("Workout has Running result data")
            _assert_no_weightlifting_entries(
                cur,
                user_id=user_id,
                scheduled_workout_id=workout["id"],
            )
            template = _assert_active_workout_template(
                cur,
                user_id=user_id,
                template_id=workout_template_id,
            )
            if template["type"] != workout["type"]:
                raise FitnessValidationError("Replacement template must match workout type")
            cur.execute(
                """
                UPDATE public.fitness_scheduled_workouts
                SET workout_template_id = %s,
                    planned_distance_miles = %s,
                    updated_at = now()
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'PLANNED'
                """,
                (
                    workout_template_id,
                    _planned_distance_snapshot(template),
                    scheduled_workout_id,
                    user_id,
                ),
            )
            if cur.rowcount != 1:
                raise FitnessConflictError("Only planned workouts can change templates")
            result = _get_scheduled_workout(
                cur,
                user_id=user_id,
                scheduled_workout_id=scheduled_workout_id,
            )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def undo_reschedule(*, user_id: str, scheduled_workout_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            original = _get_scheduled_workout(
                cur,
                user_id=user_id,
                scheduled_workout_id=scheduled_workout_id,
                lock=True,
            )
            replacement_id = original.get("replacement_scheduled_workout_id")
            if original["status"] != "RESCHEDULED" or not replacement_id:
                raise FitnessConflictError("Workout is not an undoable reschedule source")
            replacement = _get_scheduled_workout(
                cur,
                user_id=user_id,
                scheduled_workout_id=replacement_id,
                lock=True,
            )
            if replacement["status"] != "PLANNED":
                raise FitnessConflictError("Only planned replacement workouts can be removed")
            if replacement.get("running_result"):
                raise FitnessConflictError("Replacement has Running result data")
            _assert_no_weightlifting_entries(
                cur,
                user_id=user_id,
                scheduled_workout_id=replacement_id,
            )
            cur.execute(
                """
                UPDATE public.fitness_scheduled_workouts
                SET status = 'PLANNED',
                    replacement_scheduled_workout_id = NULL,
                    updated_at = now()
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'RESCHEDULED'
                  AND replacement_scheduled_workout_id = %s
                """,
                (scheduled_workout_id, user_id, replacement_id),
            )
            if cur.rowcount != 1:
                raise FitnessConflictError("Reschedule could not be restored safely")
            cur.execute(
                """
                DELETE FROM public.fitness_scheduled_workouts
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'PLANNED'
                """,
                (replacement_id, user_id),
            )
            if cur.rowcount != 1:
                raise FitnessConflictError("Replacement could not be removed safely")
            restored = _get_scheduled_workout(
                cur,
                user_id=user_id,
                scheduled_workout_id=scheduled_workout_id,
            )
        conn.commit()
        return {
            "original": restored,
            "removed_replacement_scheduled_workout_id": replacement_id,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def remove_remaining_recurring_workouts(
    *,
    user_id: str,
    series_id: str,
    from_date: date | None = None,
) -> dict:
    cutoff = from_date or current_fitness_date()
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _get_recurring_series(cur, user_id=user_id, series_id=series_id)
            cur.execute(
                """
                SELECT scheduled.id
                FROM public.fitness_scheduled_workouts AS scheduled
                WHERE scheduled.user_id = %s
                  AND scheduled.recurring_series_id = %s
                  AND scheduled.scheduled_date >= %s
                  AND scheduled.status = 'PLANNED'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.fitness_running_workout_results AS result
                      WHERE result.scheduled_workout_id = scheduled.id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.weightlifting_entries AS lifting
                      WHERE lifting.user_id = scheduled.user_id
                        AND lifting.fitness_scheduled_workout_id = scheduled.id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.fitness_scheduled_workouts AS source
                      WHERE source.user_id = scheduled.user_id
                        AND source.status = 'RESCHEDULED'
                        AND source.replacement_scheduled_workout_id = scheduled.id
                  )
                ORDER BY scheduled.scheduled_date, scheduled.id
                FOR UPDATE OF scheduled
                """,
                (user_id, series_id, cutoff),
            )
            removable_ids = [str(row[0]) for row in cur.fetchall()]
            if removable_ids:
                cur.execute(
                    """
                    DELETE FROM public.fitness_scheduled_workouts
                    WHERE user_id = %s
                      AND id = ANY(%s::uuid[])
                    """,
                    (user_id, removable_ids),
                )
            cur.execute(
                """
                UPDATE public.fitness_recurring_schedule_series
                SET status = 'STOPPED',
                    stopped_at = now(),
                    updated_at = now()
                WHERE id = %s
                  AND user_id = %s
                """,
                (series_id, user_id),
            )
            series = _get_recurring_series(cur, user_id=user_id, series_id=series_id)
        conn.commit()
        return {
            **series,
            "removed_scheduled_workout_ids": removable_ids,
            "removed_count": len(removable_ids),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _plan_instance_cleanup(
    *,
    user_id: str,
    instance_id: str,
    unstarted_only: bool,
    from_date: date | None = None,
) -> dict:
    cutoff = from_date or current_fitness_date()
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, stopped_at
                FROM public.fitness_training_plan_instances
                WHERE id = %s
                  AND user_id = %s
                FOR UPDATE
                """,
                (instance_id, user_id),
            )
            instance_row = cur.fetchone()
            if not instance_row:
                raise FitnessNotFoundError(f"Training plan instance not found: {instance_id}")
            instance_status = instance_row[1]
            cur.execute(
                """
                SELECT scheduled.id,
                       scheduled.status,
                       result.scheduled_workout_id IS NOT NULL AS has_running_result,
                       EXISTS (
                           SELECT 1
                           FROM public.weightlifting_entries AS lifting
                           WHERE lifting.user_id = scheduled.user_id
                             AND lifting.fitness_scheduled_workout_id = scheduled.id
                       ) AS has_weightlifting_entries,
                       EXISTS (
                           SELECT 1
                           FROM public.fitness_scheduled_workouts AS source
                           WHERE source.user_id = scheduled.user_id
                             AND source.status = 'RESCHEDULED'
                             AND source.replacement_scheduled_workout_id = scheduled.id
                       ) AS is_reschedule_replacement
                FROM public.fitness_scheduled_workouts AS scheduled
                LEFT JOIN public.fitness_running_workout_results AS result
                  ON result.scheduled_workout_id = scheduled.id
                WHERE scheduled.user_id = %s
                  AND scheduled.plan_instance_id = %s
                  AND (%s OR scheduled.scheduled_date >= %s)
                ORDER BY scheduled.scheduled_date, scheduled.id
                FOR UPDATE OF scheduled
                """,
                (user_id, instance_id, unstarted_only, cutoff),
            )
            locked_rows = cur.fetchall()
            if unstarted_only and instance_status == "COMPLETED":
                raise FitnessConflictError("Completed plan instances cannot be removed as unstarted")
            removable_ids = [
                str(row[0])
                for row in locked_rows
                if row[1] == "PLANNED" and not row[2] and not row[3] and not row[4]
            ]
            if unstarted_only:
                if len(removable_ids) != len(locked_rows):
                    raise FitnessConflictError("Plan instance has workout history")
            if removable_ids:
                cur.execute(
                    """
                    DELETE FROM public.fitness_scheduled_workouts
                    WHERE user_id = %s
                      AND id = ANY(%s::uuid[])
                    """,
                    (user_id, removable_ids),
                )
            if unstarted_only:
                cur.execute(
                    """
                    DELETE FROM public.fitness_training_plan_instances
                    WHERE id = %s
                      AND user_id = %s
                    """,
                    (instance_id, user_id),
                )
                result = {"removed_plan_instance_id": instance_id}
            else:
                if instance_status == "ACTIVE":
                    cur.execute(
                        """
                        UPDATE public.fitness_training_plan_instances
                        SET stopped_at = COALESCE(stopped_at, now()),
                            updated_at = now()
                        WHERE id = %s
                          AND user_id = %s
                          AND status = 'ACTIVE'
                        """,
                        (instance_id, user_id),
                    )
                result = _get_plan_instance(
                    cur,
                    user_id=user_id,
                    instance_id=instance_id,
                    include_workouts=True,
                )
            result["removed_scheduled_workout_ids"] = removable_ids
            result["removed_count"] = len(removable_ids)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def remove_unstarted_plan_instance(*, user_id: str, instance_id: str) -> dict:
    return _plan_instance_cleanup(
        user_id=user_id,
        instance_id=instance_id,
        unstarted_only=True,
    )


def remove_remaining_plan_workouts(
    *,
    user_id: str,
    instance_id: str,
    from_date: date | None = None,
) -> dict:
    return _plan_instance_cleanup(
        user_id=user_id,
        instance_id=instance_id,
        unstarted_only=False,
        from_date=from_date,
    )


def _week_start(value: date) -> date:
    return value - timedelta(days=value.isoweekday() - 1)


def training_calendar(
    *,
    user_id: str,
    start_date: date,
    end_date: date,
) -> dict:
    if end_date < start_date:
        raise FitnessValidationError("end_date must be on or after start_date")
    if (end_date - start_date).days > 120:
        raise FitnessValidationError("Calendar range cannot exceed 121 days")
    calendar_start = _week_start(start_date)
    calendar_end = _week_start(end_date) + timedelta(days=6)
    workouts = list_scheduled_workouts(
        user_id=user_id,
        start_date=calendar_start,
        end_date=calendar_end,
    )
    by_date: dict[str, list[dict]] = {}
    for workout in workouts:
        by_date.setdefault(workout["scheduled_date"], []).append(workout)

    weeks = []
    previous_summary = None
    today = current_fitness_date().isoformat()
    current_week = calendar_start
    while current_week <= calendar_end:
        days = []
        week_workouts = []
        for offset in range(7):
            day = current_week + timedelta(days=offset)
            day_key = day.isoformat()
            day_workouts = by_date.get(day_key, [])
            week_workouts.extend(day_workouts)
            days.append(
                {
                    "date": day_key,
                    "is_today": day_key == today,
                    "workouts": day_workouts,
                }
            )
        planned_runs = [
            workout
            for workout in week_workouts
            if workout["type"] == "RUNNING"
            and workout["status"] != "RESCHEDULED"
            and workout.get("planned_distance_miles") is not None
        ]
        completed_runs = [
            workout
            for workout in week_workouts
            if workout["type"] == "RUNNING"
            and workout["status"] == "COMPLETED"
            and workout.get("running_result")
            and workout["running_result"].get("completed_distance_miles") is not None
        ]
        planned_mileage = sum(Decimal(str(workout["planned_distance_miles"])) for workout in planned_runs)
        actual_mileage = sum(
            Decimal(str(workout["running_result"]["completed_distance_miles"]))
            for workout in completed_runs
        )
        longest_planned = max(
            (Decimal(str(workout["planned_distance_miles"])) for workout in planned_runs),
            default=None,
        )
        longest_completed = max(
            (
                Decimal(str(workout["running_result"]["completed_distance_miles"]))
                for workout in completed_runs
            ),
            default=None,
        )
        summary = {
            "planned_running_miles": _serialize_value(planned_mileage),
            "actual_running_miles": _serialize_value(actual_mileage),
            "longest_planned_run_miles": _serialize_value(longest_planned),
            "longest_completed_run_miles": _serialize_value(longest_completed),
            "planned_mileage_change": (
                None
                if previous_summary is None
                else _serialize_value(
                    planned_mileage
                    - Decimal(str(previous_summary["planned_running_miles"]))
                )
            ),
            "actual_mileage_change": (
                None
                if previous_summary is None
                else _serialize_value(
                    actual_mileage
                    - Decimal(str(previous_summary["actual_running_miles"]))
                )
            ),
            "planned_long_run_percentage": (
                None
                if not planned_mileage or longest_planned is None
                else _serialize_value((longest_planned / planned_mileage) * Decimal("100"))
            ),
            "completed_lifting_sessions": len(
                [
                    workout
                    for workout in week_workouts
                    if workout["type"] == "LIFTING" and workout["status"] == "COMPLETED"
                ]
            ),
        }
        weeks.append(
            {
                "week_start": current_week.isoformat(),
                "days": days,
                "summary": summary,
            }
        )
        previous_summary = summary
        current_week += timedelta(days=7)
    return {
        "start_date": calendar_start.isoformat(),
        "end_date": calendar_end.isoformat(),
        "weeks": weeks,
    }


def complete_scheduled_workout(
    *,
    user_id: str,
    scheduled_workout_id: str,
    running: dict | None = None,
) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            workout = _get_scheduled_workout(
                cur,
                user_id=user_id,
                scheduled_workout_id=scheduled_workout_id,
                lock=True,
            )
            if workout["status"] != "PLANNED":
                raise FitnessConflictError("Only planned workouts can be completed")
            if workout["type"] == "RUNNING":
                if not running:
                    raise FitnessValidationError("Running completion details are required")
                cur.execute(
                    """
                    INSERT INTO public.fitness_running_workout_results (
                        scheduled_workout_id,
                        planned_distance_miles,
                        completed_distance_miles,
                        duration_seconds,
                        notes
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (scheduled_workout_id)
                    DO UPDATE SET
                        planned_distance_miles = EXCLUDED.planned_distance_miles,
                        completed_distance_miles = EXCLUDED.completed_distance_miles,
                        duration_seconds = EXCLUDED.duration_seconds,
                        notes = EXCLUDED.notes,
                        updated_at = now()
                    """,
                    (
                        scheduled_workout_id,
                        workout["planned_distance_miles"],
                        _decimal(running["completed_distance_miles"]),
                        running["duration_seconds"],
                        running.get("notes"),
                    ),
                )
            cur.execute(
                """
                UPDATE public.fitness_scheduled_workouts
                SET status = 'COMPLETED',
                    updated_at = now()
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'PLANNED'
                """,
                (scheduled_workout_id, user_id),
            )
            if cur.rowcount != 1:
                raise FitnessConflictError("Only planned workouts can be completed")
            result = _get_scheduled_workout(
                cur,
                user_id=user_id,
                scheduled_workout_id=scheduled_workout_id,
            )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def skip_scheduled_workout(*, user_id: str, scheduled_workout_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            workout = _get_scheduled_workout(
                cur,
                user_id=user_id,
                scheduled_workout_id=scheduled_workout_id,
                lock=True,
            )
            if workout["status"] != "PLANNED":
                raise FitnessConflictError("Only planned workouts can be skipped")
            cur.execute(
                """
                UPDATE public.fitness_scheduled_workouts
                SET status = 'SKIPPED',
                    updated_at = now()
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'PLANNED'
                """,
                (scheduled_workout_id, user_id),
            )
            if cur.rowcount != 1:
                raise FitnessConflictError("Only planned workouts can be skipped")
            result = _get_scheduled_workout(
                cur,
                user_id=user_id,
                scheduled_workout_id=scheduled_workout_id,
            )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def reschedule_scheduled_workout(
    *,
    user_id: str,
    scheduled_workout_id: str,
    scheduled_date: date,
) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            workout = _get_scheduled_workout(
                cur,
                user_id=user_id,
                scheduled_workout_id=scheduled_workout_id,
                lock=True,
            )
            if workout["status"] != "PLANNED":
                raise FitnessConflictError("Only planned workouts can be rescheduled")
            replacement_id = _insert_scheduled_workout(
                cur,
                user_id=user_id,
                workout_template_id=workout["workout_template_id"],
                plan_instance_id=workout["plan_instance_id"],
                recurring_series_id=workout["recurring_series_id"],
                scheduled_date=scheduled_date,
                original_scheduled_date=workout["original_scheduled_date"],
                planned_distance_miles=workout["planned_distance_miles"],
            )
            cur.execute(
                """
                UPDATE public.fitness_scheduled_workouts
                SET status = 'RESCHEDULED',
                    replacement_scheduled_workout_id = %s,
                    updated_at = now()
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'PLANNED'
                """,
                (replacement_id, scheduled_workout_id, user_id),
            )
            if cur.rowcount != 1:
                raise FitnessConflictError("Only planned workouts can be rescheduled")
            result = {
                "original": _get_scheduled_workout(
                    cur,
                    user_id=user_id,
                    scheduled_workout_id=scheduled_workout_id,
                ),
                "replacement": _get_scheduled_workout(
                    cur,
                    user_id=user_id,
                    scheduled_workout_id=replacement_id,
                ),
            }
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)

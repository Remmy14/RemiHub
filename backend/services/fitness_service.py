from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.database.database import get_db_conn, put_db_conn

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
               scheduled.planned_distance_miles,
               template.name AS workout_name,
               template.workout_type AS type,
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
    original_scheduled_date: date | None = None,
    planned_distance_miles=None,
) -> str:
    cur.execute(
        """
        INSERT INTO public.fitness_scheduled_workouts (
            user_id,
            workout_template_id,
            plan_instance_id,
            scheduled_date,
            original_scheduled_date,
            planned_distance_miles
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            user_id,
            workout_template_id,
            plan_instance_id,
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
            return _rows_to_dicts(cur, cur.fetchall())
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
                       instance.created_at,
                       instance.updated_at
                FROM public.fitness_training_plan_instances AS instance
                JOIN public.fitness_training_plan_templates AS template
                  ON template.id = instance.plan_template_id
                WHERE instance.user_id = %s
                  AND instance.status = 'ACTIVE'
                ORDER BY instance.start_date DESC, instance.created_at DESC
                LIMIT 1
                """,
                (user_id,),
            )
            return _row_to_dict(cur, cur.fetchone())
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

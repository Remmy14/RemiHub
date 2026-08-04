from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from backend.database.database import get_db_conn, put_db_conn


DEFAULT_WEIGHT_UNIT = "lb"
DEFAULT_WEIGHT_INCREMENT = Decimal("5")
DEFAULT_TARGET_REPS = 12
DEFAULT_SETS = 3
MAX_HISTORY_LIMIT = 500
WEEKDAY_OFFSETS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
DEFAULT_DAY_SLOTS = (
    {"slot": 1, "label": "Day 1", "weekday": "monday"},
    {"slot": 2, "label": "Day 2", "weekday": "wednesday"},
    {"slot": 3, "label": "Day 3", "weekday": "friday"},
)


class WeightliftingNotFoundError(ValueError):
    pass


class WeightliftingValidationError(ValueError):
    pass


class WeightliftingConflictError(ValueError):
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
        {
            column: _serialize_value(value)
            for column, value in zip(columns, row)
        }
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
        raise WeightliftingValidationError("Invalid decimal value") from exc


def _normalize_week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _validate_slot(slot: int) -> None:
    if slot not in (1, 2, 3):
        raise WeightliftingValidationError("workout_day_slot must be 1, 2, or 3")


def _ensure_settings(cur, user_id: str) -> None:
    cur.execute(
        """
        INSERT INTO public.weightlifting_settings (
            user_id,
            weight_unit,
            default_weight_increment,
            default_target_reps,
            default_sets
        )
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (
            user_id,
            DEFAULT_WEIGHT_UNIT,
            DEFAULT_WEIGHT_INCREMENT,
            DEFAULT_TARGET_REPS,
            DEFAULT_SETS,
        ),
    )
    for day in DEFAULT_DAY_SLOTS:
        cur.execute(
            """
            INSERT INTO public.weightlifting_day_slots (
                user_id,
                slot,
                label,
                weekday
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, slot) DO NOTHING
            """,
            (user_id, day["slot"], day["label"], day["weekday"]),
        )


def _settings_from_rows(settings: dict, days: list[dict]) -> dict:
    return {
        "weight_unit": settings["weight_unit"],
        "default_weight_increment": settings["default_weight_increment"],
        "default_target_reps": settings["default_target_reps"],
        "default_sets": settings["default_sets"],
        "days": days,
        "created_at": settings["created_at"],
        "updated_at": settings["updated_at"],
    }


def get_settings(user_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _ensure_settings(cur, user_id)
            cur.execute(
                """
                SELECT user_id,
                       weight_unit,
                       default_weight_increment,
                       default_target_reps,
                       default_sets,
                       created_at,
                       updated_at
                FROM public.weightlifting_settings
                WHERE user_id = %s
                """,
                (user_id,),
            )
            settings = _row_to_dict(cur, cur.fetchone())
            cur.execute(
                """
                SELECT slot,
                       label,
                       weekday
                FROM public.weightlifting_day_slots
                WHERE user_id = %s
                ORDER BY slot
                """,
                (user_id,),
            )
            days = _rows_to_dicts(cur, cur.fetchall())
        conn.commit()
        return _settings_from_rows(settings, days)
    finally:
        put_db_conn(conn)


def update_settings(
    *,
    user_id: str,
    weight_unit: str,
    default_weight_increment,
    default_target_reps: int,
    default_sets: int | None,
    days: list[dict],
) -> dict:
    slots = sorted(day["slot"] for day in days)
    if slots != [1, 2, 3]:
        raise WeightliftingValidationError("days must contain slots 1, 2, and 3")

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _ensure_settings(cur, user_id)
            cur.execute(
                """
                UPDATE public.weightlifting_settings
                SET weight_unit = %s,
                    default_weight_increment = %s,
                    default_target_reps = %s,
                    default_sets = %s,
                    updated_at = now()
                WHERE user_id = %s
                """,
                (
                    weight_unit,
                    _decimal(default_weight_increment),
                    default_target_reps,
                    default_sets,
                    user_id,
                ),
            )
            for day in days:
                cur.execute(
                    """
                    UPDATE public.weightlifting_day_slots
                    SET label = %s,
                        weekday = %s,
                        updated_at = now()
                    WHERE user_id = %s
                      AND slot = %s
                    """,
                    (day["label"], day.get("weekday"), user_id, day["slot"]),
                )
            cur.execute(
                """
                SELECT user_id,
                       weight_unit,
                       default_weight_increment,
                       default_target_reps,
                       default_sets,
                       created_at,
                       updated_at
                FROM public.weightlifting_settings
                WHERE user_id = %s
                """,
                (user_id,),
            )
            settings = _row_to_dict(cur, cur.fetchone())
            cur.execute(
                """
                SELECT slot,
                       label,
                       weekday
                FROM public.weightlifting_day_slots
                WHERE user_id = %s
                ORDER BY slot
                """,
                (user_id,),
            )
            updated_days = _rows_to_dicts(cur, cur.fetchall())
        conn.commit()
        return _settings_from_rows(settings, updated_days)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _exercise_select() -> str:
    return """
        SELECT id,
               name,
               display_order,
               active,
               notes,
               target_reps,
               target_sets,
               weight_increment,
               weight_unit,
               created_at,
               updated_at
        FROM public.weightlifting_exercises
    """


def _get_defaults(cur, user_id: str) -> dict:
    _ensure_settings(cur, user_id)
    cur.execute(
        """
        SELECT weight_unit,
               default_weight_increment,
               default_target_reps,
               default_sets
        FROM public.weightlifting_settings
        WHERE user_id = %s
        """,
        (user_id,),
    )
    return _row_to_dict(cur, cur.fetchone())


def list_exercises(*, user_id: str, include_archived: bool = False) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            if include_archived:
                cur.execute(
                    _exercise_select()
                    + """
                    WHERE user_id = %s
                    ORDER BY active DESC, display_order, name
                    """,
                    (user_id,),
                )
            else:
                cur.execute(
                    _exercise_select()
                    + """
                    WHERE user_id = %s
                      AND active = true
                    ORDER BY display_order, name
                    """,
                    (user_id,),
                )
            return _rows_to_dicts(cur, cur.fetchall())
    finally:
        put_db_conn(conn)


def create_exercise(
    *,
    user_id: str,
    name: str,
    display_order: int | None = None,
    notes: str | None = None,
    target_reps: int | None = None,
    target_sets: int | None = None,
    weight_increment=None,
    weight_unit: str | None = None,
) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            defaults = _get_defaults(cur, user_id)
            if display_order is None:
                cur.execute(
                    """
                    SELECT COALESCE(MAX(display_order), 0) + 1
                    FROM public.weightlifting_exercises
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                display_order = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO public.weightlifting_exercises (
                    user_id,
                    name,
                    display_order,
                    notes,
                    target_reps,
                    target_sets,
                    weight_increment,
                    weight_unit
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id,
                          name,
                          display_order,
                          active,
                          notes,
                          target_reps,
                          target_sets,
                          weight_increment,
                          weight_unit,
                          created_at,
                          updated_at
                """,
                (
                    user_id,
                    name,
                    display_order,
                    notes,
                    target_reps or defaults["default_target_reps"],
                    target_sets if target_sets is not None else defaults["default_sets"],
                    _decimal(
                        weight_increment
                        if weight_increment is not None
                        else defaults["default_weight_increment"]
                    ),
                    weight_unit or defaults["weight_unit"],
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return _row_to_dict(cur, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def update_exercise(user_id: str, exercise_id: str, **fields) -> dict:
    allowed = {
        "name",
        "display_order",
        "notes",
        "target_reps",
        "target_sets",
        "weight_increment",
        "weight_unit",
    }
    updates = []
    values = []
    for key, value in fields.items():
        if key in allowed:
            updates.append(f"{key} = %s")
            values.append(_decimal(value) if key == "weight_increment" else value)
    if not updates:
        raise WeightliftingValidationError("No exercise fields supplied for update")
    updates.append("updated_at = now()")
    values.extend([exercise_id, user_id])

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE public.weightlifting_exercises
                SET {", ".join(updates)}
                WHERE id = %s
                  AND user_id = %s
                RETURNING id,
                          name,
                          display_order,
                          active,
                          notes,
                          target_reps,
                          target_sets,
                          weight_increment,
                          weight_unit,
                          created_at,
                          updated_at
                """,
                tuple(values),
            )
            row = cur.fetchone()
            if not row:
                raise WeightliftingNotFoundError(f"Exercise not found: {exercise_id}")
        conn.commit()
        return _row_to_dict(cur, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def reorder_exercises(*, user_id: str, exercises: list[dict]) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            for item in exercises:
                cur.execute(
                    """
                    UPDATE public.weightlifting_exercises
                    SET display_order = %s,
                        updated_at = now()
                    WHERE id = %s
                      AND user_id = %s
                      AND active = true
                    """,
                    (item["display_order"], str(item["id"]), user_id),
                )
                if cur.rowcount != 1:
                    raise WeightliftingNotFoundError(
                        f"Active exercise not found: {item['id']}"
                    )
        conn.commit()
        return list_exercises(user_id=user_id, include_archived=False)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def set_exercise_active(*, user_id: str, exercise_id: str, active: bool) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.weightlifting_exercises
                SET active = %s,
                    updated_at = now()
                WHERE id = %s
                  AND user_id = %s
                RETURNING id,
                          name,
                          display_order,
                          active,
                          notes,
                          target_reps,
                          target_sets,
                          weight_increment,
                          weight_unit,
                          created_at,
                          updated_at
                """,
                (active, exercise_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                raise WeightliftingNotFoundError(f"Exercise not found: {exercise_id}")
        conn.commit()
        return _row_to_dict(cur, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _entry_select() -> str:
    return """
        SELECT id,
               exercise_id,
               week_start,
               workout_day_slot,
               workout_date,
               weight,
               reps,
               sets,
               notes,
               completed,
               created_at,
               updated_at
        FROM public.weightlifting_entries
    """


def _get_active_exercise(cur, *, user_id: str, exercise_id: str) -> dict:
    cur.execute(
        _exercise_select()
        + """
        WHERE id = %s
          AND user_id = %s
          AND active = true
        """,
        (exercise_id, user_id),
    )
    exercise = _row_to_dict(cur, cur.fetchone())
    if not exercise:
        raise WeightliftingNotFoundError(f"Active exercise not found: {exercise_id}")
    return exercise


def upsert_entry(
    *,
    user_id: str,
    exercise_id: str,
    week_start: date,
    workout_day_slot: int,
    workout_date: date | None,
    weight,
    reps: int,
    sets: int | None,
    notes: str | None,
    completed: bool,
) -> dict:
    _validate_slot(workout_day_slot)
    normalized_week = _normalize_week_start(week_start)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _get_active_exercise(cur, user_id=user_id, exercise_id=exercise_id)
            cur.execute(
                """
                INSERT INTO public.weightlifting_entries (
                    user_id,
                    exercise_id,
                    week_start,
                    workout_day_slot,
                    workout_date,
                    weight,
                    reps,
                    sets,
                    notes,
                    completed
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (exercise_id, week_start, workout_day_slot)
                DO UPDATE SET
                    workout_date = EXCLUDED.workout_date,
                    weight = EXCLUDED.weight,
                    reps = EXCLUDED.reps,
                    sets = EXCLUDED.sets,
                    notes = EXCLUDED.notes,
                    completed = EXCLUDED.completed,
                    updated_at = now()
                WHERE weightlifting_entries.user_id = EXCLUDED.user_id
                RETURNING id,
                          exercise_id,
                          week_start,
                          workout_day_slot,
                          workout_date,
                          weight,
                          reps,
                          sets,
                          notes,
                          completed,
                          created_at,
                          updated_at
                """,
                (
                    user_id,
                    exercise_id,
                    normalized_week,
                    workout_day_slot,
                    workout_date,
                    _decimal(weight),
                    reps,
                    sets,
                    notes,
                    completed,
                ),
            )
            row = cur.fetchone()
            if not row:
                raise WeightliftingConflictError("Entry belongs to another user")
        conn.commit()
        return _row_to_dict(cur, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def update_entry(user_id: str, entry_id: str, **fields) -> dict:
    allowed = {"workout_date", "weight", "reps", "sets", "notes", "completed"}
    updates = []
    values = []
    for key, value in fields.items():
        if key in allowed:
            updates.append(f"{key} = %s")
            values.append(_decimal(value) if key == "weight" else value)
    if not updates:
        raise WeightliftingValidationError("No entry fields supplied for update")
    updates.append("updated_at = now()")
    values.extend([entry_id, user_id])

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE public.weightlifting_entries
                SET {", ".join(updates)}
                WHERE id = %s
                  AND user_id = %s
                RETURNING id,
                          exercise_id,
                          week_start,
                          workout_day_slot,
                          workout_date,
                          weight,
                          reps,
                          sets,
                          notes,
                          completed,
                          created_at,
                          updated_at
                """,
                tuple(values),
            )
            row = cur.fetchone()
            if not row:
                raise WeightliftingNotFoundError(f"Entry not found: {entry_id}")
        conn.commit()
        return _row_to_dict(cur, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def clear_entry(
    *,
    user_id: str,
    exercise_id: str,
    week_start: date,
    workout_day_slot: int,
) -> dict:
    _validate_slot(workout_day_slot)
    normalized_week = _normalize_week_start(week_start)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM public.weightlifting_entries
                WHERE user_id = %s
                  AND exercise_id = %s
                  AND week_start = %s
                  AND workout_day_slot = %s
                RETURNING id
                """,
                (user_id, exercise_id, normalized_week, workout_day_slot),
            )
            row = cur.fetchone()
        conn.commit()
        return {"deleted": bool(row), "exercise_id": exercise_id, "week_start": normalized_week.isoformat(), "workout_day_slot": workout_day_slot}
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def recommendation_for_exercise(exercise: dict, latest_entry: dict | None) -> dict:
    if not latest_entry:
        return {
            "weight": None,
            "reps": exercise["target_reps"],
            "sets": exercise["target_sets"],
            "reason_code": "no_history",
            "reason": "No completed workout has been recorded for this exercise.",
        }

    target_sets = exercise.get("target_sets")
    met_reps = latest_entry["reps"] >= exercise["target_reps"]
    met_sets = target_sets is None or (latest_entry.get("sets") or 0) >= target_sets
    previous_weight = _decimal(latest_entry["weight"])

    if met_reps and met_sets:
        return {
            "weight": _serialize_value(previous_weight + _decimal(exercise["weight_increment"])),
            "reps": exercise["target_reps"],
            "sets": target_sets,
            "reason_code": "target_met_increase",
            "reason": "Previous completed entry met the configured target.",
        }

    return {
        "weight": _serialize_value(previous_weight),
        "reps": exercise["target_reps"],
        "sets": target_sets,
        "reason_code": "target_not_met_repeat",
        "reason": "Previous completed entry did not meet the configured target.",
    }


def _day_date(week_start: date, weekday: str | None, slot: int) -> date:
    if weekday in WEEKDAY_OFFSETS:
        return week_start + timedelta(days=WEEKDAY_OFFSETS[weekday])
    return week_start + timedelta(days=slot - 1)


def get_weekly_grid(*, user_id: str, week_start: date) -> dict:
    normalized_week = _normalize_week_start(week_start)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _ensure_settings(cur, user_id)
            cur.execute(
                """
                SELECT weight_unit,
                       default_weight_increment,
                       default_target_reps,
                       default_sets,
                       created_at,
                       updated_at
                FROM public.weightlifting_settings
                WHERE user_id = %s
                """,
                (user_id,),
            )
            settings = _row_to_dict(cur, cur.fetchone())
            cur.execute(
                """
                SELECT slot,
                       label,
                       weekday
                FROM public.weightlifting_day_slots
                WHERE user_id = %s
                ORDER BY slot
                """,
                (user_id,),
            )
            days = _rows_to_dicts(cur, cur.fetchall())
            cur.execute(
                _exercise_select()
                + """
                WHERE user_id = %s
                  AND active = true
                ORDER BY display_order, name
                """,
                (user_id,),
            )
            exercises = _rows_to_dicts(cur, cur.fetchall())
            exercise_ids = [exercise["id"] for exercise in exercises]
            entries_by_exercise: dict[str, dict[str, dict]] = {
                exercise_id: {"1": None, "2": None, "3": None}
                for exercise_id in exercise_ids
            }
            if exercise_ids:
                cur.execute(
                    _entry_select()
                    + """
                    WHERE user_id = %s
                      AND week_start = %s
                      AND exercise_id = ANY(%s::uuid[])
                    ORDER BY exercise_id, workout_day_slot
                    """,
                    (user_id, normalized_week, exercise_ids),
                )
                for entry in _rows_to_dicts(cur, cur.fetchall()):
                    entries_by_exercise[entry["exercise_id"]][
                        str(entry["workout_day_slot"])
                    ] = entry
            latest_by_exercise = _latest_completed_entries(
                cur,
                user_id=user_id,
                exercise_ids=exercise_ids,
            )

        return {
            "week_start": normalized_week.isoformat(),
            "weight_unit": settings["weight_unit"],
            "days": [
                {
                    **day,
                    "date": _day_date(
                        normalized_week,
                        day.get("weekday"),
                        day["slot"],
                    ).isoformat(),
                }
                for day in days
            ],
            "exercises": [
                {
                    **exercise,
                    "previous_performance": latest_by_exercise.get(exercise["id"]),
                    "suggested_next": recommendation_for_exercise(
                        exercise,
                        latest_by_exercise.get(exercise["id"]),
                    ),
                    "entries": entries_by_exercise[exercise["id"]],
                }
                for exercise in exercises
            ],
        }
    finally:
        put_db_conn(conn)


def _latest_completed_entries(cur, *, user_id: str, exercise_ids: list[str]) -> dict[str, dict]:
    if not exercise_ids:
        return {}
    cur.execute(
        _entry_select()
        + """
        WHERE user_id = %s
          AND completed = true
          AND exercise_id = ANY(%s::uuid[])
        ORDER BY exercise_id,
                 week_start DESC,
                 workout_day_slot DESC,
                 workout_date DESC NULLS LAST,
                 updated_at DESC
        """,
        (user_id, exercise_ids),
    )
    latest = {}
    for entry in _rows_to_dicts(cur, cur.fetchall()):
        latest.setdefault(entry["exercise_id"], entry)
    return latest


def get_exercise_history(
    *,
    user_id: str,
    exercise_id: str,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    limit = max(1, min(limit, MAX_HISTORY_LIMIT))
    offset = max(0, offset)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                _exercise_select()
                + """
                WHERE id = %s
                  AND user_id = %s
                """,
                (exercise_id, user_id),
            )
            exercise = _row_to_dict(cur, cur.fetchone())
            if not exercise:
                raise WeightliftingNotFoundError(f"Exercise not found: {exercise_id}")
            cur.execute(
                _entry_select()
                + """
                WHERE user_id = %s
                  AND exercise_id = %s
                ORDER BY week_start DESC,
                         workout_day_slot DESC,
                         workout_date DESC NULLS LAST,
                         updated_at DESC
                LIMIT %s OFFSET %s
                """,
                (user_id, exercise_id, limit, offset),
            )
            entries = [
                {
                    **entry,
                    "recommendation_after": (
                        recommendation_for_exercise(exercise, entry)
                        if entry["completed"]
                        else None
                    ),
                }
                for entry in _rows_to_dicts(cur, cur.fetchall())
            ]
        chronological = list(reversed(entries))
        return {
            "exercise": exercise,
            "entries": entries,
            "series": [
                {
                    "entry_id": entry["id"],
                    "week_start": entry["week_start"],
                    "workout_date": entry["workout_date"],
                    "workout_day_slot": entry["workout_day_slot"],
                    "weight": entry["weight"],
                    "reps": entry["reps"],
                    "sets": entry["sets"],
                    "completed": entry["completed"],
                }
                for entry in chronological
            ],
            "suggested_next": recommendation_for_exercise(
                exercise,
                next((entry for entry in entries if entry["completed"]), None),
            ),
            "limit": limit,
            "offset": offset,
        }
    finally:
        put_db_conn(conn)

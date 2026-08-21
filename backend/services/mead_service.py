from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID

from psycopg2.extras import Json

from backend.database.database import get_db_conn, put_db_conn


ABV_FACTOR = Decimal("131.25")
DISPLAY_QUANT = Decimal("0.0001")
ABV_QUANT = Decimal("0.01")
TOSNA_DOSE_COUNT = 4
TOSNA_FUTURE_DOSES = (2, 3, 4)

STAGES = {"primary", "secondary", "aging", "bottled", "archived"}
EVENT_TYPES = {
    "gravity_reading",
    "note",
    "racking",
    "nutrient_addition",
    "stage_change",
    "other",
}
TASK_TYPES = {
    "check_gravity",
    "add_nutrients",
    "consider_racking",
    "check_clarity_taste",
    "consider_bottling",
    "custom",
}
TASK_STATUSES = {"pending", "completed", "cancelled"}


class MeadNotFoundError(ValueError):
    pass


class MeadValidationError(ValueError):
    pass


class MeadConflictError(ValueError):
    pass


def _serialize_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f")


def _serialize_value(value):
    if value is None:
        return None

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, Decimal):
        return _serialize_decimal(value)

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
        raise MeadValidationError("Invalid decimal value") from exc


def _nullable_decimal(value) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value)


def _quantize_display(value: Decimal) -> Decimal:
    return value.quantize(DISPLAY_QUANT, rounding=ROUND_HALF_UP).normalize()


def calculate_abv(original_gravity, specific_gravity) -> Decimal | None:
    if original_gravity is None or specific_gravity is None:
        return None
    return (
        (_decimal(original_gravity) - _decimal(specific_gravity)) * ABV_FACTOR
    ).quantize(ABV_QUANT, rounding=ROUND_HALF_UP)


def calculate_tosna_per_dose(total_amount) -> Decimal:
    amount = _decimal(total_amount)
    if amount <= 0:
        raise MeadValidationError("TOSNA total nutrient amount must be greater than 0")
    return _quantize_display(amount / Decimal(TOSNA_DOSE_COUNT))


def tosna_due_at(start_at: datetime, dose_number: int) -> datetime:
    if dose_number not in TOSNA_FUTURE_DOSES:
        raise MeadValidationError("TOSNA reminder dose must be 2, 3, or 4")
    return start_at + timedelta(hours=24 * (dose_number - 1))


def _tosna_description(
    *,
    dose_number: int,
    per_dose_amount: Decimal,
    unit: str,
    nutrient_name: str | None,
) -> str:
    nutrient = nutrient_name or "nutrients"
    return (
        f"Dose {dose_number} of 4: add "
        f"{_serialize_decimal(per_dose_amount)} {unit} {nutrient}."
    )


def build_tosna_task(
    *,
    batch_name: str,
    start_at: datetime,
    dose_number: int,
    total_amount,
    unit: str,
    nutrient_name: str | None,
) -> dict:
    per_dose_amount = calculate_tosna_per_dose(total_amount)
    due_at = tosna_due_at(start_at, dose_number)
    return {
        "task_type": "add_nutrients",
        "title": f"Add TOSNA Nutrients - {batch_name}",
        "description": _tosna_description(
            dose_number=dose_number,
            per_dose_amount=per_dose_amount,
            unit=unit,
            nutrient_name=nutrient_name,
        ),
        "due_at": due_at,
        "source": "tosna",
        "source_key": f"dose_{dose_number}_of_4",
        "metadata": {
            "tosna": True,
            "dose_number": dose_number,
            "dose_count": TOSNA_DOSE_COUNT,
            "per_dose_amount": _serialize_decimal(per_dose_amount),
            "unit": unit,
            "nutrient_name": nutrient_name,
        },
    }


def _batch_columns() -> str:
    return """
        id,
        user_id,
        name,
        start_at,
        stage,
        volume,
        volume_unit,
        original_gravity,
        target_final_gravity,
        notes,
        recipe_notes,
        tosna_enabled,
        tosna_nutrient_name,
        tosna_total_amount,
        tosna_unit,
        created_at,
        updated_at
    """


def _task_columns() -> str:
    return """
        id,
        batch_id,
        task_type,
        title,
        description,
        due_at,
        status,
        completed_at,
        notified_at,
        notified_due_at,
        source,
        source_key,
        metadata,
        created_at,
        updated_at
    """


def _qualified_task_columns(alias: str = "task") -> str:
    return f"""
        {alias}.id,
        {alias}.batch_id,
        {alias}.task_type,
        {alias}.title,
        {alias}.description,
        {alias}.due_at,
        {alias}.status,
        {alias}.completed_at,
        {alias}.notified_at,
        {alias}.notified_due_at,
        {alias}.source,
        {alias}.source_key,
        {alias}.metadata,
        {alias}.created_at,
        {alias}.updated_at
    """


def _event_columns() -> str:
    return """
        id,
        batch_id,
        event_at,
        event_type,
        gravity,
        notes,
        metadata,
        created_at,
        updated_at
    """


def _recipe_columns() -> str:
    return """
        id,
        batch_id,
        name,
        amount,
        unit,
        notes,
        display_order,
        created_at,
        updated_at
    """


def _batch_by_id(cur, *, user_id: str, batch_id: str) -> dict:
    cur.execute(
        f"""
        SELECT {_batch_columns()}
        FROM public.mead_batches
        WHERE id = %s
          AND user_id = %s
        """,
        (batch_id, user_id),
    )
    batch = _row_to_dict(cur, cur.fetchone())
    if not batch:
        raise MeadNotFoundError(f"Mead batch not found: {batch_id}")
    return batch


def _batch_row_by_id(cur, *, user_id: str, batch_id: str) -> dict:
    cur.execute(
        f"""
        SELECT {_batch_columns()}
        FROM public.mead_batches
        WHERE id = %s
          AND user_id = %s
        """,
        (batch_id, user_id),
    )
    row = cur.fetchone()
    batch = _row_to_dict(cur, row)
    if not batch:
        raise MeadNotFoundError(f"Mead batch not found: {batch_id}")
    return batch


def _latest_gravity(cur, batch_id: str) -> dict | None:
    cur.execute(
        f"""
        SELECT {_event_columns()}
        FROM public.mead_events
        WHERE batch_id = %s
          AND event_type = 'gravity_reading'
        ORDER BY event_at DESC, created_at DESC, id DESC
        LIMIT 1
        """,
        (batch_id,),
    )
    return _row_to_dict(cur, cur.fetchone())


def _next_pending_task(cur, batch_id: str) -> dict | None:
    cur.execute(
        f"""
        SELECT {_task_columns()}
        FROM public.mead_tasks
        WHERE batch_id = %s
          AND status = 'pending'
        ORDER BY due_at ASC, created_at ASC
        LIMIT 1
        """,
        (batch_id,),
    )
    return _row_to_dict(cur, cur.fetchone())


def _list_recipe_items(cur, batch_id: str) -> list[dict]:
    cur.execute(
        f"""
        SELECT {_recipe_columns()}
        FROM public.mead_recipe_items
        WHERE batch_id = %s
        ORDER BY display_order, created_at, id
        """,
        (batch_id,),
    )
    return _rows_to_dicts(cur, cur.fetchall())


def _list_events(cur, batch_id: str) -> list[dict]:
    cur.execute(
        f"""
        SELECT {_event_columns()}
        FROM public.mead_events
        WHERE batch_id = %s
        ORDER BY event_at, created_at, id
        """,
        (batch_id,),
    )
    return _rows_to_dicts(cur, cur.fetchall())


def _list_tasks(cur, batch_id: str, status: str | None = None) -> list[dict]:
    if status:
        cur.execute(
            f"""
            SELECT {_task_columns()}
            FROM public.mead_tasks
            WHERE batch_id = %s
              AND status = %s
            ORDER BY due_at, created_at, id
            """,
            (batch_id, status),
        )
    else:
        cur.execute(
            f"""
            SELECT {_task_columns()}
            FROM public.mead_tasks
            WHERE batch_id = %s
            ORDER BY status, due_at, created_at, id
            """,
            (batch_id,),
        )
    return _rows_to_dicts(cur, cur.fetchall())


def _tosna_summary(batch: dict) -> dict | None:
    if not batch.get("tosna_enabled"):
        return None
    total = batch.get("tosna_total_amount")
    unit = batch.get("tosna_unit")
    if total is None or unit is None:
        return None
    start_at = datetime.fromisoformat(batch["start_at"])
    per_dose = calculate_tosna_per_dose(total)
    return {
        "enabled": True,
        "nutrient_name": batch.get("tosna_nutrient_name"),
        "total_amount": _serialize_decimal(_decimal(total)),
        "unit": unit,
        "per_dose_amount": _serialize_decimal(per_dose),
        "doses": [
            {
                "dose_number": 1,
                "due_at": batch["start_at"],
                "amount": _serialize_decimal(per_dose),
                "unit": unit,
                "initial": True,
            },
            *[
                {
                    "dose_number": dose,
                    "due_at": tosna_due_at(start_at, dose).isoformat(),
                    "amount": _serialize_decimal(per_dose),
                    "unit": unit,
                    "initial": False,
                    "source_key": f"dose_{dose}_of_4",
                }
                for dose in TOSNA_FUTURE_DOSES
            ],
        ],
    }


def _decorate_batch(cur, batch: dict, *, include_detail: bool = False) -> dict:
    latest = _latest_gravity(cur, batch["id"])
    next_task = _next_pending_task(cur, batch["id"])
    latest_sg = latest["gravity"] if latest else None
    batch["latest_gravity"] = latest_sg
    batch["latest_gravity_event"] = latest
    batch["estimated_current_abv"] = _serialize_value(
        calculate_abv(batch["original_gravity"], latest_sg)
    )
    batch["projected_final_abv"] = _serialize_value(
        calculate_abv(batch["original_gravity"], batch.get("target_final_gravity"))
    )
    batch["next_pending_task"] = next_task
    batch["tosna_summary"] = _tosna_summary(batch)
    if include_detail:
        batch["recipe_items"] = _list_recipe_items(cur, batch["id"])
        batch["timeline"] = _list_events(cur, batch["id"])
        batch["tasks"] = _list_tasks(cur, batch["id"])
    return batch


def _validate_batch_fields(fields: dict) -> None:
    stage = fields.get("stage")
    if stage is not None and stage not in STAGES:
        raise MeadValidationError("Invalid Mead stage")
    if fields.get("tosna_enabled"):
        if fields.get("tosna_total_amount") is None:
            raise MeadValidationError("tosna_total_amount is required when TOSNA is enabled")
        if not fields.get("tosna_unit"):
            raise MeadValidationError("tosna_unit is required when TOSNA is enabled")


def _sync_tosna_tasks(cur, batch: dict) -> None:
    if not batch.get("tosna_enabled"):
        cur.execute(
            """
            UPDATE public.mead_tasks
            SET status = 'cancelled',
                updated_at = now()
            WHERE batch_id = %s
              AND source = 'tosna'
              AND status = 'pending'
            """,
            (batch["id"],),
        )
        return

    total = batch.get("tosna_total_amount")
    unit = batch.get("tosna_unit")
    if total is None or not unit:
        raise MeadValidationError("TOSNA total amount and unit are required")
    start_at = datetime.fromisoformat(batch["start_at"])

    for dose in TOSNA_FUTURE_DOSES:
        task = build_tosna_task(
            batch_name=batch["name"],
            start_at=start_at,
            dose_number=dose,
            total_amount=total,
            unit=unit,
            nutrient_name=batch.get("tosna_nutrient_name"),
        )
        cur.execute(
            """
            INSERT INTO public.mead_tasks (
                batch_id,
                task_type,
                title,
                description,
                due_at,
                source,
                source_key,
                metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (batch_id, source, source_key)
            WHERE source_key IS NOT NULL
            DO UPDATE SET
                task_type = EXCLUDED.task_type,
                title = EXCLUDED.title,
                description = EXCLUDED.description,
                due_at = EXCLUDED.due_at,
                status = 'pending',
                notified_at = CASE
                    WHEN mead_tasks.due_at = EXCLUDED.due_at
                    THEN mead_tasks.notified_at
                    ELSE NULL
                END,
                notified_due_at = CASE
                    WHEN mead_tasks.due_at = EXCLUDED.due_at
                    THEN mead_tasks.notified_due_at
                    ELSE NULL
                END,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            WHERE mead_tasks.status <> 'completed'
            """,
            (
                batch["id"],
                task["task_type"],
                task["title"],
                task["description"],
                task["due_at"],
                task["source"],
                task["source_key"],
                Json(task["metadata"]),
            ),
        )


def list_batches(*, user_id: str, include_archived: bool = False) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            if include_archived:
                cur.execute(
                    f"""
                    SELECT {_batch_columns()}
                    FROM public.mead_batches
                    WHERE user_id = %s
                    ORDER BY start_at DESC, created_at DESC
                    """,
                    (user_id,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT {_batch_columns()}
                    FROM public.mead_batches
                    WHERE user_id = %s
                      AND stage <> 'archived'
                    ORDER BY start_at DESC, created_at DESC
                    """,
                    (user_id,),
                )
            batches = _rows_to_dicts(cur, cur.fetchall())
            return [_decorate_batch(cur, batch) for batch in batches]
    finally:
        put_db_conn(conn)


def create_batch(*, user_id: str, **fields) -> dict:
    _validate_batch_fields(fields)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO public.mead_batches (
                    user_id,
                    name,
                    start_at,
                    stage,
                    volume,
                    volume_unit,
                    original_gravity,
                    target_final_gravity,
                    notes,
                    recipe_notes,
                    tosna_enabled,
                    tosna_nutrient_name,
                    tosna_total_amount,
                    tosna_unit
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_batch_columns()}
                """,
                (
                    user_id,
                    fields["name"],
                    fields["start_at"],
                    fields.get("stage", "primary"),
                    _decimal(fields["volume"]),
                    fields["volume_unit"],
                    _decimal(fields["original_gravity"]),
                    _nullable_decimal(fields.get("target_final_gravity")),
                    fields.get("notes"),
                    fields.get("recipe_notes"),
                    fields.get("tosna_enabled", False),
                    fields.get("tosna_nutrient_name"),
                    _nullable_decimal(fields.get("tosna_total_amount")),
                    fields.get("tosna_unit"),
                ),
            )
            batch = _row_to_dict(cur, cur.fetchone())
            _sync_tosna_tasks(cur, batch)
            batch = _decorate_batch(cur, batch, include_detail=True)
        conn.commit()
        return batch
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def get_batch(*, user_id: str, batch_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            batch = _batch_by_id(cur, user_id=user_id, batch_id=batch_id)
            return _decorate_batch(cur, batch, include_detail=True)
    finally:
        put_db_conn(conn)


def update_batch(user_id: str, batch_id: str, **fields) -> dict:
    allowed = {
        "name",
        "start_at",
        "stage",
        "volume",
        "volume_unit",
        "original_gravity",
        "target_final_gravity",
        "notes",
        "recipe_notes",
        "tosna_enabled",
        "tosna_nutrient_name",
        "tosna_total_amount",
        "tosna_unit",
    }
    updates = []
    values = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        updates.append(f"{key} = %s")
        if key in {"volume", "original_gravity", "target_final_gravity", "tosna_total_amount"}:
            values.append(_nullable_decimal(value))
        else:
            values.append(value)
    if not updates:
        raise MeadValidationError("No batch fields supplied for update")

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            current = _batch_row_by_id(cur, user_id=user_id, batch_id=batch_id)
            merged = {**current, **fields}
            _validate_batch_fields(merged)
            updates.append("updated_at = now()")
            values.extend([batch_id, user_id])
            cur.execute(
                f"""
                UPDATE public.mead_batches
                SET {", ".join(updates)}
                WHERE id = %s
                  AND user_id = %s
                RETURNING {_batch_columns()}
                """,
                tuple(values),
            )
            batch = _row_to_dict(cur, cur.fetchone())
            if not batch:
                raise MeadNotFoundError(f"Mead batch not found: {batch_id}")
            if "stage" in fields and fields["stage"] != current["stage"]:
                cur.execute(
                    """
                    INSERT INTO public.mead_events (
                        batch_id,
                        event_at,
                        event_type,
                        notes,
                        metadata
                    )
                    VALUES (%s, now(), 'stage_change', %s, %s)
                    """,
                    (
                        batch_id,
                        f"Stage changed from {current['stage']} to {fields['stage']}.",
                        Json({"from_stage": current["stage"], "to_stage": fields["stage"]}),
                    ),
                )
            _sync_tosna_tasks(cur, batch)
            batch = _decorate_batch(cur, batch, include_detail=True)
        conn.commit()
        return batch
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def archive_batch(*, user_id: str, batch_id: str) -> dict:
    return update_batch(user_id, batch_id, stage="archived")


def replace_recipe_items(
    *,
    user_id: str,
    batch_id: str,
    items: list[dict],
) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _batch_row_by_id(cur, user_id=user_id, batch_id=batch_id)
            cur.execute(
                "DELETE FROM public.mead_recipe_items WHERE batch_id = %s",
                (batch_id,),
            )
            for index, item in enumerate(items):
                cur.execute(
                    """
                    INSERT INTO public.mead_recipe_items (
                        batch_id,
                        name,
                        amount,
                        unit,
                        notes,
                        display_order
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        batch_id,
                        item["name"],
                        _nullable_decimal(item.get("amount")),
                        item.get("unit"),
                        item.get("notes"),
                        item.get("display_order", index + 1),
                    ),
                )
            items_out = _list_recipe_items(cur, batch_id)
        conn.commit()
        return items_out
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def create_recipe_item(*, user_id: str, batch_id: str, **fields) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _batch_row_by_id(cur, user_id=user_id, batch_id=batch_id)
            display_order = fields.get("display_order")
            if display_order is None:
                cur.execute(
                    """
                    SELECT COALESCE(MAX(display_order), 0) + 1
                    FROM public.mead_recipe_items
                    WHERE batch_id = %s
                    """,
                    (batch_id,),
                )
                display_order = cur.fetchone()[0]
            cur.execute(
                f"""
                INSERT INTO public.mead_recipe_items (
                    batch_id,
                    name,
                    amount,
                    unit,
                    notes,
                    display_order
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING {_recipe_columns()}
                """,
                (
                    batch_id,
                    fields["name"],
                    _nullable_decimal(fields.get("amount")),
                    fields.get("unit"),
                    fields.get("notes"),
                    display_order,
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


def update_recipe_item(user_id: str, item_id: str, **fields) -> dict:
    allowed = {"name", "amount", "unit", "notes", "display_order"}
    updates = []
    values = []
    for key, value in fields.items():
        if key in allowed:
            updates.append(f"{key} = %s")
            values.append(_nullable_decimal(value) if key == "amount" else value)
    if not updates:
        raise MeadValidationError("No recipe item fields supplied for update")
    updates.append("updated_at = now()")
    values.extend([item_id, user_id])

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE public.mead_recipe_items AS item
                SET {", ".join(updates)}
                FROM public.mead_batches AS batch
                WHERE item.id = %s
                  AND item.batch_id = batch.id
                  AND batch.user_id = %s
                RETURNING item.id,
                          item.batch_id,
                          item.name,
                          item.amount,
                          item.unit,
                          item.notes,
                          item.display_order,
                          item.created_at,
                          item.updated_at
                """,
                tuple(values),
            )
            row = cur.fetchone()
            if not row:
                raise MeadNotFoundError(f"Recipe item not found: {item_id}")
        conn.commit()
        return _row_to_dict(cur, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def delete_recipe_item(*, user_id: str, item_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM public.mead_recipe_items AS item
                USING public.mead_batches AS batch
                WHERE item.id = %s
                  AND item.batch_id = batch.id
                  AND batch.user_id = %s
                RETURNING item.id, item.batch_id
                """,
                (item_id, user_id),
            )
            row = cur.fetchone()
        conn.commit()
        return {
            "deleted": bool(row),
            "id": str(row[0]) if row else item_id,
            "batch_id": str(row[1]) if row else None,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def add_event(*, user_id: str, batch_id: str, **fields) -> dict:
    event_type = fields["event_type"]
    if event_type not in EVENT_TYPES:
        raise MeadValidationError("Invalid Mead event type")
    if event_type == "gravity_reading" and fields.get("gravity") is None:
        raise MeadValidationError("gravity is required for gravity readings")
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _batch_row_by_id(cur, user_id=user_id, batch_id=batch_id)
            cur.execute(
                f"""
                INSERT INTO public.mead_events (
                    batch_id,
                    event_at,
                    event_type,
                    gravity,
                    notes
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING {_event_columns()}
                """,
                (
                    batch_id,
                    fields["event_at"],
                    event_type,
                    _nullable_decimal(fields.get("gravity")),
                    fields.get("notes"),
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


def add_gravity_reading(
    *,
    user_id: str,
    batch_id: str,
    event_at: datetime,
    gravity,
    notes: str | None = None,
) -> dict:
    return add_event(
        user_id=user_id,
        batch_id=batch_id,
        event_at=event_at,
        event_type="gravity_reading",
        gravity=gravity,
        notes=notes,
    )


def get_timeline(*, user_id: str, batch_id: str) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _batch_row_by_id(cur, user_id=user_id, batch_id=batch_id)
            return _list_events(cur, batch_id)
    finally:
        put_db_conn(conn)


def list_tasks(*, user_id: str, batch_id: str, status: str | None = None) -> list[dict]:
    if status and status not in TASK_STATUSES:
        raise MeadValidationError("Invalid task status")
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _batch_row_by_id(cur, user_id=user_id, batch_id=batch_id)
            return _list_tasks(cur, batch_id, status)
    finally:
        put_db_conn(conn)


def create_task(*, user_id: str, batch_id: str, **fields) -> dict:
    task_type = fields.get("task_type", "custom")
    if task_type not in TASK_TYPES:
        raise MeadValidationError("Invalid Mead task type")
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _batch_row_by_id(cur, user_id=user_id, batch_id=batch_id)
            cur.execute(
                f"""
                INSERT INTO public.mead_tasks (
                    batch_id,
                    task_type,
                    title,
                    description,
                    due_at
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING {_task_columns()}
                """,
                (
                    batch_id,
                    task_type,
                    fields["title"],
                    fields.get("description"),
                    fields["due_at"],
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


def _task_by_id(
    cur,
    *,
    user_id: str,
    task_id: str,
    for_update: bool = False,
) -> dict:
    lock_clause = "FOR UPDATE" if for_update else ""
    cur.execute(
        f"""
        SELECT {_qualified_task_columns("task")}
        FROM public.mead_tasks AS task
        JOIN public.mead_batches AS batch
          ON batch.id = task.batch_id
        WHERE task.id = %s
          AND batch.user_id = %s
        {lock_clause}
        """,
        (task_id, user_id),
    )
    task = _row_to_dict(cur, cur.fetchone())
    if not task:
        raise MeadNotFoundError(f"Mead task not found: {task_id}")
    return task


def complete_task(
    *,
    user_id: str,
    task_id: str,
    completed_at: datetime | None = None,
    notes: str | None = None,
) -> dict:
    completed = completed_at or datetime.now(timezone.utc)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            task = _task_by_id(
                cur,
                user_id=user_id,
                task_id=task_id,
                for_update=True,
            )
            if task["status"] == "cancelled":
                raise MeadConflictError("Cancelled tasks cannot be completed")
            if task["status"] == "completed":
                conn.commit()
                return task
            cur.execute(
                f"""
                UPDATE public.mead_tasks
                SET status = 'completed',
                    completed_at = %s,
                    updated_at = now()
                WHERE id = %s
                  AND status = 'pending'
                RETURNING {_task_columns()}
                """,
                (completed, task_id),
            )
            updated = _row_to_dict(cur, cur.fetchone())
            if not updated:
                raise MeadConflictError("Task could not be completed")
            if task["source"] == "tosna" and task["task_type"] == "add_nutrients":
                event_notes = notes or task["description"]
                cur.execute(
                    """
                    INSERT INTO public.mead_events (
                        batch_id,
                        event_at,
                        event_type,
                        notes,
                        metadata
                    )
                    VALUES (%s, %s, 'nutrient_addition', %s, %s)
                    """,
                    (
                        task["batch_id"],
                        completed,
                        event_notes,
                        Json(
                            {
                                "task_id": task["id"],
                                "source": "tosna",
                                **(task.get("metadata") or {}),
                            }
                        ),
                    ),
                )
        conn.commit()
        return updated
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def reschedule_task(*, user_id: str, task_id: str, due_at: datetime) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            task = _task_by_id(cur, user_id=user_id, task_id=task_id)
            if task["status"] != "pending":
                raise MeadConflictError("Only pending tasks can be rescheduled")
            cur.execute(
                f"""
                UPDATE public.mead_tasks
                SET due_at = %s,
                    notified_at = NULL,
                    notified_due_at = NULL,
                    updated_at = now()
                WHERE id = %s
                RETURNING {_task_columns()}
                """,
                (due_at, task_id),
            )
            row = cur.fetchone()
        conn.commit()
        return _row_to_dict(cur, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def cancel_task(*, user_id: str, task_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            task = _task_by_id(
                cur,
                user_id=user_id,
                task_id=task_id,
                for_update=True,
            )
            if task["status"] == "completed":
                raise MeadConflictError("Completed tasks cannot be cancelled")
            if task["status"] == "cancelled":
                conn.commit()
                return task
            cur.execute(
                f"""
                UPDATE public.mead_tasks
                SET status = 'cancelled',
                    updated_at = now()
                WHERE id = %s
                  AND status = 'pending'
                RETURNING {_task_columns()}
                """,
                (task_id,),
            )
            row = cur.fetchone()
            if not row:
                raise MeadConflictError("Task could not be cancelled")
        conn.commit()
        return _row_to_dict(cur, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)

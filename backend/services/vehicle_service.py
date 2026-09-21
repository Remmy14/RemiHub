from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID

from backend.database.database import get_db_conn, put_db_conn


ODOMETER_SOURCES = {"MANUAL", "FUEL", "MAINTENANCE"}
DRIVING_CONTEXTS = {"NORMAL", "TOWING", "MIXED"}
MAINTENANCE_ACTIONS = {"REPLACED", "SERVICED", "INSPECTED", "ROTATED", "REPAIRED", "OTHER"}
MAINTENANCE_STATUSES = ("OK", "DUE_SOON", "DUE", "OVERDUE")
STATUS_RANK = {status: index for index, status in enumerate(MAINTENANCE_STATUSES)}

MONEY_QUANT = Decimal("0.01")
GALLONS_QUANT = Decimal("0.001")
PRICE_QUANT = Decimal("0.001")
ODOMETER_QUANT = Decimal("0.1")
MPG_QUANT = Decimal("0.01")
FUEL_TOLERANCE = Decimal("0.02")


class VehicleNotFoundError(ValueError):
    pass


class VehicleValidationError(ValueError):
    pass


class VehicleConflictError(ValueError):
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
        raise VehicleValidationError("Invalid decimal value") from exc


def _nullable_decimal(value) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value)


def _datetime_value(value) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise VehicleValidationError("Invalid datetime value") from exc


def _date_from_datetime(value) -> date:
    return _datetime_value(value).date()


def _quantize(value, quant: Decimal) -> Decimal:
    return _decimal(value).quantize(quant, rounding=ROUND_HALF_UP)


def _money(value) -> Decimal:
    return _quantize(value, MONEY_QUANT)


def _gallons(value) -> Decimal:
    return _quantize(value, GALLONS_QUANT)


def _price(value) -> Decimal:
    return _quantize(value, PRICE_QUANT)


def _odometer(value) -> Decimal:
    return _quantize(value, ODOMETER_QUANT)


def _vehicle_columns(alias: str = "vehicle") -> str:
    return f"""
        {alias}.id,
        {alias}.user_id,
        {alias}.year,
        {alias}.make,
        {alias}.model,
        {alias}.trim,
        {alias}.engine,
        {alias}.fuel_type,
        {alias}.tank_capacity_gallons,
        {alias}.vin,
        {alias}.license_plate,
        {alias}.license_state,
        {alias}.purchase_date,
        {alias}.purchase_odometer_miles,
        {alias}.active,
        {alias}.created_at,
        {alias}.updated_at
    """


def _odometer_columns(alias: str = "observation") -> str:
    return f"""
        {alias}.id,
        {alias}.vehicle_id,
        {alias}.odometer_miles,
        {alias}.observed_at,
        {alias}.source,
        {alias}.source_record_type,
        {alias}.source_record_id,
        {alias}.created_at
    """


def _fuel_columns(alias: str = "fuel") -> str:
    return f"""
        {alias}.id,
        {alias}.vehicle_id,
        {alias}.filled_at,
        {alias}.odometer_miles,
        {alias}.gallons,
        {alias}.total_cost,
        {alias}.price_per_gallon,
        {alias}.is_full_fill,
        {alias}.driving_context,
        {alias}.fuel_grade,
        {alias}.station_name,
        {alias}.notes,
        {alias}.created_at,
        {alias}.updated_at
    """


def _schedule_columns(alias: str = "schedule") -> str:
    return f"""
        {alias}.id,
        {alias}.vehicle_id,
        {alias}.name,
        {alias}.category,
        {alias}.description,
        {alias}.mileage_interval_miles,
        {alias}.time_interval_days,
        {alias}.last_service_odometer_miles,
        {alias}.last_service_date,
        {alias}.due_soon_miles,
        {alias}.due_soon_days,
        {alias}.overdue_miles,
        {alias}.overdue_days,
        {alias}.active,
        {alias}.created_at,
        {alias}.updated_at
    """


def _event_columns(alias: str = "event") -> str:
    return f"""
        {alias}.id,
        {alias}.vehicle_id,
        {alias}.maintenance_schedule_id,
        {alias}.performed_at,
        {alias}.odometer_miles,
        {alias}.category,
        {alias}.action,
        {alias}.description,
        {alias}.cost,
        {alias}.service_provider,
        {alias}.notes,
        {alias}.created_at,
        {alias}.updated_at
    """


def _get_vehicle_row(cur, *, user_id: str, vehicle_id: str) -> dict:
    cur.execute(
        f"""
        SELECT {_vehicle_columns()}
        FROM public.vehicles AS vehicle
        WHERE vehicle.id = %s
          AND vehicle.user_id = %s
        """,
        (vehicle_id, user_id),
    )
    vehicle = _row_to_dict(cur, cur.fetchone())
    if not vehicle:
        raise VehicleNotFoundError(f"Vehicle not found: {vehicle_id}")
    return vehicle


def _current_odometer(cur, *, vehicle_id: str) -> dict | None:
    cur.execute(
        f"""
        SELECT {_odometer_columns()}
        FROM public.vehicle_odometer_observations AS observation
        WHERE observation.vehicle_id = %s
        ORDER BY observation.observed_at DESC,
                 observation.created_at DESC,
                 observation.id DESC
        LIMIT 1
        """,
        (vehicle_id,),
    )
    return _row_to_dict(cur, cur.fetchone())


def _assert_vehicle_owner(cur, *, user_id: str, vehicle_id: str) -> None:
    _get_vehicle_row(cur, user_id=user_id, vehicle_id=vehicle_id)


def _validate_odometer(odometer_miles) -> Decimal:
    miles = _odometer(odometer_miles)
    if miles < 0:
        raise VehicleValidationError("Odometer miles cannot be negative")
    return miles


def normalize_fuel_amounts(*, gallons=None, total_cost=None, price_per_gallon=None) -> dict:
    supplied = {
        "gallons": _nullable_decimal(gallons),
        "total_cost": _nullable_decimal(total_cost),
        "price_per_gallon": _nullable_decimal(price_per_gallon),
    }
    provided = [name for name, value in supplied.items() if value is not None]
    if len(provided) < 2:
        raise VehicleValidationError(
            "At least two of gallons, total_cost, and price_per_gallon are required"
        )

    for name, value in supplied.items():
        if value is not None and value <= 0:
            raise VehicleValidationError(f"{name} must be greater than zero")

    if supplied["gallons"] is None:
        supplied["gallons"] = supplied["total_cost"] / supplied["price_per_gallon"]
    if supplied["total_cost"] is None:
        supplied["total_cost"] = supplied["gallons"] * supplied["price_per_gallon"]
    if supplied["price_per_gallon"] is None:
        supplied["price_per_gallon"] = supplied["total_cost"] / supplied["gallons"]

    gallons_value = _gallons(supplied["gallons"])
    total_value = _money(supplied["total_cost"])
    price_value = _price(supplied["price_per_gallon"])
    expected_total = _money(gallons_value * price_value)
    if abs(expected_total - total_value) > FUEL_TOLERANCE:
        raise VehicleValidationError("Fuel gallons, total_cost, and price_per_gallon are inconsistent")

    return {
        "gallons": gallons_value,
        "total_cost": total_value,
        "price_per_gallon": price_value,
    }


def _validate_context(value: str) -> str:
    if value not in DRIVING_CONTEXTS:
        raise VehicleValidationError("Invalid driving context")
    return value


def _validate_action(value: str) -> str:
    if value not in MAINTENANCE_ACTIONS:
        raise VehicleValidationError("Invalid maintenance action")
    return value


def _upsert_linked_odometer(
    cur,
    *,
    vehicle_id: str,
    odometer_miles,
    observed_at: datetime,
    source: str,
    source_record_type: str,
    source_record_id: str,
) -> dict:
    miles = _validate_odometer(odometer_miles)
    if source not in {"FUEL", "MAINTENANCE"}:
        raise VehicleValidationError("Linked odometer source must be FUEL or MAINTENANCE")
    cur.execute(
        f"""
        INSERT INTO public.vehicle_odometer_observations AS observation (
            vehicle_id,
            odometer_miles,
            observed_at,
            source,
            source_record_type,
            source_record_id
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (vehicle_id, source, source_record_type, source_record_id)
        WHERE source_record_id IS NOT NULL
        DO UPDATE SET
            odometer_miles = EXCLUDED.odometer_miles,
            observed_at = EXCLUDED.observed_at
        RETURNING {_odometer_columns()}
        """,
        (
            vehicle_id,
            miles,
            observed_at,
            source,
            source_record_type,
            source_record_id,
        ),
    )
    return _row_to_dict(cur, cur.fetchone())


def list_vehicles(*, user_id: str, include_archived: bool = False) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            if include_archived:
                cur.execute(
                    f"""
                    SELECT {_vehicle_columns()}
                    FROM public.vehicles AS vehicle
                    WHERE vehicle.user_id = %s
                    ORDER BY vehicle.active DESC, vehicle.year DESC, vehicle.make, vehicle.model
                    """,
                    (user_id,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT {_vehicle_columns()}
                    FROM public.vehicles AS vehicle
                    WHERE vehicle.user_id = %s
                      AND vehicle.active = true
                    ORDER BY vehicle.year DESC, vehicle.make, vehicle.model
                    """,
                    (user_id,),
                )
            return _rows_to_dicts(cur, cur.fetchall())
    finally:
        put_db_conn(conn)


def create_vehicle(*, user_id: str, **fields) -> dict:
    conn = get_db_conn()
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO public.vehicles AS vehicle (
                        user_id,
                        year,
                        make,
                        model,
                        trim,
                        engine,
                        fuel_type,
                        tank_capacity_gallons,
                        vin,
                        license_plate,
                        license_state,
                        purchase_date,
                        purchase_odometer_miles
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {_vehicle_columns()}
                    """,
                    (
                        user_id,
                        fields["year"],
                        fields["make"],
                        fields["model"],
                        fields.get("trim"),
                        fields.get("engine"),
                        fields["fuel_type"],
                        _nullable_decimal(fields.get("tank_capacity_gallons")),
                        fields.get("vin"),
                        fields.get("license_plate"),
                        fields.get("license_state"),
                        fields.get("purchase_date"),
                        _nullable_decimal(fields.get("purchase_odometer_miles")),
                    ),
                )
                vehicle = _row_to_dict(cur, cur.fetchone())
                if fields.get("purchase_odometer_miles") is not None:
                    cur.execute(
                        f"""
                        INSERT INTO public.vehicle_odometer_observations AS observation (
                            vehicle_id,
                            odometer_miles,
                            observed_at,
                            source
                        )
                        VALUES (%s, %s, %s, 'MANUAL')
                        RETURNING {_odometer_columns()}
                        """,
                        (
                            vehicle["id"],
                            _validate_odometer(fields["purchase_odometer_miles"]),
                            datetime.now(timezone.utc),
                        ),
                    )
                conn.commit()
                return vehicle
        except Exception:
            conn.rollback()
            raise
    finally:
        put_db_conn(conn)


def get_vehicle(*, user_id: str, vehicle_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            vehicle = _get_vehicle_row(cur, user_id=user_id, vehicle_id=vehicle_id)
            vehicle["current_odometer"] = _current_odometer(cur, vehicle_id=vehicle_id)
            return vehicle
    finally:
        put_db_conn(conn)


def update_vehicle(user_id: str, vehicle_id: str, **fields) -> dict:
    if not fields:
        raise VehicleValidationError("No vehicle fields supplied for update")

    allowed = {
        "year",
        "make",
        "model",
        "trim",
        "engine",
        "fuel_type",
        "tank_capacity_gallons",
        "vin",
        "license_plate",
        "license_state",
        "purchase_date",
        "purchase_odometer_miles",
    }
    updates = []
    values = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in {"tank_capacity_gallons", "purchase_odometer_miles"}:
            value = _nullable_decimal(value)
        updates.append(f"{key} = %s")
        values.append(value)
    if not updates:
        raise VehicleValidationError("No vehicle fields supplied for update")

    values.extend([vehicle_id, user_id])
    conn = get_db_conn()
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE public.vehicles AS vehicle
                    SET {", ".join(updates)},
                        updated_at = CURRENT_TIMESTAMP
                    WHERE vehicle.id = %s
                      AND vehicle.user_id = %s
                    RETURNING {_vehicle_columns()}
                    """,
                    values,
                )
                vehicle = _row_to_dict(cur, cur.fetchone())
                if not vehicle:
                    raise VehicleNotFoundError(f"Vehicle not found: {vehicle_id}")
            conn.commit()
            return vehicle
        except Exception:
            conn.rollback()
            raise
    finally:
        put_db_conn(conn)


def archive_vehicle(*, user_id: str, vehicle_id: str) -> dict:
    conn = get_db_conn()
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE public.vehicles AS vehicle
                    SET active = false,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE vehicle.id = %s
                      AND vehicle.user_id = %s
                    RETURNING {_vehicle_columns()}
                    """,
                    (vehicle_id, user_id),
                )
                vehicle = _row_to_dict(cur, cur.fetchone())
                if not vehicle:
                    raise VehicleNotFoundError(f"Vehicle not found: {vehicle_id}")
            conn.commit()
            return vehicle
        except Exception:
            conn.rollback()
            raise
    finally:
        put_db_conn(conn)


def restore_vehicle(*, user_id: str, vehicle_id: str) -> dict:
    conn = get_db_conn()
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE public.vehicles AS vehicle
                    SET active = true,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE vehicle.id = %s
                      AND vehicle.user_id = %s
                    RETURNING {_vehicle_columns()}
                    """,
                    (vehicle_id, user_id),
                )
                vehicle = _row_to_dict(cur, cur.fetchone())
                if not vehicle:
                    raise VehicleNotFoundError(f"Vehicle not found: {vehicle_id}")
            conn.commit()
            return vehicle
        except Exception:
            conn.rollback()
            raise
    finally:
        put_db_conn(conn)


def create_manual_odometer_observation(
    *,
    user_id: str,
    vehicle_id: str,
    odometer_miles,
    observed_at: datetime,
) -> dict:
    conn = get_db_conn()
    try:
        try:
            with conn.cursor() as cur:
                _assert_vehicle_owner(cur, user_id=user_id, vehicle_id=vehicle_id)
                cur.execute(
                    f"""
                    INSERT INTO public.vehicle_odometer_observations AS observation (
                        vehicle_id,
                        odometer_miles,
                        observed_at,
                        source
                    )
                    VALUES (%s, %s, %s, 'MANUAL')
                    RETURNING {_odometer_columns()}
                    """,
                    (vehicle_id, _validate_odometer(odometer_miles), observed_at),
                )
                observation = _row_to_dict(cur, cur.fetchone())
            conn.commit()
            return observation
        except Exception:
            conn.rollback()
            raise
    finally:
        put_db_conn(conn)


def list_odometer_observations(*, user_id: str, vehicle_id: str) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _assert_vehicle_owner(cur, user_id=user_id, vehicle_id=vehicle_id)
            cur.execute(
                f"""
                SELECT {_odometer_columns()}
                FROM public.vehicle_odometer_observations AS observation
                WHERE observation.vehicle_id = %s
                ORDER BY observation.observed_at DESC,
                         observation.created_at DESC,
                         observation.id DESC
                """,
                (vehicle_id,),
            )
            return _rows_to_dicts(cur, cur.fetchall())
    finally:
        put_db_conn(conn)


def create_fuel_record(*, user_id: str, vehicle_id: str, **fields) -> dict:
    amounts = normalize_fuel_amounts(
        gallons=fields.get("gallons"),
        total_cost=fields.get("total_cost"),
        price_per_gallon=fields.get("price_per_gallon"),
    )
    context = _validate_context(fields.get("driving_context", "NORMAL"))
    odometer = _validate_odometer(fields["odometer_miles"])
    conn = get_db_conn()
    try:
        try:
            with conn.cursor() as cur:
                _assert_vehicle_owner(cur, user_id=user_id, vehicle_id=vehicle_id)
                cur.execute(
                    f"""
                    INSERT INTO public.vehicle_fuel_records AS fuel (
                        vehicle_id,
                        filled_at,
                        odometer_miles,
                        gallons,
                        total_cost,
                        price_per_gallon,
                        is_full_fill,
                        driving_context,
                        fuel_grade,
                        station_name,
                        notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {_fuel_columns()}
                    """,
                    (
                        vehicle_id,
                        fields["filled_at"],
                        odometer,
                        amounts["gallons"],
                        amounts["total_cost"],
                        amounts["price_per_gallon"],
                        fields.get("is_full_fill", True),
                        context,
                        fields.get("fuel_grade"),
                        fields.get("station_name"),
                        fields.get("notes"),
                    ),
                )
                record = _row_to_dict(cur, cur.fetchone())
                _upsert_linked_odometer(
                    cur,
                    vehicle_id=vehicle_id,
                    odometer_miles=odometer,
                    observed_at=fields["filled_at"],
                    source="FUEL",
                    source_record_type="fuel_record",
                    source_record_id=record["id"],
                )
            conn.commit()
            return record
        except Exception:
            conn.rollback()
            raise
    finally:
        put_db_conn(conn)


def _get_fuel_record(cur, *, user_id: str, vehicle_id: str, fuel_record_id: str) -> dict:
    cur.execute(
        f"""
        SELECT {_fuel_columns()}
        FROM public.vehicle_fuel_records AS fuel
        INNER JOIN public.vehicles AS vehicle
          ON vehicle.id = fuel.vehicle_id
        WHERE fuel.id = %s
          AND fuel.vehicle_id = %s
          AND vehicle.user_id = %s
        """,
        (fuel_record_id, vehicle_id, user_id),
    )
    record = _row_to_dict(cur, cur.fetchone())
    if not record:
        raise VehicleNotFoundError(f"Fuel record not found: {fuel_record_id}")
    return record


def get_fuel_record(*, user_id: str, vehicle_id: str, fuel_record_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            return _get_fuel_record(
                cur,
                user_id=user_id,
                vehicle_id=vehicle_id,
                fuel_record_id=fuel_record_id,
            )
    finally:
        put_db_conn(conn)


def list_fuel_records(*, user_id: str, vehicle_id: str) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _assert_vehicle_owner(cur, user_id=user_id, vehicle_id=vehicle_id)
            cur.execute(
                f"""
                SELECT {_fuel_columns()}
                FROM public.vehicle_fuel_records AS fuel
                WHERE fuel.vehicle_id = %s
                ORDER BY fuel.filled_at DESC, fuel.created_at DESC, fuel.id DESC
                """,
                (vehicle_id,),
            )
            return _rows_to_dicts(cur, cur.fetchall())
    finally:
        put_db_conn(conn)


def update_fuel_record(user_id: str, vehicle_id: str, fuel_record_id: str, **fields) -> dict:
    if not fields:
        raise VehicleValidationError("No fuel record fields supplied for update")
    conn = get_db_conn()
    try:
        try:
            with conn.cursor() as cur:
                current = _get_fuel_record(
                    cur,
                    user_id=user_id,
                    vehicle_id=vehicle_id,
                    fuel_record_id=fuel_record_id,
                )
                merged = {**current, **fields}
                amounts = normalize_fuel_amounts(
                    gallons=merged.get("gallons"),
                    total_cost=merged.get("total_cost"),
                    price_per_gallon=merged.get("price_per_gallon"),
                )
                context = _validate_context(merged["driving_context"])
                odometer = _validate_odometer(merged["odometer_miles"])
                cur.execute(
                    f"""
                    UPDATE public.vehicle_fuel_records AS fuel
                    SET filled_at = %s,
                        odometer_miles = %s,
                        gallons = %s,
                        total_cost = %s,
                        price_per_gallon = %s,
                        is_full_fill = %s,
                        driving_context = %s,
                        fuel_grade = %s,
                        station_name = %s,
                        notes = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE fuel.id = %s
                      AND fuel.vehicle_id = %s
                    RETURNING {_fuel_columns()}
                    """,
                    (
                        merged["filled_at"],
                        odometer,
                        amounts["gallons"],
                        amounts["total_cost"],
                        amounts["price_per_gallon"],
                        merged["is_full_fill"],
                        context,
                        merged.get("fuel_grade"),
                        merged.get("station_name"),
                        merged.get("notes"),
                        fuel_record_id,
                        vehicle_id,
                    ),
                )
                record = _row_to_dict(cur, cur.fetchone())
                _upsert_linked_odometer(
                    cur,
                    vehicle_id=vehicle_id,
                    odometer_miles=odometer,
                    observed_at=merged["filled_at"],
                    source="FUEL",
                    source_record_type="fuel_record",
                    source_record_id=fuel_record_id,
                )
            conn.commit()
            return record
        except Exception:
            conn.rollback()
            raise
    finally:
        put_db_conn(conn)


def build_fuel_intervals(records: list[dict]) -> list[dict]:
    ordered = sorted(records, key=lambda item: (item["filled_at"], item.get("created_at") or "", item["id"]))
    intervals: list[dict] = []
    baseline = None
    gallons_since_baseline = Decimal("0")
    cost_since_baseline = Decimal("0")
    contexts_since_baseline: list[str] = []

    for record in ordered:
        gallons_value = _decimal(record["gallons"])
        cost_value = _decimal(record["total_cost"])
        context = record["driving_context"]

        if baseline is None:
            if record["is_full_fill"]:
                baseline = record
                gallons_since_baseline = Decimal("0")
                cost_since_baseline = Decimal("0")
                contexts_since_baseline = []
            continue

        gallons_since_baseline += gallons_value
        cost_since_baseline += cost_value
        contexts_since_baseline.append(context)

        if not record["is_full_fill"]:
            continue

        miles = _decimal(record["odometer_miles"]) - _decimal(baseline["odometer_miles"])
        if miles > 0 and gallons_since_baseline > 0:
            interval_context = (
                contexts_since_baseline[0]
                if contexts_since_baseline
                and len(set(contexts_since_baseline)) == 1
                and contexts_since_baseline[0] != "MIXED"
                else "MIXED"
            )
            intervals.append(
                {
                    "start_fuel_record_id": baseline["id"],
                    "end_fuel_record_id": record["id"],
                    "started_at": _serialize_value(baseline["filled_at"]),
                    "ended_at": _serialize_value(record["filled_at"]),
                    "start_odometer_miles": _serialize_decimal(_decimal(baseline["odometer_miles"])),
                    "end_odometer_miles": _serialize_decimal(_decimal(record["odometer_miles"])),
                    "miles": _serialize_decimal(miles.quantize(ODOMETER_QUANT, rounding=ROUND_HALF_UP)),
                    "gallons": _serialize_decimal(gallons_since_baseline.quantize(GALLONS_QUANT, rounding=ROUND_HALF_UP)),
                    "fuel_cost": _serialize_decimal(cost_since_baseline.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)),
                    "mpg": _serialize_decimal((miles / gallons_since_baseline).quantize(MPG_QUANT, rounding=ROUND_HALF_UP)),
                    "driving_context": interval_context,
                }
            )

        baseline = record
        gallons_since_baseline = Decimal("0")
        cost_since_baseline = Decimal("0")
        contexts_since_baseline = []

    return intervals


def summarize_fuel(records: list[dict], *, rolling_count: int = 3) -> dict:
    intervals = build_fuel_intervals(records)
    total_spend = sum((_decimal(record["total_cost"]) for record in records), Decimal("0"))
    total_gallons = sum((_decimal(record["gallons"]) for record in records), Decimal("0"))
    calculable_miles = sum((_decimal(interval["miles"]) for interval in intervals), Decimal("0"))
    calculable_gallons = sum((_decimal(interval["gallons"]) for interval in intervals), Decimal("0"))
    calculable_cost = sum((_decimal(interval["fuel_cost"]) for interval in intervals), Decimal("0"))
    rolling = intervals[-rolling_count:]
    rolling_miles = sum((_decimal(interval["miles"]) for interval in rolling), Decimal("0"))
    rolling_gallons = sum((_decimal(interval["gallons"]) for interval in rolling), Decimal("0"))

    def mpg_for(context: str) -> str | None:
        context_intervals = [interval for interval in intervals if interval["driving_context"] == context]
        miles = sum((_decimal(interval["miles"]) for interval in context_intervals), Decimal("0"))
        gallons = sum((_decimal(interval["gallons"]) for interval in context_intervals), Decimal("0"))
        if gallons <= 0:
            return None
        return _serialize_decimal((miles / gallons).quantize(MPG_QUANT, rounding=ROUND_HALF_UP))

    fill_count = len(records)
    average_miles_between_fills = None
    ordered = sorted(records, key=lambda item: (item["filled_at"], item.get("created_at") or "", item["id"]))
    if len(ordered) > 1:
        miles_between = _decimal(ordered[-1]["odometer_miles"]) - _decimal(ordered[0]["odometer_miles"])
        if miles_between > 0:
            average_miles_between_fills = _serialize_decimal(
                (miles_between / Decimal(len(ordered) - 1)).quantize(MPG_QUANT, rounding=ROUND_HALF_UP)
            )

    return {
        "intervals": intervals,
        "latest_calculable_mpg": intervals[-1]["mpg"] if intervals else None,
        "rolling_mpg": (
            _serialize_decimal((rolling_miles / rolling_gallons).quantize(MPG_QUANT, rounding=ROUND_HALF_UP))
            if rolling_gallons > 0
            else None
        ),
        "rolling_interval_count": len(rolling),
        "lifetime_calculable_mpg": (
            _serialize_decimal((calculable_miles / calculable_gallons).quantize(MPG_QUANT, rounding=ROUND_HALF_UP))
            if calculable_gallons > 0
            else None
        ),
        "normal_mpg": mpg_for("NORMAL"),
        "towing_mpg": mpg_for("TOWING"),
        "mixed_mpg": mpg_for("MIXED"),
        "average_fuel_price": (
            _serialize_decimal((total_spend / total_gallons).quantize(PRICE_QUANT, rounding=ROUND_HALF_UP))
            if total_gallons > 0
            else None
        ),
        "average_miles_between_fills": average_miles_between_fills,
        "fuel_cost_per_mile": (
            _serialize_decimal((calculable_cost / calculable_miles).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP))
            if calculable_miles > 0
            else None
        ),
        "total_fuel_spend": _serialize_decimal(total_spend.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)),
        "gallons_consumed": _serialize_decimal(total_gallons.quantize(GALLONS_QUANT, rounding=ROUND_HALF_UP)),
        "fill_count": fill_count,
    }


def get_fuel_summary(*, user_id: str, vehicle_id: str) -> dict:
    records = list_fuel_records(user_id=user_id, vehicle_id=vehicle_id)
    return summarize_fuel(records)


def create_maintenance_schedule(*, user_id: str, vehicle_id: str, **fields) -> dict:
    if fields.get("mileage_interval_miles") is None and fields.get("time_interval_days") is None:
        raise VehicleValidationError("mileage_interval_miles or time_interval_days is required")
    conn = get_db_conn()
    try:
        try:
            with conn.cursor() as cur:
                _assert_vehicle_owner(cur, user_id=user_id, vehicle_id=vehicle_id)
                cur.execute(
                    f"""
                    INSERT INTO public.vehicle_maintenance_schedules AS schedule (
                        vehicle_id,
                        name,
                        category,
                        description,
                        mileage_interval_miles,
                        time_interval_days,
                        last_service_odometer_miles,
                        last_service_date,
                        due_soon_miles,
                        due_soon_days,
                        overdue_miles,
                        overdue_days
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {_schedule_columns()}
                    """,
                    (
                        vehicle_id,
                        fields["name"],
                        fields["category"],
                        fields.get("description"),
                        _nullable_decimal(fields.get("mileage_interval_miles")),
                        fields.get("time_interval_days"),
                        _nullable_decimal(fields.get("last_service_odometer_miles")),
                        fields.get("last_service_date"),
                        _decimal(fields.get("due_soon_miles", Decimal("500"))),
                        fields.get("due_soon_days", 14),
                        _decimal(fields.get("overdue_miles", Decimal("500"))),
                        fields.get("overdue_days", 14),
                    ),
                )
                schedule = _row_to_dict(cur, cur.fetchone())
            conn.commit()
            return schedule
        except Exception:
            conn.rollback()
            raise
    finally:
        put_db_conn(conn)


def list_maintenance_schedules(
    *,
    user_id: str,
    vehicle_id: str,
    include_archived: bool = False,
) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _assert_vehicle_owner(cur, user_id=user_id, vehicle_id=vehicle_id)
            if include_archived:
                cur.execute(
                    f"""
                    SELECT {_schedule_columns()}
                    FROM public.vehicle_maintenance_schedules AS schedule
                    WHERE schedule.vehicle_id = %s
                    ORDER BY schedule.active DESC, schedule.category, schedule.name
                    """,
                    (vehicle_id,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT {_schedule_columns()}
                    FROM public.vehicle_maintenance_schedules AS schedule
                    WHERE schedule.vehicle_id = %s
                      AND schedule.active = true
                    ORDER BY schedule.category, schedule.name
                    """,
                    (vehicle_id,),
                )
            return _rows_to_dicts(cur, cur.fetchall())
    finally:
        put_db_conn(conn)


def _get_schedule(cur, *, user_id: str, vehicle_id: str, schedule_id: str) -> dict:
    cur.execute(
        f"""
        SELECT {_schedule_columns()}
        FROM public.vehicle_maintenance_schedules AS schedule
        INNER JOIN public.vehicles AS vehicle
          ON vehicle.id = schedule.vehicle_id
        WHERE schedule.id = %s
          AND schedule.vehicle_id = %s
          AND vehicle.user_id = %s
        """,
        (schedule_id, vehicle_id, user_id),
    )
    schedule = _row_to_dict(cur, cur.fetchone())
    if not schedule:
        raise VehicleNotFoundError(f"Maintenance schedule not found: {schedule_id}")
    return schedule


def get_maintenance_schedule(*, user_id: str, vehicle_id: str, schedule_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            return _get_schedule(cur, user_id=user_id, vehicle_id=vehicle_id, schedule_id=schedule_id)
    finally:
        put_db_conn(conn)


def update_maintenance_schedule(user_id: str, vehicle_id: str, schedule_id: str, **fields) -> dict:
    if not fields:
        raise VehicleValidationError("No maintenance schedule fields supplied for update")
    allowed = {
        "name",
        "category",
        "description",
        "mileage_interval_miles",
        "time_interval_days",
        "last_service_odometer_miles",
        "last_service_date",
        "due_soon_miles",
        "due_soon_days",
        "overdue_miles",
        "overdue_days",
    }
    updates = []
    values = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in {
            "mileage_interval_miles",
            "last_service_odometer_miles",
            "due_soon_miles",
            "overdue_miles",
        }:
            value = _nullable_decimal(value)
        updates.append(f"{key} = %s")
        values.append(value)
    if not updates:
        raise VehicleValidationError("No maintenance schedule fields supplied for update")
    values.extend([schedule_id, vehicle_id, user_id])
    conn = get_db_conn()
    try:
        try:
            with conn.cursor() as cur:
                current = _get_schedule(
                    cur,
                    user_id=user_id,
                    vehicle_id=vehicle_id,
                    schedule_id=schedule_id,
                )
                merged = {**current, **fields}
                if merged.get("mileage_interval_miles") is None and merged.get("time_interval_days") is None:
                    raise VehicleValidationError("mileage_interval_miles or time_interval_days is required")
                cur.execute(
                    f"""
                    UPDATE public.vehicle_maintenance_schedules AS schedule
                    SET {", ".join(updates)},
                        updated_at = CURRENT_TIMESTAMP
                    FROM public.vehicles AS vehicle
                    WHERE schedule.id = %s
                      AND schedule.vehicle_id = %s
                      AND vehicle.id = schedule.vehicle_id
                      AND vehicle.user_id = %s
                    RETURNING {_schedule_columns("schedule")}
                    """,
                    values,
                )
                schedule = _row_to_dict(cur, cur.fetchone())
                if not schedule:
                    raise VehicleNotFoundError(f"Maintenance schedule not found: {schedule_id}")
            conn.commit()
            return schedule
        except Exception:
            conn.rollback()
            raise
    finally:
        put_db_conn(conn)


def set_maintenance_schedule_active(
    *,
    user_id: str,
    vehicle_id: str,
    schedule_id: str,
    active: bool,
) -> dict:
    conn = get_db_conn()
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE public.vehicle_maintenance_schedules AS schedule
                    SET active = %s,
                        updated_at = CURRENT_TIMESTAMP
                    FROM public.vehicles AS vehicle
                    WHERE schedule.id = %s
                      AND schedule.vehicle_id = %s
                      AND vehicle.id = schedule.vehicle_id
                      AND vehicle.user_id = %s
                    RETURNING {_schedule_columns("schedule")}
                    """,
                    (active, schedule_id, vehicle_id, user_id),
                )
                schedule = _row_to_dict(cur, cur.fetchone())
                if not schedule:
                    raise VehicleNotFoundError(f"Maintenance schedule not found: {schedule_id}")
            conn.commit()
            return schedule
        except Exception:
            conn.rollback()
            raise
    finally:
        put_db_conn(conn)


def create_maintenance_event(*, user_id: str, vehicle_id: str, **fields) -> dict:
    action = _validate_action(fields["action"])
    odometer = _validate_odometer(fields["odometer_miles"])
    conn = get_db_conn()
    try:
        try:
            with conn.cursor() as cur:
                _assert_vehicle_owner(cur, user_id=user_id, vehicle_id=vehicle_id)
                schedule_id = fields.get("maintenance_schedule_id")
                if schedule_id is not None:
                    _get_schedule(
                        cur,
                        user_id=user_id,
                        vehicle_id=vehicle_id,
                        schedule_id=str(schedule_id),
                    )
                cur.execute(
                    f"""
                    INSERT INTO public.vehicle_maintenance_events AS event (
                        vehicle_id,
                        maintenance_schedule_id,
                        performed_at,
                        odometer_miles,
                        category,
                        action,
                        description,
                        cost,
                        service_provider,
                        notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {_event_columns()}
                    """,
                    (
                        vehicle_id,
                        schedule_id,
                        fields["performed_at"],
                        odometer,
                        fields["category"],
                        action,
                        fields["description"],
                        _nullable_decimal(fields.get("cost")),
                        fields.get("service_provider"),
                        fields.get("notes"),
                    ),
                )
                event = _row_to_dict(cur, cur.fetchone())
                _upsert_linked_odometer(
                    cur,
                    vehicle_id=vehicle_id,
                    odometer_miles=odometer,
                    observed_at=fields["performed_at"],
                    source="MAINTENANCE",
                    source_record_type="maintenance_event",
                    source_record_id=event["id"],
                )
                if schedule_id is not None:
                    cur.execute(
                        """
                        UPDATE public.vehicle_maintenance_schedules
                        SET last_service_odometer_miles = %s,
                            last_service_date = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                          AND vehicle_id = %s
                        """,
                        (
                            odometer,
                            _date_from_datetime(fields["performed_at"]),
                            schedule_id,
                            vehicle_id,
                        ),
                    )
            conn.commit()
            return event
        except Exception:
            conn.rollback()
            raise
    finally:
        put_db_conn(conn)


def list_maintenance_events(*, user_id: str, vehicle_id: str) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _assert_vehicle_owner(cur, user_id=user_id, vehicle_id=vehicle_id)
            cur.execute(
                f"""
                SELECT {_event_columns()}
                FROM public.vehicle_maintenance_events AS event
                WHERE event.vehicle_id = %s
                ORDER BY event.performed_at DESC, event.created_at DESC, event.id DESC
                """,
                (vehicle_id,),
            )
            return _rows_to_dicts(cur, cur.fetchall())
    finally:
        put_db_conn(conn)


def _get_event(cur, *, user_id: str, vehicle_id: str, event_id: str) -> dict:
    cur.execute(
        f"""
        SELECT {_event_columns()}
        FROM public.vehicle_maintenance_events AS event
        INNER JOIN public.vehicles AS vehicle
          ON vehicle.id = event.vehicle_id
        WHERE event.id = %s
          AND event.vehicle_id = %s
          AND vehicle.user_id = %s
        """,
        (event_id, vehicle_id, user_id),
    )
    event = _row_to_dict(cur, cur.fetchone())
    if not event:
        raise VehicleNotFoundError(f"Maintenance event not found: {event_id}")
    return event


def get_maintenance_event(*, user_id: str, vehicle_id: str, event_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            return _get_event(cur, user_id=user_id, vehicle_id=vehicle_id, event_id=event_id)
    finally:
        put_db_conn(conn)


def update_maintenance_event(user_id: str, vehicle_id: str, event_id: str, **fields) -> dict:
    if not fields:
        raise VehicleValidationError("No maintenance event fields supplied for update")
    conn = get_db_conn()
    try:
        try:
            with conn.cursor() as cur:
                current = _get_event(cur, user_id=user_id, vehicle_id=vehicle_id, event_id=event_id)
                merged = {**current, **fields}
                schedule_id = merged.get("maintenance_schedule_id")
                if schedule_id is not None:
                    _get_schedule(
                        cur,
                        user_id=user_id,
                        vehicle_id=vehicle_id,
                        schedule_id=str(schedule_id),
                    )
                action = _validate_action(merged["action"])
                odometer = _validate_odometer(merged["odometer_miles"])
                cur.execute(
                    f"""
                    UPDATE public.vehicle_maintenance_events AS event
                    SET maintenance_schedule_id = %s,
                        performed_at = %s,
                        odometer_miles = %s,
                        category = %s,
                        action = %s,
                        description = %s,
                        cost = %s,
                        service_provider = %s,
                        notes = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE event.id = %s
                      AND event.vehicle_id = %s
                    RETURNING {_event_columns()}
                    """,
                    (
                        schedule_id,
                        merged["performed_at"],
                        odometer,
                        merged["category"],
                        action,
                        merged["description"],
                        _nullable_decimal(merged.get("cost")),
                        merged.get("service_provider"),
                        merged.get("notes"),
                        event_id,
                        vehicle_id,
                    ),
                )
                event = _row_to_dict(cur, cur.fetchone())
                _upsert_linked_odometer(
                    cur,
                    vehicle_id=vehicle_id,
                    odometer_miles=odometer,
                    observed_at=merged["performed_at"],
                    source="MAINTENANCE",
                    source_record_type="maintenance_event",
                    source_record_id=event_id,
                )
                if schedule_id is not None:
                    cur.execute(
                        """
                        UPDATE public.vehicle_maintenance_schedules
                        SET last_service_odometer_miles = %s,
                            last_service_date = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                          AND vehicle_id = %s
                        """,
                        (
                            odometer,
                            _date_from_datetime(merged["performed_at"]),
                            schedule_id,
                            vehicle_id,
                        ),
                    )
            conn.commit()
            return event
        except Exception:
            conn.rollback()
            raise
    finally:
        put_db_conn(conn)


def _status_for_remaining(remaining, due_soon, overdue) -> str:
    if remaining is None:
        return "OK"
    remaining = _decimal(remaining)
    due_soon = _decimal(due_soon)
    overdue = _decimal(overdue)
    if remaining <= -overdue:
        return "OVERDUE"
    if remaining <= 0:
        return "DUE"
    if remaining <= due_soon:
        return "DUE_SOON"
    return "OK"


def _worst_status(statuses: list[str]) -> str:
    return max(statuses or ["OK"], key=lambda status: STATUS_RANK[status])


def maintenance_status_for_schedule(
    schedule: dict,
    *,
    current_odometer_miles=None,
    as_of: date | None = None,
) -> dict:
    as_of = as_of or datetime.now(timezone.utc).date()
    statuses: list[str] = []
    next_due_mileage = None
    miles_remaining = None
    next_due_date = None
    days_remaining = None

    if schedule.get("mileage_interval_miles") is not None and schedule.get("last_service_odometer_miles") is not None:
        next_due_mileage = _decimal(schedule["last_service_odometer_miles"]) + _decimal(schedule["mileage_interval_miles"])
        if current_odometer_miles is not None:
            miles_remaining = next_due_mileage - _decimal(current_odometer_miles)
            statuses.append(
                _status_for_remaining(
                    miles_remaining,
                    schedule.get("due_soon_miles", Decimal("500")),
                    schedule.get("overdue_miles", Decimal("500")),
                )
            )

    if schedule.get("time_interval_days") is not None and schedule.get("last_service_date") is not None:
        last_service_date = schedule["last_service_date"]
        if isinstance(last_service_date, str):
            last_service_date = date.fromisoformat(last_service_date)
        next_due_date = last_service_date + timedelta(days=int(schedule["time_interval_days"]))
        days_remaining = (next_due_date - as_of).days
        statuses.append(
            _status_for_remaining(
                Decimal(days_remaining),
                Decimal(int(schedule.get("due_soon_days", 14))),
                Decimal(int(schedule.get("overdue_days", 14))),
            )
        )

    status = _worst_status(statuses)
    return {
        "schedule_id": schedule["id"],
        "name": schedule["name"],
        "category": schedule["category"],
        "status": status,
        "last_serviced_date": _serialize_value(schedule.get("last_service_date")),
        "last_serviced_mileage": _serialize_value(schedule.get("last_service_odometer_miles")),
        "next_due_date": _serialize_value(next_due_date),
        "next_due_mileage": _serialize_value(next_due_mileage),
        "miles_remaining": _serialize_value(miles_remaining),
        "days_remaining": days_remaining,
        "due_soon_miles": _serialize_value(schedule.get("due_soon_miles")),
        "due_soon_days": schedule.get("due_soon_days"),
        "overdue_miles": _serialize_value(schedule.get("overdue_miles")),
        "overdue_days": schedule.get("overdue_days"),
    }


def get_maintenance_status(*, user_id: str, vehicle_id: str) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _assert_vehicle_owner(cur, user_id=user_id, vehicle_id=vehicle_id)
            current = _current_odometer(cur, vehicle_id=vehicle_id)
            current_mileage = current["odometer_miles"] if current else None
            cur.execute(
                f"""
                SELECT {_schedule_columns()}
                FROM public.vehicle_maintenance_schedules AS schedule
                WHERE schedule.vehicle_id = %s
                  AND schedule.active = true
                ORDER BY schedule.category, schedule.name
                """,
                (vehicle_id,),
            )
            schedules = _rows_to_dicts(cur, cur.fetchall())
            return [
                maintenance_status_for_schedule(
                    schedule,
                    current_odometer_miles=current_mileage,
                )
                for schedule in schedules
            ]
    finally:
        put_db_conn(conn)


def get_vehicle_summary(*, user_id: str, vehicle_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            vehicle = _get_vehicle_row(cur, user_id=user_id, vehicle_id=vehicle_id)
            current = _current_odometer(cur, vehicle_id=vehicle_id)
            cur.execute(
                f"""
                SELECT {_fuel_columns()}
                FROM public.vehicle_fuel_records AS fuel
                WHERE fuel.vehicle_id = %s
                ORDER BY fuel.filled_at DESC, fuel.created_at DESC, fuel.id DESC
                """,
                (vehicle_id,),
            )
            fuel_records = _rows_to_dicts(cur, cur.fetchall())
            cur.execute(
                f"""
                SELECT {_schedule_columns()}
                FROM public.vehicle_maintenance_schedules AS schedule
                WHERE schedule.vehicle_id = %s
                  AND schedule.active = true
                ORDER BY schedule.category, schedule.name
                """,
                (vehicle_id,),
            )
            schedules = _rows_to_dicts(cur, cur.fetchall())
            cur.execute(
                f"""
                SELECT {_event_columns()}
                FROM public.vehicle_maintenance_events AS event
                WHERE event.vehicle_id = %s
                ORDER BY event.performed_at DESC, event.created_at DESC, event.id DESC
                LIMIT 5
                """,
                (vehicle_id,),
            )
            recent_maintenance = _rows_to_dicts(cur, cur.fetchall())
            cur.execute(
                f"""
                SELECT {_odometer_columns()}
                FROM public.vehicle_odometer_observations AS observation
                WHERE observation.vehicle_id = %s
                ORDER BY observation.observed_at DESC,
                         observation.created_at DESC,
                         observation.id DESC
                LIMIT 10
                """,
                (vehicle_id,),
            )
            recent_odometer = _rows_to_dicts(cur, cur.fetchall())

        fuel_summary = summarize_fuel(fuel_records)
        current_mileage = current["odometer_miles"] if current else None
        maintenance_statuses = [
            maintenance_status_for_schedule(schedule, current_odometer_miles=current_mileage)
            for schedule in schedules
        ]
        maintenance_statuses.sort(key=lambda item: STATUS_RANK[item["status"]], reverse=True)
        return {
            "vehicle": vehicle,
            "current_odometer": current,
            "latest_fill_up": fuel_records[0] if fuel_records else None,
            "fuel": fuel_summary,
            "upcoming_maintenance": maintenance_statuses[:10],
            "recent_activity": {
                "maintenance_events": recent_maintenance,
                "odometer_observations": recent_odometer,
            },
        }
    finally:
        put_db_conn(conn)

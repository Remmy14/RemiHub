from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from psycopg2 import pool


class OfflineThreadedConnectionPool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise RuntimeError("database access is disabled during Vehicle tests")

    def putconn(self, _connection):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool

from backend.services import vehicle_service


USER_ID = "11111111-1111-4111-8111-111111111111"
VEHICLE_ID = "22222222-2222-4222-8222-222222222222"
ODOMETER_ID = "33333333-3333-4333-8333-333333333333"
FUEL_ID = "44444444-4444-4444-8444-444444444444"
SCHEDULE_ID = "55555555-5555-4555-8555-555555555555"
EVENT_ID = "66666666-6666-4666-8666-666666666666"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

VEHICLE_COLUMNS = [
    "id",
    "user_id",
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
    "active",
    "created_at",
    "updated_at",
]
VEHICLE_ROW = (
    VEHICLE_ID,
    USER_ID,
    2026,
    "Generic",
    "Vehicle",
    None,
    None,
    "gasoline",
    None,
    None,
    None,
    None,
    None,
    None,
    True,
    NOW,
    NOW,
)

ODOMETER_COLUMNS = [
    "id",
    "vehicle_id",
    "odometer_miles",
    "observed_at",
    "source",
    "source_record_type",
    "source_record_id",
    "created_at",
]
ODOMETER_ROW = (
    ODOMETER_ID,
    VEHICLE_ID,
    Decimal("10000.0"),
    NOW,
    "MANUAL",
    None,
    None,
    NOW,
)

FUEL_COLUMNS = [
    "id",
    "vehicle_id",
    "filled_at",
    "odometer_miles",
    "gallons",
    "total_cost",
    "price_per_gallon",
    "is_full_fill",
    "driving_context",
    "fuel_grade",
    "station_name",
    "notes",
    "created_at",
    "updated_at",
]
FUEL_ROW = (
    FUEL_ID,
    VEHICLE_ID,
    NOW,
    Decimal("10000.0"),
    Decimal("20.000"),
    Decimal("80.00"),
    Decimal("4.000"),
    True,
    "NORMAL",
    None,
    None,
    None,
    NOW,
    NOW,
)

SCHEDULE_COLUMNS = [
    "id",
    "vehicle_id",
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
    "active",
    "created_at",
    "updated_at",
]
SCHEDULE_ROW = (
    SCHEDULE_ID,
    VEHICLE_ID,
    "Oil",
    "engine oil/filter",
    None,
    Decimal("5000.0"),
    None,
    None,
    None,
    Decimal("500.0"),
    14,
    Decimal("500.0"),
    14,
    True,
    NOW,
    NOW,
)

EVENT_COLUMNS = [
    "id",
    "vehicle_id",
    "maintenance_schedule_id",
    "performed_at",
    "odometer_miles",
    "category",
    "action",
    "description",
    "cost",
    "service_provider",
    "notes",
    "created_at",
    "updated_at",
]
EVENT_ROW = (
    EVENT_ID,
    VEHICLE_ID,
    None,
    NOW,
    Decimal("10000.0"),
    "engine oil/filter",
    "REPLACED",
    "Changed oil and filter",
    None,
    None,
    None,
    NOW,
    NOW,
)


class FakeCursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.executed = []
        self.description = []
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        columns, rows = self.responses.pop(0)
        self.description = [(column,) for column in columns]
        self.rows = list(rows)

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows = self.rows
        self.rows = []
        return rows


class FakeConnection:
    def __init__(self, responses):
        self.cursor_instance = FakeCursor(responses)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def patch_vehicle_connection(responses):
    connection = FakeConnection(responses)
    return connection, patch.multiple(
        vehicle_service,
        get_db_conn=lambda: connection,
        put_db_conn=lambda _connection: None,
    )


def normalized_sql(sql: str) -> str:
    return " ".join(sql.split())


def insert_sqls(connection: FakeConnection, table: str) -> list[str]:
    return [
        sql
        for sql, _params in connection.cursor_instance.executed
        if f"INSERT INTO {table}" in sql
    ]


def fuel_record(
    record_id: str,
    *,
    filled_at: datetime,
    odometer_miles: str,
    gallons: str,
    total_cost: str,
    price_per_gallon: str = "4.000",
    is_full_fill: bool = True,
    driving_context: str = "NORMAL",
) -> dict:
    return {
        "id": record_id,
        "vehicle_id": "vehicle-1",
        "filled_at": filled_at,
        "odometer_miles": odometer_miles,
        "gallons": gallons,
        "total_cost": total_cost,
        "price_per_gallon": price_per_gallon,
        "is_full_fill": is_full_fill,
        "driving_context": driving_context,
        "fuel_grade": None,
        "station_name": None,
        "notes": None,
        "created_at": filled_at,
        "updated_at": filled_at,
    }


class VehicleInsertAliasSqlTests(unittest.TestCase):
    def assert_insert_alias(self, sql: str, *, table: str, alias: str):
        normalized = normalized_sql(sql)
        self.assertIn(f"INSERT INTO {table} AS {alias} (", normalized)
        self.assertIn(f"RETURNING {alias}.id", normalized)

    def test_create_vehicle_declares_vehicle_returning_alias(self):
        connection, patches = patch_vehicle_connection(
            [(VEHICLE_COLUMNS, [VEHICLE_ROW])]
        )

        with patches:
            vehicle_service.create_vehicle(
                user_id=USER_ID,
                year=2026,
                make="Generic",
                model="Vehicle",
                fuel_type="gasoline",
            )

        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        self.assert_insert_alias(
            insert_sqls(connection, "public.vehicles")[0],
            table="public.vehicles",
            alias="vehicle",
        )

    def test_create_vehicle_purchase_odometer_declares_observation_returning_alias(self):
        connection, patches = patch_vehicle_connection(
            [
                (VEHICLE_COLUMNS, [VEHICLE_ROW]),
                (ODOMETER_COLUMNS, [ODOMETER_ROW]),
            ]
        )

        with patches:
            vehicle_service.create_vehicle(
                user_id=USER_ID,
                year=2026,
                make="Generic",
                model="Vehicle",
                fuel_type="gasoline",
                purchase_odometer_miles=Decimal("10000"),
            )

        self.assert_insert_alias(
            insert_sqls(connection, "public.vehicle_odometer_observations")[0],
            table="public.vehicle_odometer_observations",
            alias="observation",
        )

    def test_manual_odometer_insert_declares_observation_returning_alias(self):
        connection, patches = patch_vehicle_connection(
            [
                (VEHICLE_COLUMNS, [VEHICLE_ROW]),
                (ODOMETER_COLUMNS, [ODOMETER_ROW]),
            ]
        )

        with patches:
            vehicle_service.create_manual_odometer_observation(
                user_id=USER_ID,
                vehicle_id=VEHICLE_ID,
                odometer_miles=Decimal("10000"),
                observed_at=NOW,
            )

        self.assert_insert_alias(
            insert_sqls(connection, "public.vehicle_odometer_observations")[0],
            table="public.vehicle_odometer_observations",
            alias="observation",
        )

    def test_fuel_and_linked_odometer_inserts_declare_returning_aliases(self):
        connection, patches = patch_vehicle_connection(
            [
                (VEHICLE_COLUMNS, [VEHICLE_ROW]),
                (FUEL_COLUMNS, [FUEL_ROW]),
                (ODOMETER_COLUMNS, [ODOMETER_ROW]),
            ]
        )

        with patches:
            vehicle_service.create_fuel_record(
                user_id=USER_ID,
                vehicle_id=VEHICLE_ID,
                filled_at=NOW,
                odometer_miles=Decimal("10000"),
                gallons=Decimal("20"),
                total_cost=Decimal("80"),
            )

        self.assert_insert_alias(
            insert_sqls(connection, "public.vehicle_fuel_records")[0],
            table="public.vehicle_fuel_records",
            alias="fuel",
        )
        self.assert_insert_alias(
            insert_sqls(connection, "public.vehicle_odometer_observations")[0],
            table="public.vehicle_odometer_observations",
            alias="observation",
        )

    def test_maintenance_schedule_insert_declares_schedule_returning_alias(self):
        connection, patches = patch_vehicle_connection(
            [
                (VEHICLE_COLUMNS, [VEHICLE_ROW]),
                (SCHEDULE_COLUMNS, [SCHEDULE_ROW]),
            ]
        )

        with patches:
            vehicle_service.create_maintenance_schedule(
                user_id=USER_ID,
                vehicle_id=VEHICLE_ID,
                name="Oil",
                category="engine oil/filter",
                mileage_interval_miles=Decimal("5000"),
            )

        self.assert_insert_alias(
            insert_sqls(connection, "public.vehicle_maintenance_schedules")[0],
            table="public.vehicle_maintenance_schedules",
            alias="schedule",
        )

    def test_maintenance_event_and_linked_odometer_inserts_declare_returning_aliases(self):
        connection, patches = patch_vehicle_connection(
            [
                (VEHICLE_COLUMNS, [VEHICLE_ROW]),
                (EVENT_COLUMNS, [EVENT_ROW]),
                (ODOMETER_COLUMNS, [ODOMETER_ROW]),
            ]
        )

        with patches:
            vehicle_service.create_maintenance_event(
                user_id=USER_ID,
                vehicle_id=VEHICLE_ID,
                performed_at=NOW,
                odometer_miles=Decimal("10000"),
                category="engine oil/filter",
                action="REPLACED",
                description="Changed oil and filter",
            )

        self.assert_insert_alias(
            insert_sqls(connection, "public.vehicle_maintenance_events")[0],
            table="public.vehicle_maintenance_events",
            alias="event",
        )
        self.assert_insert_alias(
            insert_sqls(connection, "public.vehicle_odometer_observations")[0],
            table="public.vehicle_odometer_observations",
            alias="observation",
        )


class VehicleFuelCalculationTests(unittest.TestCase):
    def test_fuel_amounts_calculate_missing_total(self):
        result = vehicle_service.normalize_fuel_amounts(
            gallons=Decimal("10"),
            price_per_gallon=Decimal("3.499"),
        )

        self.assertEqual(result["gallons"], Decimal("10.000"))
        self.assertEqual(result["price_per_gallon"], Decimal("3.499"))
        self.assertEqual(result["total_cost"], Decimal("34.99"))

    def test_fuel_amounts_calculate_missing_price(self):
        result = vehicle_service.normalize_fuel_amounts(
            gallons=Decimal("12.5"),
            total_cost=Decimal("50.00"),
        )

        self.assertEqual(result["price_per_gallon"], Decimal("4.000"))

    def test_fuel_amounts_reject_insufficient_values(self):
        with self.assertRaises(vehicle_service.VehicleValidationError):
            vehicle_service.normalize_fuel_amounts(gallons=Decimal("10"))

    def test_fuel_amounts_reject_inconsistent_values(self):
        with self.assertRaises(vehicle_service.VehicleValidationError):
            vehicle_service.normalize_fuel_amounts(
                gallons=Decimal("10"),
                total_cost=Decimal("50"),
                price_per_gallon=Decimal("4"),
            )

    def test_first_full_fill_does_not_generate_mpg(self):
        records = [
            fuel_record(
                "full-1",
                filled_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                odometer_miles="10000",
                gallons="20",
                total_cost="80",
            )
        ]

        self.assertEqual(vehicle_service.build_fuel_intervals(records), [])
        self.assertIsNone(vehicle_service.summarize_fuel(records)["lifetime_calculable_mpg"])

    def test_partial_fill_sequence_uses_all_fuel_since_previous_full_fill(self):
        records = [
            fuel_record(
                "full-1",
                filled_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                odometer_miles="10000",
                gallons="20",
                total_cost="80",
                is_full_fill=True,
            ),
            fuel_record(
                "partial",
                filled_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
                odometer_miles="10150",
                gallons="8",
                total_cost="32",
                is_full_fill=False,
            ),
            fuel_record(
                "full-2",
                filled_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
                odometer_miles="10400",
                gallons="20",
                total_cost="80",
                is_full_fill=True,
            ),
        ]

        intervals = vehicle_service.build_fuel_intervals(records)

        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0]["miles"], "400")
        self.assertEqual(intervals[0]["gallons"], "28")
        self.assertEqual(intervals[0]["mpg"], "14.29")

    def test_context_mpgs_are_partitioned_by_completed_interval_context(self):
        records = [
            fuel_record(
                "full-1",
                filled_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                odometer_miles="0",
                gallons="10",
                total_cost="40",
                driving_context="NORMAL",
            ),
            fuel_record(
                "full-2",
                filled_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                odometer_miles="200",
                gallons="10",
                total_cost="40",
                driving_context="NORMAL",
            ),
            fuel_record(
                "partial-1",
                filled_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
                odometer_miles="300",
                gallons="5",
                total_cost="20",
                is_full_fill=False,
                driving_context="TOWING",
            ),
            fuel_record(
                "full-3",
                filled_at=datetime(2026, 1, 4, tzinfo=timezone.utc),
                odometer_miles="400",
                gallons="5",
                total_cost="20",
                driving_context="NORMAL",
            ),
        ]

        summary = vehicle_service.summarize_fuel(records)

        self.assertEqual(summary["normal_mpg"], "20")
        self.assertEqual(summary["mixed_mpg"], "20")
        self.assertIsNone(summary["towing_mpg"])


class VehicleMaintenanceStatusTests(unittest.TestCase):
    def schedule(self, **overrides):
        base = {
            "id": "schedule-1",
            "name": "Oil",
            "category": "engine oil/filter",
            "mileage_interval_miles": Decimal("5000"),
            "time_interval_days": 180,
            "last_service_odometer_miles": Decimal("10000"),
            "last_service_date": date(2026, 1, 1),
            "due_soon_miles": Decimal("500"),
            "due_soon_days": 14,
            "overdue_miles": Decimal("500"),
            "overdue_days": 14,
        }
        base.update(overrides)
        return base

    def test_mileage_status_boundaries(self):
        schedule = self.schedule(time_interval_days=None, last_service_date=None)

        self.assertEqual(
            vehicle_service.maintenance_status_for_schedule(
                schedule,
                current_odometer_miles=Decimal("14499.9"),
            )["status"],
            "OK",
        )
        self.assertEqual(
            vehicle_service.maintenance_status_for_schedule(
                schedule,
                current_odometer_miles=Decimal("14500"),
            )["status"],
            "DUE_SOON",
        )
        self.assertEqual(
            vehicle_service.maintenance_status_for_schedule(
                schedule,
                current_odometer_miles=Decimal("15000"),
            )["status"],
            "DUE",
        )
        self.assertEqual(
            vehicle_service.maintenance_status_for_schedule(
                schedule,
                current_odometer_miles=Decimal("15499.9"),
            )["status"],
            "DUE",
        )
        self.assertEqual(
            vehicle_service.maintenance_status_for_schedule(
                schedule,
                current_odometer_miles=Decimal("15500"),
            )["status"],
            "OVERDUE",
        )

    def test_time_status_boundaries(self):
        schedule = self.schedule(mileage_interval_miles=None, last_service_odometer_miles=None)

        self.assertEqual(
            vehicle_service.maintenance_status_for_schedule(
                schedule,
                as_of=date(2026, 6, 16),
            )["status"],
            "DUE_SOON",
        )
        self.assertEqual(
            vehicle_service.maintenance_status_for_schedule(
                schedule,
                as_of=date(2026, 6, 30),
            )["status"],
            "DUE",
        )
        self.assertEqual(
            vehicle_service.maintenance_status_for_schedule(
                schedule,
                as_of=date(2026, 7, 13),
            )["status"],
            "DUE",
        )
        self.assertEqual(
            vehicle_service.maintenance_status_for_schedule(
                schedule,
                as_of=date(2026, 7, 14),
            )["status"],
            "OVERDUE",
        )

    def test_combined_schedule_uses_worst_dimension(self):
        schedule = self.schedule()

        result = vehicle_service.maintenance_status_for_schedule(
            schedule,
            current_odometer_miles=Decimal("12000"),
            as_of=date(2026, 7, 14),
        )

        self.assertEqual(result["status"], "OVERDUE")

    def test_custom_thresholds_are_used(self):
        schedule = self.schedule(
            time_interval_days=None,
            last_service_date=None,
            due_soon_miles=Decimal("100"),
            overdue_miles=Decimal("50"),
        )

        self.assertEqual(
            vehicle_service.maintenance_status_for_schedule(
                schedule,
                current_odometer_miles=Decimal("14900"),
            )["status"],
            "DUE_SOON",
        )
        self.assertEqual(
            vehicle_service.maintenance_status_for_schedule(
                schedule,
                current_odometer_miles=Decimal("15049.9"),
            )["status"],
            "DUE",
        )
        self.assertEqual(
            vehicle_service.maintenance_status_for_schedule(
                schedule,
                current_odometer_miles=Decimal("15050"),
            )["status"],
            "OVERDUE",
        )


if __name__ == "__main__":
    unittest.main()

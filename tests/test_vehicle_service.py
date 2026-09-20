from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

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

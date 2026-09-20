from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from psycopg2 import pool


class OfflineThreadedConnectionPool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise RuntimeError("database access is disabled during Vehicle HTTP tests")

    def putconn(self, _connection):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool

from backend.core.auth import AuthenticatedPrincipal, require_current_principal
from backend.models.vehicle_models import (
    FuelRecordCreate,
    MaintenanceEventCreate,
    MaintenanceScheduleCreate,
    ManualOdometerObservationCreate,
    VehicleCreate,
)
from backend.routers import vehicles


USER = AuthenticatedPrincipal(
    id="11111111-1111-4111-8111-111111111111",
    firebase_uid="firebase-user-1",
    email="alex@example.com",
    display_name="Alex",
    role="member",
)


class VehicleHttpTests(unittest.TestCase):
    def test_vehicle_router_requires_strict_principal_dependency(self):
        route = next(
            route
            for route in vehicles.router.routes
            if getattr(route, "path", "") == "/vehicles/{vehicle_id}/summary"
            and "GET" in getattr(route, "methods", set())
        )

        dependency_calls = [dependency.call for dependency in route.dependant.dependencies]

        self.assertIn(require_current_principal, dependency_calls)

    def test_create_vehicle_delegates_owner(self):
        create_vehicle = MagicMock(return_value={"id": "vehicle"})
        request = VehicleCreate(
            year=2026,
            make="Generic",
            model="Vehicle",
            fuel_type="gasoline",
        )
        with patch("backend.routers.vehicles.vehicle_service.create_vehicle", create_vehicle):
            response = vehicles.create_vehicle(request, principal=USER)

        self.assertTrue(response["success"])
        self.assertEqual(create_vehicle.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(create_vehicle.call_args.kwargs["make"], "Generic")

    def test_manual_odometer_delegates_owner_and_vehicle(self):
        create_observation = MagicMock(return_value={"id": "observation"})
        request = ManualOdometerObservationCreate(
            odometer_miles=Decimal("12345.6"),
            observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        with patch(
            "backend.routers.vehicles.vehicle_service.create_manual_odometer_observation",
            create_observation,
        ):
            response = vehicles.create_manual_odometer_observation(
                "vehicle",
                request,
                principal=USER,
            )

        self.assertTrue(response["success"])
        self.assertEqual(create_observation.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(create_observation.call_args.kwargs["vehicle_id"], "vehicle")

    def test_create_fuel_record_delegates_owner_and_vehicle(self):
        create_fuel = MagicMock(return_value={"id": "fuel"})
        request = FuelRecordCreate(
            filled_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            odometer_miles=Decimal("10000"),
            gallons=Decimal("20"),
            total_cost=Decimal("80"),
        )
        with patch("backend.routers.vehicles.vehicle_service.create_fuel_record", create_fuel):
            response = vehicles.create_fuel_record("vehicle", request, principal=USER)

        self.assertTrue(response["success"])
        self.assertEqual(create_fuel.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(create_fuel.call_args.kwargs["vehicle_id"], "vehicle")

    def test_create_schedule_model_requires_an_interval(self):
        with self.assertRaises(ValueError):
            MaintenanceScheduleCreate(name="Oil", category="engine oil/filter")

    def test_create_maintenance_event_delegates_owner_and_vehicle(self):
        create_event = MagicMock(return_value={"id": "event"})
        request = MaintenanceEventCreate(
            performed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            odometer_miles=Decimal("10000"),
            category="engine oil/filter",
            action="REPLACED",
            description="Changed oil and filter",
        )
        with patch("backend.routers.vehicles.vehicle_service.create_maintenance_event", create_event):
            response = vehicles.create_maintenance_event("vehicle", request, principal=USER)

        self.assertTrue(response["success"])
        self.assertEqual(create_event.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(create_event.call_args.kwargs["vehicle_id"], "vehicle")

    def test_summary_delegates_owner_and_vehicle(self):
        summary = MagicMock(return_value={"vehicle": {"id": "vehicle"}})
        with patch("backend.routers.vehicles.vehicle_service.get_vehicle_summary", summary):
            response = vehicles.get_vehicle_summary("vehicle", principal=USER)

        self.assertTrue(response["success"])
        self.assertEqual(summary.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(summary.call_args.kwargs["vehicle_id"], "vehicle")


if __name__ == "__main__":
    unittest.main()

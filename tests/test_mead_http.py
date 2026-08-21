from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from psycopg2 import pool
from pydantic import ValidationError


class OfflineThreadedConnectionPool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise RuntimeError("database access is disabled during Mead HTTP tests")

    def putconn(self, _connection):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool

from backend.core.auth import AuthenticatedPrincipal, require_current_principal
from backend.models.mead_models import (
    MeadBatchCreate,
    MeadEventCreate,
    MeadTaskCreate,
)
from backend.routers import mead


USER = AuthenticatedPrincipal(
    id="11111111-1111-4111-8111-111111111111",
    firebase_uid="firebase-user-1",
    email="alex@example.com",
    display_name="Alex",
    role="member",
)


class MeadHttpTests(unittest.TestCase):
    def test_mead_router_requires_strict_principal_dependency(self):
        route = next(
            route
            for route in mead.router.routes
            if getattr(route, "path", "") == "/mead/batches"
            and "GET" in getattr(route, "methods", set())
        )

        dependency_calls = [
            dependency.call
            for dependency in route.dependant.dependencies
        ]

        self.assertIn(require_current_principal, dependency_calls)

    def test_batch_create_forwards_owner_and_decimal_payload(self):
        create_batch = MagicMock()
        create_batch.return_value = {
            "id": "22222222-2222-4222-8222-222222222222",
            "name": "Blackberry Mead",
        }
        request = MeadBatchCreate(
            name="Blackberry Mead",
            start_at=datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc),
            volume=Decimal("1.0"),
            volume_unit="gal",
            original_gravity=Decimal("1.112"),
            target_final_gravity=Decimal("1.010"),
            tosna_enabled=True,
            tosna_total_amount=Decimal("4.8"),
            tosna_unit="g",
            tosna_nutrient_name="Fermaid O",
        )

        with patch("backend.routers.mead.mead_service.create_batch", create_batch):
            response = mead.create_batch(request, principal=USER)

        self.assertTrue(response["success"])
        self.assertEqual(create_batch.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(create_batch.call_args.kwargs["original_gravity"], "1.112")
        self.assertEqual(create_batch.call_args.kwargs["tosna_total_amount"], "4.8")

    def test_tosna_validation_requires_total_and_unit(self):
        with self.assertRaises(ValidationError):
            MeadBatchCreate(
                name="Blackberry Mead",
                start_at=datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc),
                volume=Decimal("1.0"),
                volume_unit="gal",
                original_gravity=Decimal("1.112"),
                tosna_enabled=True,
            )

    def test_gravity_event_requires_gravity_value(self):
        with self.assertRaises(ValidationError):
            MeadEventCreate(
                event_at=datetime(2026, 8, 18, 19, 0, tzinfo=timezone.utc),
                event_type="gravity_reading",
            )

    def test_rejects_implausible_gravity(self):
        with self.assertRaises(ValidationError):
            MeadBatchCreate(
                name="Bad Mead",
                start_at=datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc),
                volume=Decimal("1.0"),
                volume_unit="gal",
                original_gravity=Decimal("2.000"),
            )

    def test_task_create_forwards_due_timestamp(self):
        create_task = MagicMock()
        create_task.return_value = {
            "id": "33333333-3333-4333-8333-333333333333",
            "title": "Check gravity",
        }
        request = MeadTaskCreate(
            task_type="check_gravity",
            title="Check gravity",
            due_at=datetime(2026, 8, 19, 19, 0, tzinfo=timezone.utc),
        )

        with patch("backend.routers.mead.mead_service.create_task", create_task):
            response = mead.create_task(
                "22222222-2222-4222-8222-222222222222",
                request,
                principal=USER,
            )

        self.assertTrue(response["success"])
        self.assertEqual(create_task.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(create_task.call_args.kwargs["task_type"], "check_gravity")

    def test_service_conflict_maps_to_409(self):
        exc = mead.mead_service.MeadConflictError("Only pending tasks can be rescheduled")

        http_exc = mead._handle_service_error(exc)

        self.assertIsInstance(http_exc, HTTPException)
        self.assertEqual(http_exc.status_code, 409)


if __name__ == "__main__":
    unittest.main()

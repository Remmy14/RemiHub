from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from psycopg2 import pool


class OfflineThreadedConnectionPool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise RuntimeError("database access is disabled during Fitness HTTP tests")

    def putconn(self, _connection):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool

from backend.core.auth import AuthenticatedPrincipal, require_current_principal
from backend.routers import fitness


USER = AuthenticatedPrincipal(
    id="11111111-1111-4111-8111-111111111111",
    firebase_uid="firebase-user-1",
    email="alex@example.com",
    display_name="Alex",
    role="member",
)


class FitnessHttpTests(unittest.TestCase):
    def test_fitness_router_requires_strict_principal_dependency(self):
        route = next(
            route
            for route in fitness.router.routes
            if getattr(route, "path", "") == "/fitness/today"
            and "GET" in getattr(route, "methods", set())
        )

        dependency_calls = [
            dependency.call
            for dependency in route.dependant.dependencies
        ]

        self.assertIn(require_current_principal, dependency_calls)

    def test_today_uses_configured_fitness_date_when_no_date_is_supplied(self):
        today_workouts = MagicMock(return_value=[])
        with patch(
            "backend.routers.fitness.fitness_service.current_fitness_date",
            return_value=date(2026, 8, 21),
        ), patch(
            "backend.routers.fitness.fitness_service.today_workouts",
            today_workouts,
        ):
            response = fitness.today_workouts(principal=USER)

        self.assertTrue(response["success"])
        self.assertEqual(today_workouts.call_args.kwargs["target_date"], date(2026, 8, 21))

    def test_plan_instance_detail_route_delegates_owner(self):
        get_plan_instance = MagicMock(return_value={"id": "plan-instance"})
        with patch(
            "backend.routers.fitness.fitness_service.get_plan_instance",
            get_plan_instance,
        ):
            response = fitness.get_plan_instance("plan-instance", principal=USER)

        self.assertTrue(response["success"])
        self.assertEqual(get_plan_instance.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(get_plan_instance.call_args.kwargs["instance_id"], "plan-instance")


if __name__ == "__main__":
    unittest.main()

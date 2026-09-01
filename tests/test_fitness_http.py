from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
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

    def test_recurring_preview_route_delegates_owner(self):
        preview = MagicMock(return_value={"count": 15, "dates": []})
        request = fitness.RecurringSeriesRequest(
            workout_template_id="22222222-2222-4222-8222-222222222222",
            start_date=date(2026, 8, 31),
            weekdays=[1, 3, 5],
            duration_weeks=5,
        )
        with patch(
            "backend.routers.fitness.fitness_service.preview_recurring_series",
            preview,
        ):
            response = fitness.preview_recurring_series(request, principal=USER)

        self.assertTrue(response["success"])
        self.assertEqual(preview.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(preview.call_args.kwargs["weekdays"], [1, 3, 5])

    def test_remove_scheduled_workout_route_delegates_owner(self):
        remove = MagicMock(return_value={"removed_scheduled_workout_id": "scheduled"})
        with patch(
            "backend.routers.fitness.fitness_service.remove_scheduled_workout",
            remove,
        ):
            response = fitness.remove_scheduled_workout("scheduled", principal=USER)

        self.assertTrue(response["success"])
        self.assertEqual(remove.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(remove.call_args.kwargs["scheduled_workout_id"], "scheduled")

    def test_replace_scheduled_workout_template_route_delegates_owner(self):
        replace = MagicMock(return_value={"id": "scheduled", "workout_template_id": "template"})
        request = fitness.ScheduledWorkoutTemplateReplace(
            workout_template_id="22222222-2222-4222-8222-222222222222",
        )
        with patch(
            "backend.routers.fitness.fitness_service.replace_scheduled_workout_template",
            replace,
        ):
            response = fitness.replace_scheduled_workout_template(
                "scheduled",
                request,
                principal=USER,
            )

        self.assertTrue(response["success"])
        self.assertEqual(replace.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(replace.call_args.kwargs["scheduled_workout_id"], "scheduled")
        self.assertEqual(
            replace.call_args.kwargs["workout_template_id"],
            "22222222-2222-4222-8222-222222222222",
        )

    def test_garmin_completion_attempt_route_delegates_owner(self):
        attempt = MagicMock(return_value={"status": "NO_MATCH"})
        with patch(
            "backend.routers.fitness.fitness_service.attempt_garmin_scheduled_workout_completion",
            attempt,
        ):
            response = fitness.attempt_garmin_scheduled_workout_completion(
                "scheduled",
                principal=USER,
            )

        self.assertTrue(response["success"])
        self.assertEqual(attempt.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(attempt.call_args.kwargs["scheduled_workout_id"], "scheduled")

    def test_garmin_selection_route_delegates_owner_and_activity_id(self):
        complete = MagicMock(return_value={"status": "COMPLETED", "workout": {"id": "scheduled"}})
        request = fitness.GarminActivitySelectionRequest(activity_id="garmin-123")
        with patch(
            "backend.routers.fitness.fitness_service.complete_scheduled_workout_with_garmin_activity",
            complete,
        ):
            response = fitness.complete_scheduled_workout_with_garmin_activity(
                "scheduled",
                request,
                principal=USER,
            )

        self.assertTrue(response["success"])
        self.assertEqual(complete.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(complete.call_args.kwargs["scheduled_workout_id"], "scheduled")
        self.assertEqual(complete.call_args.kwargs["activity_id"], "garmin-123")

    def test_historical_efforts_route_requires_strict_principal_dependency(self):
        route = next(
            route
            for route in fitness.router.routes
            if getattr(route, "path", "") == "/fitness/scheduled-workouts/{scheduled_workout_id}/historical-efforts"
            and "GET" in getattr(route, "methods", set())
        )

        dependency_calls = [
            dependency.call
            for dependency in route.dependant.dependencies
        ]

        self.assertIn(require_current_principal, dependency_calls)

    def test_historical_efforts_route_delegates_owner_and_limit(self):
        historical = MagicMock(return_value={"total_efforts": 0, "efforts": []})
        with patch(
            "backend.routers.fitness.fitness_service.get_historical_efforts",
            historical,
        ):
            response = fitness.get_historical_efforts(
                "scheduled",
                limit="all",
                principal=USER,
            )

        self.assertTrue(response["success"])
        self.assertEqual(historical.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(historical.call_args.kwargs["scheduled_workout_id"], "scheduled")
        self.assertEqual(historical.call_args.kwargs["limit"], "all")

    def test_template_completed_workouts_route_requires_strict_principal_dependency(self):
        route = next(
            route
            for route in fitness.router.routes
            if getattr(route, "path", "") == "/fitness/workout-templates/{template_id}/completed-workouts"
            and "GET" in getattr(route, "methods", set())
        )

        dependency_calls = [
            dependency.call
            for dependency in route.dependant.dependencies
        ]

        self.assertIn(require_current_principal, dependency_calls)

    def test_template_completed_workouts_route_delegates_owner_and_template(self):
        history = MagicMock(return_value=[])
        with patch(
            "backend.routers.fitness.fitness_service.list_completed_workouts_for_template",
            history,
        ):
            response = fitness.list_completed_workouts_for_template(
                "template",
                principal=USER,
            )

        self.assertTrue(response["success"])
        self.assertEqual(history.call_args.kwargs["user_id"], USER.id)
        self.assertEqual(history.call_args.kwargs["template_id"], "template")

    def test_historical_efforts_route_maps_validation_errors(self):
        with patch(
            "backend.routers.fitness.fitness_service.get_historical_efforts",
            side_effect=fitness.fitness_service.FitnessValidationError("Historical efforts are not supported for LIFTING workouts"),
        ):
            with self.assertRaises(HTTPException) as caught:
                fitness.get_historical_efforts(
                    "scheduled",
                    limit="5",
                    principal=USER,
                )

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("not supported", caught.exception.detail)

    def test_training_calendar_route_delegates_owner(self):
        calendar = MagicMock(return_value={"weeks": []})
        with patch(
            "backend.routers.fitness.fitness_service.training_calendar",
            calendar,
        ):
            response = fitness.training_calendar(
                start_date=date(2026, 8, 31),
                end_date=date(2026, 10, 4),
                principal=USER,
            )

        self.assertTrue(response["success"])
        self.assertEqual(calendar.call_args.kwargs["user_id"], USER.id)


if __name__ == "__main__":
    unittest.main()

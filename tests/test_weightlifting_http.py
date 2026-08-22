import unittest
from unittest.mock import MagicMock, patch

from psycopg2 import pool

from pydantic import ValidationError


class OfflineThreadedConnectionPool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise RuntimeError("database access is disabled during weightlifting tests")

    def putconn(self, _connection):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool

from backend.core.auth import AuthenticatedPrincipal, require_current_principal
from backend.models.weightlifting_models import (
    WeightliftingEntryUpsert,
    WeightliftingExerciseCreate,
    WeightliftingSettingsUpdate,
)
from backend.routers import weightlifting


USER = AuthenticatedPrincipal(
    id="11111111-1111-4111-8111-111111111111",
    firebase_uid="firebase-user-1",
    email="alex@example.com",
    display_name="Alex",
    role="member",
)


class WeightliftingHttpTests(unittest.TestCase):
    def test_weightlifting_router_requires_strict_principal_dependency(self):
        route = next(
            route
            for route in weightlifting.router.routes
            if getattr(route, "path", "") == "/weightlifting/settings"
            and "GET" in getattr(route, "methods", set())
        )

        dependency_calls = [
            dependency.call
            for dependency in route.dependant.dependencies
        ]

        self.assertIn(require_current_principal, dependency_calls)

    def test_updates_labels_and_weekday_assignments(self):
        update_settings = MagicMock()
        request = WeightliftingSettingsUpdate(
            weight_unit="lb",
            default_weight_increment=5,
            default_target_reps=12,
            default_sets=3,
            days=[
                {"slot": 1, "label": "Push", "weekday": "monday"},
                {"slot": 2, "label": "Pull", "weekday": "wednesday"},
                {"slot": 3, "label": "Legs", "weekday": "friday"},
            ],
        )
        update_settings.return_value = {
            "weight_unit": "lb",
            "default_weight_increment": 5.0,
            "default_target_reps": 12,
            "default_sets": 3,
            "days": [
                {"slot": 1, "label": "Push", "weekday": "monday"},
                {"slot": 2, "label": "Pull", "weekday": "wednesday"},
                {"slot": 3, "label": "Legs", "weekday": "friday"},
            ],
        }

        with patch(
            "backend.routers.weightlifting.weightlifting_service.update_settings",
            update_settings,
        ):
            response = weightlifting.update_settings(request, principal=USER)

        self.assertEqual(response["data"]["days"][2]["label"], "Legs")
        update_settings.assert_called_once()
        self.assertEqual(update_settings.call_args.kwargs["user_id"], USER.id)

    def test_accepts_variable_contiguous_settings_slots(self):
        one_day = WeightliftingSettingsUpdate(
            weight_unit="lb",
            default_weight_increment=5,
            default_target_reps=12,
            default_sets=3,
            days=[
                {"slot": 1, "label": "A", "weekday": "monday"},
            ],
        )
        four_days = WeightliftingSettingsUpdate(
            weight_unit="lb",
            default_weight_increment=5,
            default_target_reps=12,
            default_sets=3,
            days=[
                {"slot": 1, "label": "A", "weekday": "monday"},
                {"slot": 2, "label": "B", "weekday": "tuesday"},
                {"slot": 3, "label": "C", "weekday": "thursday"},
                {"slot": 4, "label": "D", "weekday": "saturday"},
            ],
        )

        self.assertEqual([day.slot for day in one_day.days], [1])
        self.assertEqual([day.slot for day in four_days.days], [1, 2, 3, 4])

    def test_rejects_duplicate_or_noncontiguous_settings_slots(self):
        with self.assertRaises(ValidationError):
            WeightliftingSettingsUpdate(
                weight_unit="lb",
                default_weight_increment=5,
                default_target_reps=12,
                default_sets=3,
                days=[
                    {"slot": 1, "label": "A", "weekday": "monday"},
                    {"slot": 1, "label": "B", "weekday": "wednesday"},
                    {"slot": 3, "label": "C", "weekday": "friday"},
                ],
            )
        with self.assertRaises(ValidationError):
            WeightliftingSettingsUpdate(
                weight_unit="lb",
                default_weight_increment=5,
                default_target_reps=12,
                default_sets=3,
                days=[
                    {"slot": 1, "label": "A", "weekday": "monday"},
                    {"slot": 3, "label": "C", "weekday": "friday"},
                ],
            )

    def test_prevents_blank_exercise_names(self):
        with self.assertRaises(ValidationError):
            WeightliftingExerciseCreate(name="   ")

    def test_rejects_invalid_entry_input(self):
        with self.assertRaises(ValidationError):
            WeightliftingEntryUpsert(
                exercise_id="22222222-2222-4222-8222-222222222222",
                week_start="2026-08-03",
                workout_day_slot=4,
                weight=-1,
                reps=0,
            )

    def test_decimal_weights_are_forwarded_to_service(self):
        upsert_entry = MagicMock()
        request = WeightliftingEntryUpsert(
            exercise_id="22222222-2222-4222-8222-222222222222",
            week_start="2026-08-03",
            workout_day_slot=1,
            weight="47.5",
            reps=12,
            sets=3,
            completed=True,
        )
        upsert_entry.return_value = {
            "id": "33333333-3333-4333-8333-333333333333",
            "exercise_id": "22222222-2222-4222-8222-222222222222",
            "week_start": "2026-08-03",
            "workout_day_slot": 1,
            "weight": 47.5,
            "reps": 12,
            "sets": 3,
            "completed": True,
        }

        with patch(
            "backend.routers.weightlifting.weightlifting_service.upsert_entry",
            upsert_entry,
        ):
            response = weightlifting.upsert_entry(request, principal=USER)

        self.assertTrue(response["success"])
        self.assertEqual(str(upsert_entry.call_args.kwargs["weight"]), "47.5")
        self.assertNotIn("fitness_scheduled_workout_id", upsert_entry.call_args.kwargs)

    def test_fitness_linkage_is_forwarded_only_when_supplied(self):
        upsert_entry = MagicMock(return_value={"id": "entry"})
        request = WeightliftingEntryUpsert(
            exercise_id="22222222-2222-4222-8222-222222222222",
            week_start="2026-08-03",
            workout_day_slot=1,
            weight="47.5",
            reps=12,
            fitness_scheduled_workout_id="33333333-3333-4333-8333-333333333333",
        )

        with patch(
            "backend.routers.weightlifting.weightlifting_service.upsert_entry",
            upsert_entry,
        ):
            weightlifting.upsert_entry(request, principal=USER)

        self.assertEqual(
            upsert_entry.call_args.kwargs["fitness_scheduled_workout_id"],
            "33333333-3333-4333-8333-333333333333",
        )

    def test_archiving_and_restoring_delegate_to_active_state(self):
        set_active = MagicMock()
        set_active.return_value = {
            "id": "22222222-2222-4222-8222-222222222222",
            "name": "Bench Press",
            "active": False,
        }

        with patch(
            "backend.routers.weightlifting.weightlifting_service.set_exercise_active",
            set_active,
        ):
            archive_response = weightlifting.archive_exercise(
                "22222222-2222-4222-8222-222222222222",
                principal=USER,
            )
            restore_response = weightlifting.restore_exercise(
                "22222222-2222-4222-8222-222222222222",
                principal=USER,
            )

        self.assertTrue(archive_response["success"])
        self.assertTrue(restore_response["success"])
        self.assertEqual(set_active.call_args_list[0].kwargs["active"], False)
        self.assertEqual(set_active.call_args_list[1].kwargs["active"], True)


if __name__ == "__main__":
    unittest.main()

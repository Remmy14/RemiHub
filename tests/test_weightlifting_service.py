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
        raise RuntimeError("database access is disabled during weightlifting tests")

    def putconn(self, _connection):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool

from backend.services import weightlifting_service


USER_ID = "11111111-1111-4111-8111-111111111111"
EXERCISE_ID = "22222222-2222-4222-8222-222222222222"
ENTRY_ID = "33333333-3333-4333-8333-333333333333"
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


SETTINGS_COLUMNS = [
    "user_id",
    "weight_unit",
    "default_weight_increment",
    "default_target_reps",
    "default_sets",
    "created_at",
    "updated_at",
]
SETTINGS_ROW = (
    USER_ID,
    "lb",
    Decimal("5.00"),
    12,
    3,
    NOW,
    NOW,
)
GRID_SETTINGS_COLUMNS = SETTINGS_COLUMNS[1:]
GRID_SETTINGS_ROW = SETTINGS_ROW[1:]
DAY_COLUMNS = ["slot", "label", "weekday"]
DAY_ROWS = [
    (1, "Day 1", "monday"),
    (2, "Day 2", "wednesday"),
    (3, "Day 3", "friday"),
]
DAY_ROWS_FOUR = DAY_ROWS + [(4, "Day 4", "saturday")]
EXERCISE_COLUMNS = [
    "id",
    "name",
    "display_order",
    "active",
    "notes",
    "target_reps",
    "target_sets",
    "weight_increment",
    "weight_unit",
    "created_at",
    "updated_at",
]
EXERCISE_ROW = (
    EXERCISE_ID,
    "Bench Press",
    1,
    True,
    None,
    12,
    3,
    Decimal("5.00"),
    "lb",
    NOW,
    NOW,
)
ENTRY_COLUMNS = [
    "id",
    "exercise_id",
    "week_start",
    "workout_day_slot",
    "workout_date",
    "weight",
    "reps",
    "sets",
    "notes",
    "completed",
    "created_at",
    "updated_at",
]
ENTRY_ROW = (
    ENTRY_ID,
    EXERCISE_ID,
    date(2026, 8, 3),
    1,
    date(2026, 8, 3),
    Decimal("47.50"),
    12,
    3,
    "solid",
    True,
    NOW,
    NOW,
)


def configured_slot_responses(slot: int = 1):
    return [
        ([], []),
        ([], []),
        ([], []),
        ([], []),
        (["exists"], [(1,)] if slot else []),
    ]


class FakeCursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.description = []
        self.executed = []
        self.rowcount = 1
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.responses:
            columns, rows = self.responses.pop(0)
            self.description = [(column,) for column in columns]
            self._rows = list(rows)
        else:
            self.description = []
            self._rows = []

    def fetchone(self):
        if not self._rows:
            return None
        return self._rows.pop(0)

    def fetchall(self):
        rows = self._rows
        self._rows = []
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


class WeightliftingRecommendationTests(unittest.TestCase):
    def test_first_entry_behavior_has_stable_reason_code(self):
        exercise = {
            "target_reps": 12,
            "target_sets": 3,
            "weight_increment": 5,
        }

        recommendation = weightlifting_service.recommendation_for_exercise(
            exercise,
            None,
        )

        self.assertIsNone(recommendation["weight"])
        self.assertEqual(recommendation["reason_code"], "no_history")
        self.assertEqual(recommendation["reps"], 12)
        self.assertEqual(recommendation["sets"], 3)

    def test_recommends_increase_when_target_is_met(self):
        exercise = {
            "target_reps": 12,
            "target_sets": 3,
            "weight_increment": Decimal("2.50"),
        }
        latest = {"weight": Decimal("47.50"), "reps": 12, "sets": 3}

        recommendation = weightlifting_service.recommendation_for_exercise(
            exercise,
            latest,
        )

        self.assertEqual(recommendation["weight"], 50.0)
        self.assertEqual(recommendation["reason_code"], "target_met_increase")

    def test_recommends_repeat_when_target_is_not_met(self):
        exercise = {
            "target_reps": 12,
            "target_sets": 3,
            "weight_increment": Decimal("5"),
        }
        latest = {"weight": Decimal("50"), "reps": 10, "sets": 3}

        recommendation = weightlifting_service.recommendation_for_exercise(
            exercise,
            latest,
        )

        self.assertEqual(recommendation["weight"], 50.0)
        self.assertEqual(recommendation["reason_code"], "target_not_met_repeat")

    def test_exercise_specific_sets_and_increment_drive_recommendation(self):
        exercise = {
            "target_reps": 8,
            "target_sets": 5,
            "weight_increment": Decimal("10"),
        }
        latest = {"weight": Decimal("135"), "reps": 8, "sets": 5}

        recommendation = weightlifting_service.recommendation_for_exercise(
            exercise,
            latest,
        )

        self.assertEqual(recommendation["weight"], 145.0)
        self.assertEqual(recommendation["reps"], 8)
        self.assertEqual(recommendation["sets"], 5)


class WeightliftingServiceDatabaseTests(unittest.TestCase):
    def patch_connection(self, responses):
        connection = FakeConnection(responses)
        return connection, patch.multiple(
            weightlifting_service,
            get_db_conn=lambda: connection,
            put_db_conn=lambda _connection: None,
        )

    def test_default_settings_have_exactly_three_workout_day_slots(self):
        connection, patches = self.patch_connection(
            [
                ([], []),
                ([], []),
                ([], []),
                ([], []),
                (SETTINGS_COLUMNS, [SETTINGS_ROW]),
                (DAY_COLUMNS, DAY_ROWS),
            ]
        )

        with patches:
            settings = weightlifting_service.get_settings(USER_ID)

        self.assertEqual(settings["weight_unit"], "lb")
        self.assertEqual([day["slot"] for day in settings["days"]], [1, 2, 3])
        self.assertEqual(connection.commits, 1)

    def test_settings_update_rejects_empty_day_configuration(self):
        with self.assertRaisesRegex(
            weightlifting_service.WeightliftingValidationError,
            "at least one",
        ):
            weightlifting_service.update_settings(
                user_id=USER_ID,
                weight_unit="lb",
                default_weight_increment=Decimal("5"),
                default_target_reps=12,
                default_sets=3,
                days=[],
            )

    def test_week_start_is_normalized_when_upserting_all_slots(self):
        for slot in (1, 2, 3):
            connection, patches = self.patch_connection(
                [
                    (EXERCISE_COLUMNS, [EXERCISE_ROW]),
                    *configured_slot_responses(slot),
                    (ENTRY_COLUMNS, [ENTRY_ROW]),
                ]
            )
            with patches:
                weightlifting_service.upsert_entry(
                    user_id=USER_ID,
                    exercise_id=EXERCISE_ID,
                    week_start=date(2026, 8, 5),
                    workout_day_slot=slot,
                    workout_date=date(2026, 8, 5),
                    weight=Decimal("47.5"),
                    reps=12,
                    sets=3,
                    notes=None,
                    completed=True,
                )
            insert_params = connection.cursor_instance.executed[6][1]
            self.assertEqual(insert_params[2], date(2026, 8, 3))
            self.assertEqual(insert_params[3], slot)

    def test_upsert_uses_one_entry_per_exercise_week_day_slot_conflict(self):
        connection, patches = self.patch_connection(
            [
                (EXERCISE_COLUMNS, [EXERCISE_ROW]),
                *configured_slot_responses(1),
                (ENTRY_COLUMNS, [ENTRY_ROW]),
            ]
        )

        with patches:
            entry = weightlifting_service.upsert_entry(
                user_id=USER_ID,
                exercise_id=EXERCISE_ID,
                week_start=date(2026, 8, 3),
                workout_day_slot=1,
                workout_date=date(2026, 8, 3),
                weight=Decimal("47.5"),
                reps=12,
                sets=3,
                notes="solid",
                completed=True,
            )

        upsert_sql = connection.cursor_instance.executed[6][0]
        self.assertIn(
            "ON CONFLICT (exercise_id, week_start, workout_day_slot)",
            upsert_sql,
        )
        self.assertIn("ELSE weightlifting_entries.fitness_scheduled_workout_id", upsert_sql)
        self.assertFalse(connection.cursor_instance.executed[6][1][-1])
        self.assertEqual(entry["weight"], 47.5)

    def test_upsert_rejects_invalid_fitness_scheduled_workout_linkage(self):
        connection, patches = self.patch_connection(
            [
                (EXERCISE_COLUMNS, [EXERCISE_ROW]),
                *configured_slot_responses(1),
                (["id"], []),
            ]
        )

        with patches:
            with self.assertRaisesRegex(
                weightlifting_service.WeightliftingValidationError,
                "same-user scheduled LIFTING workout",
            ):
                weightlifting_service.upsert_entry(
                    user_id=USER_ID,
                    exercise_id=EXERCISE_ID,
                    week_start=date(2026, 8, 3),
                    workout_day_slot=1,
                    workout_date=date(2026, 8, 3),
                    weight=Decimal("47.5"),
                    reps=12,
                    sets=3,
                    notes=None,
                    completed=True,
                    fitness_scheduled_workout_id="55555555-5555-4555-8555-555555555555",
                )

        linkage_sql = connection.cursor_instance.executed[6][0]
        self.assertIn("template.workout_type = 'LIFTING'", linkage_sql)
        self.assertIn("scheduled.user_id = %s", linkage_sql)

    def test_upsert_rejects_running_or_cross_user_scheduled_workout_linkage(self):
        rejection_cases = ("running scheduled workout", "cross-user scheduled workout")
        for rejection_case in rejection_cases:
            with self.subTest(rejection_case=rejection_case):
                connection, patches = self.patch_connection(
                    [
                        (EXERCISE_COLUMNS, [EXERCISE_ROW]),
                        *configured_slot_responses(1),
                        (["id"], []),
                    ]
                )

                with patches:
                    with self.assertRaisesRegex(
                        weightlifting_service.WeightliftingValidationError,
                        "same-user scheduled LIFTING workout",
                    ):
                        weightlifting_service.upsert_entry(
                            user_id=USER_ID,
                            exercise_id=EXERCISE_ID,
                            week_start=date(2026, 8, 3),
                            workout_day_slot=1,
                            workout_date=date(2026, 8, 3),
                            weight=Decimal("47.5"),
                            reps=12,
                            sets=3,
                            notes=None,
                            completed=True,
                            fitness_scheduled_workout_id="55555555-5555-4555-8555-555555555555",
                        )

                linkage_sql = connection.cursor_instance.executed[6][0]
                self.assertIn("template.workout_type = 'LIFTING'", linkage_sql)
                self.assertIn("scheduled.user_id = %s", linkage_sql)

    def test_upsert_accepts_same_user_lifting_scheduled_workout_linkage(self):
        scheduled_workout_id = "55555555-5555-4555-8555-555555555555"
        linked_entry = list(ENTRY_ROW)
        linked_entry.insert(10, scheduled_workout_id)
        linked_columns = ENTRY_COLUMNS[:10] + ["fitness_scheduled_workout_id"] + ENTRY_COLUMNS[10:]
        connection, patches = self.patch_connection(
            [
                (EXERCISE_COLUMNS, [EXERCISE_ROW]),
                *configured_slot_responses(1),
                (["id"], [(scheduled_workout_id,)]),
                (linked_columns, [tuple(linked_entry)]),
            ]
        )

        with patches:
            entry = weightlifting_service.upsert_entry(
                user_id=USER_ID,
                exercise_id=EXERCISE_ID,
                week_start=date(2026, 8, 3),
                workout_day_slot=1,
                workout_date=date(2026, 8, 3),
                weight=Decimal("47.5"),
                reps=12,
                sets=3,
                notes=None,
                completed=True,
                fitness_scheduled_workout_id=scheduled_workout_id,
            )

        linkage_sql = connection.cursor_instance.executed[6][0]
        upsert_params = connection.cursor_instance.executed[7][1]
        self.assertIn("template.workout_type = 'LIFTING'", linkage_sql)
        self.assertTrue(upsert_params[-1])
        self.assertEqual(entry["fitness_scheduled_workout_id"], scheduled_workout_id)

    def test_upsert_rejects_slot_that_is_not_currently_configured(self):
        connection, patches = self.patch_connection(
            [
                (EXERCISE_COLUMNS, [EXERCISE_ROW]),
                *configured_slot_responses(0),
            ]
        )

        with patches:
            with self.assertRaisesRegex(
                weightlifting_service.WeightliftingValidationError,
                "currently configured",
            ):
                weightlifting_service.upsert_entry(
                    user_id=USER_ID,
                    exercise_id=EXERCISE_ID,
                    week_start=date(2026, 8, 3),
                    workout_day_slot=4,
                    workout_date=date(2026, 8, 3),
                    weight=Decimal("47.5"),
                    reps=12,
                    sets=3,
                    notes=None,
                    completed=True,
                )

    def test_weekly_grid_aggregates_entries_and_empty_combinations(self):
        connection, patches = self.patch_connection(
            [
                ([], []),
                ([], []),
                ([], []),
                ([], []),
                (GRID_SETTINGS_COLUMNS, [GRID_SETTINGS_ROW]),
                (DAY_COLUMNS, DAY_ROWS),
                (EXERCISE_COLUMNS, [EXERCISE_ROW]),
                (ENTRY_COLUMNS, [ENTRY_ROW]),
                (ENTRY_COLUMNS, [ENTRY_ROW]),
            ]
        )

        with patches:
            grid = weightlifting_service.get_weekly_grid(
                user_id=USER_ID,
                week_start=date(2026, 8, 5),
            )

        self.assertEqual(grid["week_start"], "2026-08-03")
        self.assertEqual(len(grid["days"]), 3)
        self.assertEqual(grid["days"][1]["date"], "2026-08-05")
        exercise = grid["exercises"][0]
        self.assertIsNotNone(exercise["entries"]["1"])
        self.assertIsNone(exercise["entries"]["2"])
        self.assertIsNone(exercise["entries"]["3"])
        self.assertEqual(
            exercise["suggested_next"]["reason_code"],
            "target_met_increase",
        )

    def test_weekly_grid_dynamically_includes_fourth_slot(self):
        connection, patches = self.patch_connection(
            [
                ([], []),
                ([], []),
                ([], []),
                ([], []),
                (GRID_SETTINGS_COLUMNS, [GRID_SETTINGS_ROW]),
                (DAY_COLUMNS, DAY_ROWS_FOUR),
                (EXERCISE_COLUMNS, [EXERCISE_ROW]),
                (ENTRY_COLUMNS, [ENTRY_ROW]),
                (ENTRY_COLUMNS, [ENTRY_ROW]),
            ]
        )

        with patches:
            grid = weightlifting_service.get_weekly_grid(
                user_id=USER_ID,
                week_start=date(2026, 8, 5),
            )

        self.assertEqual([day["slot"] for day in grid["days"]], [1, 2, 3, 4])
        self.assertEqual(grid["days"][3]["date"], "2026-08-08")
        self.assertIn("4", grid["exercises"][0]["entries"])
        self.assertIsNone(grid["exercises"][0]["entries"]["4"])

    def test_weekly_grid_ignores_historical_entry_for_removed_slot(self):
        slot_four_entry = list(ENTRY_ROW)
        slot_four_entry[3] = 4
        connection, patches = self.patch_connection(
            [
                ([], []),
                ([], []),
                ([], []),
                ([], []),
                (GRID_SETTINGS_COLUMNS, [GRID_SETTINGS_ROW]),
                (DAY_COLUMNS, DAY_ROWS),
                (EXERCISE_COLUMNS, [EXERCISE_ROW]),
                (ENTRY_COLUMNS, [tuple(slot_four_entry)]),
                (ENTRY_COLUMNS, [tuple(slot_four_entry)]),
            ]
        )

        with patches:
            grid = weightlifting_service.get_weekly_grid(
                user_id=USER_ID,
                week_start=date(2026, 8, 5),
            )

        exercise = grid["exercises"][0]
        self.assertEqual(set(exercise["entries"].keys()), {"1", "2", "3"})
        self.assertIsNone(exercise["entries"]["1"])
        self.assertEqual(exercise["previous_performance"]["workout_day_slot"], 4)

    def test_exercise_history_retains_removed_slot_entry(self):
        slot_four_entry = list(ENTRY_ROW)
        slot_four_entry[3] = 4
        connection, patches = self.patch_connection(
            [
                (EXERCISE_COLUMNS, [EXERCISE_ROW]),
                (ENTRY_COLUMNS, [tuple(slot_four_entry)]),
            ]
        )

        with patches:
            history = weightlifting_service.get_exercise_history(
                user_id=USER_ID,
                exercise_id=EXERCISE_ID,
            )

        self.assertEqual(history["entries"][0]["workout_day_slot"], 4)
        self.assertEqual(history["series"][0]["workout_day_slot"], 4)

    def test_removed_slot_sequence_keeps_old_grid_and_history_readable(self):
        slot_four_entry = list(ENTRY_ROW)
        slot_four_entry[3] = 4
        slot_four_entry[4] = date(2026, 8, 8)
        slot_four_entry = tuple(slot_four_entry)
        connection, patches = self.patch_connection(
            [
                *[([], []) for _ in range(10)],
                (SETTINGS_COLUMNS, [SETTINGS_ROW]),
                (DAY_COLUMNS, DAY_ROWS_FOUR),
                (EXERCISE_COLUMNS, [EXERCISE_ROW]),
                *configured_slot_responses(4),
                (ENTRY_COLUMNS, [slot_four_entry]),
                *[([], []) for _ in range(9)],
                (SETTINGS_COLUMNS, [SETTINGS_ROW]),
                (DAY_COLUMNS, DAY_ROWS),
                *[([], []) for _ in range(4)],
                (GRID_SETTINGS_COLUMNS, [GRID_SETTINGS_ROW]),
                (DAY_COLUMNS, DAY_ROWS),
                (EXERCISE_COLUMNS, [EXERCISE_ROW]),
                (ENTRY_COLUMNS, [slot_four_entry]),
                (ENTRY_COLUMNS, [slot_four_entry]),
                (EXERCISE_COLUMNS, [EXERCISE_ROW]),
                (ENTRY_COLUMNS, [slot_four_entry]),
            ]
        )

        with patches:
            four_day_settings = weightlifting_service.update_settings(
                user_id=USER_ID,
                weight_unit="lb",
                default_weight_increment=Decimal("5"),
                default_target_reps=12,
                default_sets=3,
                days=[
                    {"slot": 1, "label": "Day 1", "weekday": "monday"},
                    {"slot": 2, "label": "Day 2", "weekday": "wednesday"},
                    {"slot": 3, "label": "Day 3", "weekday": "friday"},
                    {"slot": 4, "label": "Day 4", "weekday": "saturday"},
                ],
            )
            recorded = weightlifting_service.upsert_entry(
                user_id=USER_ID,
                exercise_id=EXERCISE_ID,
                week_start=date(2026, 8, 3),
                workout_day_slot=4,
                workout_date=date(2026, 8, 8),
                weight=Decimal("47.5"),
                reps=12,
                sets=3,
                notes=None,
                completed=True,
            )
            three_day_settings = weightlifting_service.update_settings(
                user_id=USER_ID,
                weight_unit="lb",
                default_weight_increment=Decimal("5"),
                default_target_reps=12,
                default_sets=3,
                days=[
                    {"slot": 1, "label": "Day 1", "weekday": "monday"},
                    {"slot": 2, "label": "Day 2", "weekday": "wednesday"},
                    {"slot": 3, "label": "Day 3", "weekday": "friday"},
                ],
            )
            grid = weightlifting_service.get_weekly_grid(
                user_id=USER_ID,
                week_start=date(2026, 8, 3),
            )
            history = weightlifting_service.get_exercise_history(
                user_id=USER_ID,
                exercise_id=EXERCISE_ID,
            )

        self.assertEqual([day["slot"] for day in four_day_settings["days"]], [1, 2, 3, 4])
        self.assertEqual(recorded["workout_day_slot"], 4)
        self.assertEqual([day["slot"] for day in three_day_settings["days"]], [1, 2, 3])
        self.assertEqual(set(grid["exercises"][0]["entries"].keys()), {"1", "2", "3"})
        self.assertEqual(grid["exercises"][0]["previous_performance"]["workout_day_slot"], 4)
        self.assertEqual(history["entries"][0]["workout_day_slot"], 4)

    def test_exercise_history_is_newest_first_with_chronological_series(self):
        older = list(ENTRY_ROW)
        older[0] = "44444444-4444-4444-8444-444444444444"
        older[2] = date(2026, 7, 27)
        older[5] = Decimal("45.00")
        newest = ENTRY_ROW
        connection, patches = self.patch_connection(
            [
                (EXERCISE_COLUMNS, [EXERCISE_ROW]),
                (ENTRY_COLUMNS, [newest, tuple(older)]),
            ]
        )

        with patches:
            history = weightlifting_service.get_exercise_history(
                user_id=USER_ID,
                exercise_id=EXERCISE_ID,
            )

        self.assertEqual(history["entries"][0]["week_start"], "2026-08-03")
        self.assertEqual(
            history["entries"][0]["recommendation_after"]["reason_code"],
            "target_met_increase",
        )
        self.assertEqual(history["series"][0]["week_start"], "2026-07-27")
        self.assertEqual(history["series"][1]["weight"], 47.5)

    def test_archived_exercise_remains_readable_in_history(self):
        archived = list(EXERCISE_ROW)
        archived[3] = False
        connection, patches = self.patch_connection(
            [
                (EXERCISE_COLUMNS, [tuple(archived)]),
                (ENTRY_COLUMNS, [ENTRY_ROW]),
            ]
        )

        with patches:
            history = weightlifting_service.get_exercise_history(
                user_id=USER_ID,
                exercise_id=EXERCISE_ID,
            )

        self.assertFalse(history["exercise"]["active"])
        self.assertEqual(len(history["entries"]), 1)

    def test_clear_entry_reports_empty_cell_predictably(self):
        connection, patches = self.patch_connection(
            [
                (["id"], []),
            ]
        )

        with patches:
            result = weightlifting_service.clear_entry(
                user_id=USER_ID,
                exercise_id=EXERCISE_ID,
                week_start=date(2026, 8, 6),
                workout_day_slot=2,
            )

        self.assertFalse(result["deleted"])
        self.assertEqual(result["week_start"], "2026-08-03")

    def test_fourth_slot_is_allowed_for_flexible_schedules(self):
        connection, patches = self.patch_connection(
            [
                (["id"], []),
            ]
        )
        with patches:
            result = weightlifting_service.clear_entry(
                user_id=USER_ID,
                exercise_id=EXERCISE_ID,
                week_start=date(2026, 8, 3),
                workout_day_slot=4,
            )

        self.assertFalse(result["deleted"])
        self.assertEqual(result["workout_day_slot"], 4)

    def test_non_positive_slot_is_rejected(self):
        with self.assertRaisesRegex(
            weightlifting_service.WeightliftingValidationError,
            "positive",
        ):
            weightlifting_service.clear_entry(
                user_id=USER_ID,
                exercise_id=EXERCISE_ID,
                week_start=date(2026, 8, 3),
                workout_day_slot=0,
            )


if __name__ == "__main__":
    unittest.main()

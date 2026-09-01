from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from psycopg2 import pool


class OfflineThreadedConnectionPool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise RuntimeError("database access is disabled during Fitness service tests")

    def putconn(self, _connection):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool

from backend.services import fitness_service
from backend.services import garmin_activity_provider


USER_ID = "11111111-1111-4111-8111-111111111111"
SCHEDULED_ID = "33333333-3333-4333-8333-333333333333"
TEMPLATE_ID = "22222222-2222-4222-8222-222222222222"
SECOND_TEMPLATE_ID = "88888888-8888-4888-8888-888888888888"
SECOND_SCHEDULED_ID = "99999999-9999-4999-8999-999999999999"
EXERCISE_ID = "77777777-7777-4777-8777-777777777777"
SERIES_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)

TEMPLATE_COLUMNS = [
    "id",
    "user_id",
    "name",
    "type",
    "notes",
    "active",
    "planned_distance_miles",
    "created_at",
    "updated_at",
]
LIFTING_TEMPLATE_ROW = (
    TEMPLATE_ID,
    USER_ID,
    "Full Body",
    "LIFTING",
    None,
    True,
    None,
    NOW,
    NOW,
)
RUNNING_TEMPLATE_ROW = (
    TEMPLATE_ID,
    USER_ID,
    "Long Run",
    "RUNNING",
    None,
    True,
    Decimal("5.00"),
    NOW,
    NOW,
)
SECOND_RUNNING_TEMPLATE_ROW = (
    SECOND_TEMPLATE_ID,
    USER_ID,
    "Easy Run",
    "RUNNING",
    None,
    True,
    Decimal("3.00"),
    NOW,
    NOW,
)
PLAN_TEMPLATE_ID = "66666666-6666-4666-8666-666666666666"
PLAN_TEMPLATE_COLUMNS = [
    "id",
    "user_id",
    "name",
    "notes",
    "active",
    "created_at",
    "updated_at",
]
PLAN_TEMPLATE_ROW = (
    PLAN_TEMPLATE_ID,
    USER_ID,
    "C25K",
    None,
    True,
    NOW,
    NOW,
)
PLAN_ITEM_COLUMNS = [
    "id",
    "workout_template_id",
    "day_offset",
    "display_order",
    "workout_name",
    "type",
]
PLAN_ITEM_ROWS = [
    ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", TEMPLATE_ID, 0, 1, "W1D1", "RUNNING"),
    ("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", SECOND_TEMPLATE_ID, 2, 2, "W1D2", "RUNNING"),
    ("cccccccc-cccc-4ccc-8ccc-cccccccccccc", TEMPLATE_ID, 2, 3, "Full Body", "LIFTING"),
]

SCHEDULED_COLUMNS = [
    "id",
    "user_id",
    "workout_template_id",
    "plan_instance_id",
    "scheduled_date",
    "original_scheduled_date",
    "status",
    "replacement_scheduled_workout_id",
    "recurring_series_id",
    "planned_distance_miles",
    "workout_name",
    "type",
    "recurring_series_weekdays",
    "recurring_series_status",
    "plan_template_name",
    "result_planned_distance_miles",
    "completed_distance_miles",
    "duration_seconds",
    "result_notes",
    "result_created_at",
    "result_updated_at",
    "created_at",
    "updated_at",
]

PLANNED_RUNNING_ROW = (
    SCHEDULED_ID,
    USER_ID,
    TEMPLATE_ID,
    None,
    date(2026, 8, 21),
    date(2026, 8, 21),
    "PLANNED",
    None,
    None,
    Decimal("5.00"),
    "Long Run",
    "RUNNING",
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    NOW,
    NOW,
)

COMPLETED_RUNNING_ROW = list(PLANNED_RUNNING_ROW)
COMPLETED_RUNNING_ROW[6] = "COMPLETED"
COMPLETED_RUNNING_ROW[15] = Decimal("5.00")
COMPLETED_RUNNING_ROW[16] = Decimal("5.10")
COMPLETED_RUNNING_ROW[17] = 1860
COMPLETED_RUNNING_ROW[18] = "felt good"
COMPLETED_RUNNING_ROW[19] = NOW
COMPLETED_RUNNING_ROW[20] = NOW
COMPLETED_RUNNING_ROW = tuple(COMPLETED_RUNNING_ROW)

HISTORICAL_EFFORT_COLUMNS = [
    "scheduled_workout_id",
    "scheduled_date",
    "status",
    "completed_distance_miles",
    "duration_seconds",
    "moving_duration_seconds",
    "average_hr",
    "max_hr",
    "training_load",
    "aerobic_training_effect",
    "anaerobic_training_effect",
    "vo2_max",
    "hr_zone_1_seconds",
    "hr_zone_2_seconds",
    "hr_zone_3_seconds",
    "hr_zone_4_seconds",
    "hr_zone_5_seconds",
    "average_cadence_spm",
    "average_power_watts",
    "average_stride_length_meters",
    "elevation_gain_meters",
    "total_efforts",
]

PLAN_INSTANCE_COLUMNS = [
    "id",
    "user_id",
    "plan_template_id",
    "plan_template_name",
    "start_date",
    "status",
    "stopped_at",
    "created_at",
    "updated_at",
]
PLAN_INSTANCE_ID = "55555555-5555-4555-8555-555555555555"
PLAN_INSTANCE_ROW = (
    PLAN_INSTANCE_ID,
    USER_ID,
    "66666666-6666-4666-8666-666666666666",
    "C25K",
    date(2026, 8, 21),
    "ACTIVE",
    None,
    NOW,
    NOW,
)
COMPLETED_PLAN_INSTANCE_ROW = list(PLAN_INSTANCE_ROW)
COMPLETED_PLAN_INSTANCE_ROW[5] = "COMPLETED"
COMPLETED_PLAN_INSTANCE_ROW = tuple(COMPLETED_PLAN_INSTANCE_ROW)
STOPPED_PLAN_INSTANCE_ROW = list(PLAN_INSTANCE_ROW)
STOPPED_PLAN_INSTANCE_ROW[6] = NOW
STOPPED_PLAN_INSTANCE_ROW = tuple(STOPPED_PLAN_INSTANCE_ROW)

RECURRING_SERIES_COLUMNS = [
    "id",
    "user_id",
    "workout_template_id",
    "workout_name",
    "type",
    "start_date",
    "end_date",
    "duration_weeks",
    "weekdays",
    "status",
    "idempotency_key",
    "created_at",
    "updated_at",
    "stopped_at",
]
RECURRING_SERIES_ROW = (
    SERIES_ID,
    USER_ID,
    TEMPLATE_ID,
    "Long Run",
    "RUNNING",
    date(2026, 8, 31),
    date(2026, 10, 4),
    5,
    [1, 3, 5],
    "ACTIVE",
    "retry-key",
    NOW,
    NOW,
    None,
)


class FakeCursor:
    def __init__(self, columns=None, rows=None):
        self.description = [(column,) for column in (columns or [])]
        self.rows = list(rows or [])
        self.executed = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        if not self.rows:
            return None
        return self.rows.pop(0)

    def fetchall(self):
        rows = self.rows
        self.rows = []
        return rows


class SequenceConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FitnessServiceTests(unittest.TestCase):
    def patch_connection(self, responses, rowcounts=None):
        cursor = FakeCursor()
        original_execute = cursor.execute
        rowcounts = list(rowcounts or [])

        def execute(sql, params=None):
            original_execute(sql, params)
            cursor.rowcount = rowcounts.pop(0) if rowcounts else 1
            if responses:
                columns, rows = responses.pop(0)
                cursor.description = [(column,) for column in columns]
                cursor.rows = list(rows)
            else:
                cursor.description = []
                cursor.rows = []

        cursor.execute = execute
        connection = SequenceConnection(cursor)
        return connection, patch.multiple(
            fitness_service,
            get_db_conn=lambda: connection,
            put_db_conn=lambda _connection: None,
        )

    def test_running_template_creation_persists_planned_distance(self):
        connection, patches = self.patch_connection(
            [
                (["id"], [(TEMPLATE_ID,)]),
                ([], []),
                (TEMPLATE_COLUMNS, [RUNNING_TEMPLATE_ROW]),
            ]
        )

        with patches:
            template = fitness_service.create_workout_template(
                user_id=USER_ID,
                name="Long Run",
                workout_type="RUNNING",
                planned_distance_miles=Decimal("5.0"),
            )

        self.assertEqual(connection.cursor_instance.executed[1][1][1], Decimal("5.0"))
        self.assertEqual(template["planned_distance_miles"], 5.0)

    def test_lifting_template_creation_persists_ordered_exercise_membership(self):
        second_exercise_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        connection, patches = self.patch_connection(
            [
                (["id"], [(TEMPLATE_ID,)]),
                (TEMPLATE_COLUMNS, [LIFTING_TEMPLATE_ROW]),
                (["id"], [(EXERCISE_ID,), (second_exercise_id,)]),
                ([], []),
                ([], []),
                ([], []),
                (TEMPLATE_COLUMNS, [LIFTING_TEMPLATE_ROW]),
                (
                    ["exercise_id", "name", "active", "display_order"],
                    [
                        (EXERCISE_ID, "Bench Press", True, 1),
                        (second_exercise_id, "Leg Press", True, 2),
                    ],
                ),
            ]
        )

        with patches:
            template = fitness_service.create_workout_template(
                user_id=USER_ID,
                name="Full Body",
                workout_type="LIFTING",
                exercises=[
                    {"exercise_id": EXERCISE_ID, "display_order": 1},
                    {"exercise_id": second_exercise_id, "display_order": 2},
                ],
            )

        member_inserts = connection.cursor_instance.executed[4:6]
        self.assertEqual(member_inserts[0][1][2], 1)
        self.assertEqual(member_inserts[1][1][2], 2)
        self.assertEqual([item["display_order"] for item in template["exercises"]], [1, 2])

    def test_plan_instantiation_generates_dates_and_no_gap_workout(self):
        second_running_template = list(RUNNING_TEMPLATE_ROW)
        second_running_template[0] = SECOND_TEMPLATE_ID
        second_running_template[2] = "W1D2"
        second_running_template[6] = Decimal("2.00")
        connection, patches = self.patch_connection(
            [
                (PLAN_TEMPLATE_COLUMNS, [PLAN_TEMPLATE_ROW]),
                (PLAN_ITEM_COLUMNS, PLAN_ITEM_ROWS),
                (["id"], [(PLAN_INSTANCE_ID,)]),
                (TEMPLATE_COLUMNS, [RUNNING_TEMPLATE_ROW]),
                (["id"], [(SCHEDULED_ID,)]),
                (TEMPLATE_COLUMNS, [tuple(second_running_template)]),
                (["id"], [(SECOND_SCHEDULED_ID,)]),
                (TEMPLATE_COLUMNS, [LIFTING_TEMPLATE_ROW]),
                (["id"], [("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",)]),
                (PLAN_INSTANCE_COLUMNS, [PLAN_INSTANCE_ROW]),
            ]
        )

        with patches:
            instance = fitness_service.instantiate_plan_template(
                user_id=USER_ID,
                plan_template_id=PLAN_TEMPLATE_ID,
                start_date=date(2026, 8, 10),
            )

        scheduled_dates = [
            params[4]
            for sql, params in connection.cursor_instance.executed
            if "INSERT INTO public.fitness_scheduled_workouts" in sql
        ]
        self.assertEqual(
            scheduled_dates,
            [date(2026, 8, 10), date(2026, 8, 12), date(2026, 8, 12)],
        )
        self.assertEqual(len(instance["scheduled_workout_ids"]), 3)

    def test_multiple_scheduled_workouts_can_share_one_date(self):
        second_row = list(PLANNED_RUNNING_ROW)
        second_row[0] = SECOND_SCHEDULED_ID
        second_row[11] = "LIFTING"
        second_row = tuple(second_row)
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW, second_row]),
            ]
        )

        with patches:
            workouts = fitness_service.list_scheduled_workouts(
                user_id=USER_ID,
                start_date=date(2026, 8, 21),
                end_date=date(2026, 8, 21),
            )

        self.assertEqual(len(workouts), 2)
        self.assertEqual({workout["scheduled_date"] for workout in workouts}, {"2026-08-21"})

    def test_running_completion_uses_scheduled_planned_distance_snapshot(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                ([], []),
                ([], []),
                (SCHEDULED_COLUMNS, [COMPLETED_RUNNING_ROW]),
            ]
        )

        with patches:
            result = fitness_service.complete_scheduled_workout(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
                running={
                    "completed_distance_miles": Decimal("5.1"),
                    "duration_seconds": 1860,
                    "notes": "felt good",
                },
            )

        insert_params = connection.cursor_instance.executed[1][1]
        self.assertEqual(insert_params[1], 5.0)
        self.assertEqual(insert_params[2], Decimal("5.1"))
        self.assertIn("FOR UPDATE", connection.cursor_instance.executed[0][0])
        self.assertIn("AND status = 'PLANNED'", connection.cursor_instance.executed[2][0])
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["running_result"]["completed_distance_miles"], 5.1)
        self.assertEqual(connection.commits, 1)

    def garmin_activity(self, activity_id="garmin-1"):
        return garmin_activity_provider.GarminRunningActivity(
            external_provider="GARMIN",
            external_activity_id=activity_id,
            external_activity_uuid="uuid-1",
            external_activity_name="Morning Run",
            completed_distance_miles=Decimal("3.106855961186669707904048349"),
            duration_seconds=1801,
            moving_duration_seconds=1799,
            average_speed_meters_per_second=Decimal("2.777778"),
            average_hr=Decimal("150"),
            max_hr=Decimal("180"),
            training_load=Decimal("52.5"),
            aerobic_training_effect=Decimal("3.2"),
            anaerobic_training_effect=Decimal("0.4"),
            training_effect_label="MAINTAINING",
            vo2_max=Decimal("45"),
            hr_zone_1_seconds=11,
            hr_zone_2_seconds=20,
            hr_zone_3_seconds=31,
            hr_zone_4_seconds=40,
            hr_zone_5_seconds=51,
            average_cadence_spm=Decimal("172"),
            average_power_watts=Decimal("245"),
            average_stride_length_meters=Decimal("0.8809"),
            elevation_gain_meters=Decimal("25.2"),
            elevation_loss_meters=Decimal("24.7"),
            calories=Decimal("410"),
            steps=6400,
        )

    def test_garmin_completion_zero_results_returns_no_match_without_mutation(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
            ]
        )
        resolution = garmin_activity_provider.GarminActivityResolution(
            activities=[],
            candidates=[],
        )

        with patches, patch(
            "backend.services.fitness_service.garmin_activity_provider.resolve_running_activities",
            return_value=resolution,
        ):
            result = fitness_service.attempt_garmin_scheduled_workout_completion(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
            )

        self.assertEqual(result, {"status": "NO_MATCH"})
        self.assertFalse(
            any(
                "INSERT INTO public.fitness_running_workout_results" in sql
                or "UPDATE public.fitness_scheduled_workouts" in sql
                for sql, _ in connection.cursor_instance.executed
            )
        )
        self.assertEqual(connection.commits, 0)

    def test_garmin_completion_one_result_auto_matches_and_persists_linkage(self):
        activity = self.garmin_activity()
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                ([], []),
                ([], []),
                (SCHEDULED_COLUMNS, [COMPLETED_RUNNING_ROW]),
            ]
        )
        resolution = garmin_activity_provider.GarminActivityResolution(
            activities=[activity],
            candidates=[],
        )

        with patches, patch(
            "backend.services.fitness_service.garmin_activity_provider.resolve_running_activities",
            return_value=resolution,
        ):
            result = fitness_service.attempt_garmin_scheduled_workout_completion(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
            )

        insert_sql, insert_params = connection.cursor_instance.executed[2]
        self.assertEqual(result["status"], "COMPLETED")
        self.assertIn("external_provider", insert_sql)
        self.assertIn("external_activity_id", insert_sql)
        self.assertEqual(insert_params[1], 5.0)
        self.assertEqual(insert_params[2], activity.completed_distance_miles)
        self.assertEqual(insert_params[5], "GARMIN")
        self.assertEqual(insert_params[6], "garmin-1")
        self.assertEqual(connection.commits, 1)

    def test_garmin_completion_multiple_results_returns_candidates_without_guessing(self):
        candidate_one = garmin_activity_provider.GarminActivityCandidate(
            activity_id="1",
            activity_name="Morning Run",
            start_time_local="2026-08-21 07:00:00",
            distance=Decimal("5000"),
            duration=1800,
        )
        candidate_two = garmin_activity_provider.GarminActivityCandidate(
            activity_id="2",
            activity_name="Evening Run",
            start_time_local="2026-08-21 18:00:00",
            distance=Decimal("3000"),
            duration=1200,
        )
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
            ]
        )
        resolution = garmin_activity_provider.GarminActivityResolution(
            activities=[],
            candidates=[candidate_one, candidate_two],
        )

        with patches, patch(
            "backend.services.fitness_service.garmin_activity_provider.resolve_running_activities",
            return_value=resolution,
        ):
            result = fitness_service.attempt_garmin_scheduled_workout_completion(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
            )

        self.assertEqual(result["status"], "AMBIGUOUS_MATCH")
        self.assertEqual([candidate["activityId"] for candidate in result["candidates"]], ["1", "2"])
        self.assertFalse(
            any(
                "INSERT INTO public.fitness_running_workout_results" in sql
                for sql, _ in connection.cursor_instance.executed
            )
        )

    def test_selected_garmin_activity_must_be_from_exact_date_query(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
            ]
        )

        with patches, patch(
            "backend.services.fitness_service.garmin_activity_provider.find_running_activity",
            return_value=None,
        ) as find_activity:
            with self.assertRaisesRegex(fitness_service.FitnessValidationError, "Selected Garmin activity"):
                fitness_service.complete_scheduled_workout_with_garmin_activity(
                    user_id=USER_ID,
                    scheduled_workout_id=SCHEDULED_ID,
                    activity_id="missing",
                )

        self.assertEqual(find_activity.call_args.kwargs["activity_id"], "missing")
        self.assertEqual(find_activity.call_args.args[0], date(2026, 8, 21))
        self.assertEqual(connection.commits, 0)

    def test_selected_garmin_activity_persists_selected_activity(self):
        activity = self.garmin_activity(activity_id="selected")
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                ([], []),
                ([], []),
                (SCHEDULED_COLUMNS, [COMPLETED_RUNNING_ROW]),
            ]
        )

        with patches, patch(
            "backend.services.fitness_service.garmin_activity_provider.find_running_activity",
            return_value=activity,
        ):
            result = fitness_service.complete_scheduled_workout_with_garmin_activity(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
                activity_id="selected",
            )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(connection.cursor_instance.executed[2][1][6], "selected")
        self.assertEqual(connection.commits, 1)

    def test_reschedule_copies_planned_distance_snapshot_to_replacement(self):
        replacement_id = "44444444-4444-4444-8444-444444444444"
        replacement_row = list(PLANNED_RUNNING_ROW)
        replacement_row[0] = replacement_id
        replacement_row[4] = date(2026, 8, 22)
        replacement_row = tuple(replacement_row)
        original_rescheduled = list(PLANNED_RUNNING_ROW)
        original_rescheduled[6] = "RESCHEDULED"
        original_rescheduled[7] = replacement_id
        original_rescheduled = tuple(original_rescheduled)
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                (["id"], [(replacement_id,)]),
                ([], []),
                (SCHEDULED_COLUMNS, [original_rescheduled]),
                (SCHEDULED_COLUMNS, [replacement_row]),
            ]
        )

        with patches:
            result = fitness_service.reschedule_scheduled_workout(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
                scheduled_date=date(2026, 8, 22),
            )

        insert_params = connection.cursor_instance.executed[1][1]
        self.assertEqual(insert_params[6], 5.0)
        self.assertIn("FOR UPDATE", connection.cursor_instance.executed[0][0])
        self.assertIn("AND status = 'PLANNED'", connection.cursor_instance.executed[2][0])
        self.assertEqual(result["replacement"]["planned_distance_miles"], 5.0)

    def test_reschedule_copies_recurring_series_to_replacement(self):
        recurring_row = list(PLANNED_RUNNING_ROW)
        recurring_row[8] = SERIES_ID
        replacement_id = "44444444-4444-4444-8444-444444444444"
        replacement_row = list(recurring_row)
        replacement_row[0] = replacement_id
        replacement_row[4] = date(2026, 8, 22)
        original_rescheduled = list(recurring_row)
        original_rescheduled[6] = "RESCHEDULED"
        original_rescheduled[7] = replacement_id
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [tuple(recurring_row)]),
                (["id"], [(replacement_id,)]),
                ([], []),
                (SCHEDULED_COLUMNS, [tuple(original_rescheduled)]),
                (SCHEDULED_COLUMNS, [tuple(replacement_row)]),
            ]
        )

        with patches:
            fitness_service.reschedule_scheduled_workout(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
                scheduled_date=date(2026, 8, 22),
            )

        insert_params = connection.cursor_instance.executed[1][1]
        self.assertEqual(insert_params[3], SERIES_ID)

    def test_replace_scheduled_workout_template_updates_one_planned_occurrence(self):
        original = list(PLANNED_RUNNING_ROW)
        original[3] = PLAN_INSTANCE_ID
        original[8] = SERIES_ID
        updated = list(original)
        updated[2] = SECOND_TEMPLATE_ID
        updated[9] = Decimal("3.00")
        updated[10] = "Easy Run"
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [tuple(original)]),
                ([], []),
                (TEMPLATE_COLUMNS, [SECOND_RUNNING_TEMPLATE_ROW]),
                ([], []),
                (SCHEDULED_COLUMNS, [tuple(updated)]),
            ]
        )

        with patches:
            result = fitness_service.replace_scheduled_workout_template(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
                workout_template_id=SECOND_TEMPLATE_ID,
            )

        update_sql, update_params = connection.cursor_instance.executed[3]
        self.assertIn("UPDATE public.fitness_scheduled_workouts", update_sql)
        self.assertIn("workout_template_id = %s", update_sql)
        self.assertIn("planned_distance_miles = %s", update_sql)
        self.assertIn("WHERE id = %s", update_sql)
        self.assertIn("AND user_id = %s", update_sql)
        self.assertIn("AND status = 'PLANNED'", update_sql)
        self.assertEqual(update_params, (SECOND_TEMPLATE_ID, Decimal("3.00"), SCHEDULED_ID, USER_ID))
        self.assertIn("FOR UPDATE", connection.cursor_instance.executed[0][0])
        self.assertEqual(result["workout_template_id"], SECOND_TEMPLATE_ID)
        self.assertEqual(result["planned_distance_miles"], 3.0)
        self.assertEqual(result["scheduled_date"], "2026-08-21")
        self.assertEqual(result["original_scheduled_date"], "2026-08-21")
        self.assertEqual(result["plan_instance_id"], PLAN_INSTANCE_ID)
        self.assertEqual(result["recurring_series_id"], SERIES_ID)
        self.assertFalse(
            any(
                "UPDATE public.fitness_training_plan" in sql
                or "UPDATE public.fitness_recurring_schedule_series" in sql
                for sql, _ in connection.cursor_instance.executed
            )
        )
        self.assertEqual(connection.commits, 1)

    def test_replace_scheduled_workout_template_rejects_incompatible_type(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                ([], []),
                (TEMPLATE_COLUMNS, [LIFTING_TEMPLATE_ROW]),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessValidationError, "match workout type"):
                fitness_service.replace_scheduled_workout_template(
                    user_id=USER_ID,
                    scheduled_workout_id=SCHEDULED_ID,
                    workout_template_id=TEMPLATE_ID,
                )

        self.assertFalse(
            any(
                "UPDATE public.fitness_scheduled_workouts" in sql
                for sql, _ in connection.cursor_instance.executed
            )
        )
        self.assertEqual(connection.rollbacks, 1)

    def test_replace_scheduled_workout_template_rejects_completed_workout(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [COMPLETED_RUNNING_ROW]),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessConflictError, "planned"):
                fitness_service.replace_scheduled_workout_template(
                    user_id=USER_ID,
                    scheduled_workout_id=SCHEDULED_ID,
                    workout_template_id=SECOND_TEMPLATE_ID,
                )

        self.assertFalse(
            any(
                "UPDATE public.fitness_scheduled_workouts" in sql
                for sql, _ in connection.cursor_instance.executed
            )
        )

    def test_replace_scheduled_workout_template_enforces_scheduled_workout_ownership_first(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, []),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessNotFoundError, "Scheduled workout not found"):
                fitness_service.replace_scheduled_workout_template(
                    user_id=USER_ID,
                    scheduled_workout_id=SCHEDULED_ID,
                    workout_template_id=SECOND_TEMPLATE_ID,
                )

        self.assertEqual(len(connection.cursor_instance.executed), 1)
        lookup_sql, lookup_params = connection.cursor_instance.executed[0]
        self.assertIn("WHERE scheduled.id = %s", lookup_sql)
        self.assertIn("AND scheduled.user_id = %s", lookup_sql)
        self.assertEqual(lookup_params, (SCHEDULED_ID, USER_ID))
        self.assertFalse(
            any(
                "UPDATE public.fitness_scheduled_workouts" in sql
                for sql, _ in connection.cursor_instance.executed
            )
        )
        self.assertEqual(connection.rollbacks, 1)

    def test_replace_scheduled_workout_template_rejects_running_result_linked_workout(self):
        planned_with_result = list(COMPLETED_RUNNING_ROW)
        planned_with_result[6] = "PLANNED"
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [tuple(planned_with_result)]),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessConflictError, "Running result"):
                fitness_service.replace_scheduled_workout_template(
                    user_id=USER_ID,
                    scheduled_workout_id=SCHEDULED_ID,
                    workout_template_id=SECOND_TEMPLATE_ID,
                )

    def test_replace_scheduled_workout_template_rejects_weightlifting_result_linked_workout(self):
        lifting_workout = list(PLANNED_RUNNING_ROW)
        lifting_workout[11] = "LIFTING"
        lifting_workout[9] = None
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [tuple(lifting_workout)]),
                (["?column?"], [(1,)]),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessConflictError, "Weightlifting"):
                fitness_service.replace_scheduled_workout_template(
                    user_id=USER_ID,
                    scheduled_workout_id=SCHEDULED_ID,
                    workout_template_id=TEMPLATE_ID,
                )

    def test_replace_scheduled_workout_template_enforces_template_ownership(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                ([], []),
                (TEMPLATE_COLUMNS, []),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessNotFoundError, "Workout template not found"):
                fitness_service.replace_scheduled_workout_template(
                    user_id=USER_ID,
                    scheduled_workout_id=SCHEDULED_ID,
                    workout_template_id=SECOND_TEMPLATE_ID,
                )

        template_params = connection.cursor_instance.executed[2][1]
        self.assertEqual(template_params, (SECOND_TEMPLATE_ID, USER_ID))
        self.assertFalse(
            any(
                "UPDATE public.fitness_scheduled_workouts" in sql
                for sql, _ in connection.cursor_instance.executed
            )
        )

    def test_recurrence_generator_uses_iso_weekdays_and_duration_range(self):
        end_date, weekdays, duration, dates = fitness_service._recurrence_dates(
            start_date=date(2026, 8, 31),
            weekdays=[5, 1, 3],
            duration_weeks=5,
        )

        self.assertEqual(end_date, date(2026, 10, 4))
        self.assertEqual(weekdays, [1, 3, 5])
        self.assertEqual(duration, 5)
        self.assertEqual(len(dates), 15)
        self.assertEqual(dates[:3], [date(2026, 8, 31), date(2026, 9, 2), date(2026, 9, 4)])
        self.assertEqual(dates[-1], date(2026, 10, 2))

    def test_recurrence_generator_accepts_maximum_end_date_only_range(self):
        start = date(2026, 8, 31)
        max_end = start + timedelta(days=(7 * fitness_service.FITNESS_RECURRENCE_MAX_WEEKS) - 1)

        end_date, weekdays, duration, dates = fitness_service._recurrence_dates(
            start_date=start,
            weekdays=[1],
            end_date=max_end,
        )

        self.assertEqual(end_date, max_end)
        self.assertEqual(weekdays, [1])
        self.assertIsNone(duration)
        self.assertEqual(len(dates), fitness_service.FITNESS_RECURRENCE_MAX_WEEKS)

    def test_recurrence_generator_rejects_end_date_beyond_maximum_range(self):
        start = date(2026, 8, 31)
        too_far = start + timedelta(days=7 * fitness_service.FITNESS_RECURRENCE_MAX_WEEKS)

        with self.assertRaisesRegex(fitness_service.FitnessValidationError, "260 weeks"):
            fitness_service._recurrence_dates(
                start_date=start,
                weekdays=[1],
                end_date=too_far,
            )

    def test_recurring_series_rejects_far_end_date_before_persistence(self):
        start = date(2026, 8, 31)
        too_far = start + timedelta(days=7 * fitness_service.FITNESS_RECURRENCE_MAX_WEEKS)

        with patch(
            "backend.services.fitness_service.get_db_conn",
            side_effect=AssertionError("database should not be opened"),
        ):
            with self.assertRaisesRegex(fitness_service.FitnessValidationError, "260 weeks"):
                fitness_service.create_recurring_series(
                    user_id=USER_ID,
                    workout_template_id=TEMPLATE_ID,
                    start_date=start,
                    weekdays=[1],
                    end_date=too_far,
                )

    def test_recurrence_generator_rejects_contradictory_end_date(self):
        with self.assertRaisesRegex(fitness_service.FitnessValidationError, "conflicts"):
            fitness_service._recurrence_dates(
                start_date=date(2026, 8, 31),
                weekdays=[1, 3, 5],
                duration_weeks=5,
                end_date=date(2026, 10, 2),
            )

    def test_recurrence_generator_handles_month_and_year_boundaries(self):
        _end, _weekdays, _duration, dates = fitness_service._recurrence_dates(
            start_date=date(2026, 12, 28),
            weekdays=[1, 3, 5],
            duration_weeks=2,
        )

        self.assertEqual(
            dates,
            [
                date(2026, 12, 28),
                date(2026, 12, 30),
                date(2027, 1, 1),
                date(2027, 1, 4),
                date(2027, 1, 6),
                date(2027, 1, 8),
            ],
        )

    def test_recurring_series_idempotency_replay_returns_existing_series(self):
        canonical_end, normalized_weekdays, normalized_duration, _dates = fitness_service._recurrence_dates(
            start_date=date(2026, 8, 31),
            weekdays=[1, 3, 5],
            duration_weeks=5,
        )
        fingerprint = fitness_service._recurrence_fingerprint(
            workout_template_id=TEMPLATE_ID,
            start_date=date(2026, 8, 31),
            end_date=canonical_end,
            duration_weeks=normalized_duration,
            weekdays=normalized_weekdays,
        )
        connection, patches = self.patch_connection(
            [
                (TEMPLATE_COLUMNS, [RUNNING_TEMPLATE_ROW]),
                (["id", "request_fingerprint", "inserted"], [(SERIES_ID, fingerprint, False)]),
                (RECURRING_SERIES_COLUMNS, [RECURRING_SERIES_ROW]),
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
            ]
        )

        with patches:
            series = fitness_service.create_recurring_series(
                user_id=USER_ID,
                workout_template_id=TEMPLATE_ID,
                start_date=date(2026, 8, 31),
                weekdays=[1, 3, 5],
                duration_weeks=5,
                idempotency_key="retry-key",
            )

        self.assertEqual(series["id"], SERIES_ID)
        self.assertEqual(series["count"], 1)
        self.assertFalse(
            any(
                "INSERT INTO public.fitness_scheduled_workouts" in sql
                for sql, _ in connection.cursor_instance.executed
            )
        )

    def test_recurring_series_idempotency_key_conflict_rejected(self):
        connection, patches = self.patch_connection(
            [
                (TEMPLATE_COLUMNS, [RUNNING_TEMPLATE_ROW]),
                (
                    ["id", "request_fingerprint", "inserted"],
                    [(SERIES_ID, '{"different":true}', False)],
                ),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessConflictError, "Idempotency key"):
                fitness_service.create_recurring_series(
                    user_id=USER_ID,
                    workout_template_id=TEMPLATE_ID,
                    start_date=date(2026, 8, 31),
                    weekdays=[1, 3, 5],
                    duration_weeks=5,
                    idempotency_key="retry-key",
                )

        self.assertEqual(connection.rollbacks, 1)
        self.assertIn("ON CONFLICT", connection.cursor_instance.executed[1][0])

    def test_remove_completed_workout_is_rejected_without_delete(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [COMPLETED_RUNNING_ROW]),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessConflictError, "planned"):
                fitness_service.remove_scheduled_workout(
                    user_id=USER_ID,
                    scheduled_workout_id=SCHEDULED_ID,
                )

        self.assertFalse(
            any("DELETE FROM public.fitness_scheduled_workouts" in sql for sql, _ in connection.cursor_instance.executed)
        )

    def test_remove_safe_planned_workout_deletes_one_row(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                ([], []),
                ([], []),
                ([], []),
            ]
        )

        with patches:
            result = fitness_service.remove_scheduled_workout(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
            )

        self.assertEqual(result["removed_scheduled_workout_id"], SCHEDULED_ID)
        self.assertIn("DELETE FROM public.fitness_scheduled_workouts", connection.cursor_instance.executed[3][0])
        self.assertEqual(connection.cursor_instance.executed[3][1], (SCHEDULED_ID, USER_ID))

    def test_remove_running_result_linked_workout_is_rejected(self):
        planned_with_result = list(COMPLETED_RUNNING_ROW)
        planned_with_result[6] = "PLANNED"
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [tuple(planned_with_result)]),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessConflictError, "Running result"):
                fitness_service.remove_scheduled_workout(
                    user_id=USER_ID,
                    scheduled_workout_id=SCHEDULED_ID,
                )

    def test_remove_weightlifting_linked_workout_is_rejected(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                (["?column?"], [(1,)]),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessConflictError, "Weightlifting"):
                fitness_service.remove_scheduled_workout(
                    user_id=USER_ID,
                    scheduled_workout_id=SCHEDULED_ID,
                )

    def test_remove_one_recurring_occurrence_leaves_series_untouched(self):
        recurring = list(PLANNED_RUNNING_ROW)
        recurring[8] = SERIES_ID
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [tuple(recurring)]),
                ([], []),
                ([], []),
                ([], []),
            ]
        )

        with patches:
            fitness_service.remove_scheduled_workout(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
            )

        self.assertFalse(
            any(
                sql.lstrip().startswith("UPDATE public.fitness_recurring_schedule_series")
                for sql, _ in connection.cursor_instance.executed
            )
        )

    def test_remove_planned_replacement_target_is_rejected(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                ([], []),
                (["?column?"], [(1,)]),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessConflictError, "Undo the reschedule"):
                fitness_service.remove_scheduled_workout(
                    user_id=USER_ID,
                    scheduled_workout_id=SCHEDULED_ID,
                )

    def test_remove_remaining_series_preserves_history_lineage(self):
        connection, patches = self.patch_connection(
            [
                (RECURRING_SERIES_COLUMNS, [RECURRING_SERIES_ROW]),
                (["id"], [(SCHEDULED_ID,)]),
                ([], []),
                ([], []),
                (RECURRING_SERIES_COLUMNS, [RECURRING_SERIES_ROW]),
            ]
        )

        with patches:
            result = fitness_service.remove_remaining_recurring_workouts(
                user_id=USER_ID,
                series_id=SERIES_ID,
                from_date=date(2026, 8, 31),
            )

        selection_sql = connection.cursor_instance.executed[1][0]
        self.assertIn("scheduled.status = 'PLANNED'", selection_sql)
        self.assertIn("source.status = 'RESCHEDULED'", selection_sql)
        self.assertEqual(result["removed_scheduled_workout_ids"], [SCHEDULED_ID])

    def test_undo_reschedule_restores_original_and_deletes_replacement(self):
        replacement_id = "44444444-4444-4444-8444-444444444444"
        original = list(PLANNED_RUNNING_ROW)
        original[6] = "RESCHEDULED"
        original[7] = replacement_id
        restored = list(PLANNED_RUNNING_ROW)
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [tuple(original)]),
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                ([], []),
                ([], []),
                ([], []),
                (SCHEDULED_COLUMNS, [tuple(restored)]),
            ]
        )

        with patches:
            result = fitness_service.undo_reschedule(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
            )

        self.assertEqual(result["original"]["status"], "PLANNED")
        self.assertEqual(result["removed_replacement_scheduled_workout_id"], replacement_id)
        self.assertIn("replacement_scheduled_workout_id = NULL", connection.cursor_instance.executed[3][0])
        self.assertIn("DELETE FROM public.fitness_scheduled_workouts", connection.cursor_instance.executed[4][0])

    def test_training_calendar_summarizes_without_reschedule_double_counting(self):
        def scheduled(row):
            values = [fitness_service._serialize_value(value) for value in row]
            return fitness_service._with_running_result(dict(zip(SCHEDULED_COLUMNS, values)))

        completed = scheduled(COMPLETED_RUNNING_ROW)
        planned = scheduled(PLANNED_RUNNING_ROW)
        rescheduled_source = dict(planned)
        rescheduled_source["id"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        rescheduled_source["status"] = "RESCHEDULED"
        lifting = dict(planned)
        lifting["id"] = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        lifting["type"] = "LIFTING"
        lifting["status"] = "COMPLETED"

        with patch(
            "backend.services.fitness_service.list_scheduled_workouts",
            return_value=[completed, planned, rescheduled_source, lifting],
        ), patch(
            "backend.services.fitness_service.current_fitness_date",
            return_value=date(2026, 8, 21),
        ):
            calendar = fitness_service.training_calendar(
                user_id=USER_ID,
                start_date=date(2026, 8, 17),
                end_date=date(2026, 8, 23),
            )

        summary = calendar["weeks"][0]["summary"]
        self.assertEqual(summary["planned_running_miles"], 10.0)
        self.assertEqual(summary["actual_running_miles"], 5.1)
        self.assertEqual(summary["longest_planned_run_miles"], 5.0)
        self.assertEqual(summary["completed_lifting_sessions"], 1)

    def test_remove_unstarted_plan_instance_uses_locked_scheduled_state(self):
        connection, patches = self.patch_connection(
            [
                (["id", "status", "stopped_at"], [(PLAN_INSTANCE_ID, "ACTIVE", None)]),
                (
                    [
                        "id",
                        "status",
                        "has_running_result",
                        "has_weightlifting_entries",
                        "is_reschedule_replacement",
                    ],
                    [(SCHEDULED_ID, "PLANNED", False, False, False)],
                ),
                ([], []),
                ([], []),
            ]
        )

        with patches:
            result = fitness_service.remove_unstarted_plan_instance(
                user_id=USER_ID,
                instance_id=PLAN_INSTANCE_ID,
            )

        self.assertEqual(result["removed_plan_instance_id"], PLAN_INSTANCE_ID)
        self.assertIn("FOR UPDATE", connection.cursor_instance.executed[0][0])
        self.assertIn("FOR UPDATE OF scheduled", connection.cursor_instance.executed[1][0])
        self.assertIn("DELETE FROM public.fitness_training_plan_instances", connection.cursor_instance.executed[3][0])

    def test_remove_unstarted_plan_instance_rejects_locked_completed_workout(self):
        connection, patches = self.patch_connection(
            [
                (["id", "status", "stopped_at"], [(PLAN_INSTANCE_ID, "ACTIVE", None)]),
                (
                    [
                        "id",
                        "status",
                        "has_running_result",
                        "has_weightlifting_entries",
                        "is_reschedule_replacement",
                    ],
                    [(SCHEDULED_ID, "COMPLETED", True, False, False)],
                ),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessConflictError, "history"):
                fitness_service.remove_unstarted_plan_instance(
                    user_id=USER_ID,
                    instance_id=PLAN_INSTANCE_ID,
                )

        self.assertFalse(
            any("DELETE FROM public.fitness_training_plan_instances" in sql for sql, _ in connection.cursor_instance.executed)
        )

    def test_remove_unstarted_completed_plan_instance_with_planned_rows_is_rejected(self):
        connection, patches = self.patch_connection(
            [
                (["id", "status", "stopped_at"], [(PLAN_INSTANCE_ID, "COMPLETED", None)]),
                (
                    [
                        "id",
                        "status",
                        "has_running_result",
                        "has_weightlifting_entries",
                        "is_reschedule_replacement",
                    ],
                    [(SCHEDULED_ID, "PLANNED", False, False, False)],
                ),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessConflictError, "Completed"):
                fitness_service.remove_unstarted_plan_instance(
                    user_id=USER_ID,
                    instance_id=PLAN_INSTANCE_ID,
                )

        executed_sql = "\n".join(sql for sql, _ in connection.cursor_instance.executed)
        self.assertNotIn("DELETE FROM public.fitness_scheduled_workouts", executed_sql)
        self.assertNotIn("DELETE FROM public.fitness_training_plan_instances", executed_sql)

    def test_remove_unstarted_completed_plan_instance_with_zero_rows_is_rejected(self):
        connection, patches = self.patch_connection(
            [
                (["id", "status", "stopped_at"], [(PLAN_INSTANCE_ID, "COMPLETED", None)]),
                (
                    [
                        "id",
                        "status",
                        "has_running_result",
                        "has_weightlifting_entries",
                        "is_reschedule_replacement",
                    ],
                    [],
                ),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessConflictError, "Completed"):
                fitness_service.remove_unstarted_plan_instance(
                    user_id=USER_ID,
                    instance_id=PLAN_INSTANCE_ID,
                )

        executed_sql = "\n".join(sql for sql, _ in connection.cursor_instance.executed)
        self.assertNotIn("DELETE FROM public.fitness_scheduled_workouts", executed_sql)
        self.assertNotIn("DELETE FROM public.fitness_training_plan_instances", executed_sql)

    def test_remove_remaining_plan_workouts_preserves_history_and_marks_active_stopped(self):
        connection, patches = self.patch_connection(
            [
                (["id", "status", "stopped_at"], [(PLAN_INSTANCE_ID, "ACTIVE", None)]),
                (
                    [
                        "id",
                        "status",
                        "has_running_result",
                        "has_weightlifting_entries",
                        "is_reschedule_replacement",
                    ],
                    [(SCHEDULED_ID, "PLANNED", False, False, False)],
                ),
                ([], []),
                ([], []),
                (PLAN_INSTANCE_COLUMNS, [STOPPED_PLAN_INSTANCE_ROW]),
                (SCHEDULED_COLUMNS, [COMPLETED_RUNNING_ROW]),
            ]
        )

        with patches:
            result = fitness_service.remove_remaining_plan_workouts(
                user_id=USER_ID,
                instance_id=PLAN_INSTANCE_ID,
                from_date=date(2026, 8, 21),
            )

        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(result["planning_status"], "STOPPED")
        self.assertIn("SET stopped_at = COALESCE", connection.cursor_instance.executed[3][0])

    def test_remove_remaining_plan_workouts_does_not_rewrite_completed_lifecycle(self):
        connection, patches = self.patch_connection(
            [
                (["id", "status", "stopped_at"], [(PLAN_INSTANCE_ID, "COMPLETED", None)]),
                (
                    [
                        "id",
                        "status",
                        "has_running_result",
                        "has_weightlifting_entries",
                        "is_reschedule_replacement",
                    ],
                    [],
                ),
                (PLAN_INSTANCE_COLUMNS, [COMPLETED_PLAN_INSTANCE_ROW]),
                (SCHEDULED_COLUMNS, [COMPLETED_RUNNING_ROW]),
            ]
        )

        with patches:
            result = fitness_service.remove_remaining_plan_workouts(
                user_id=USER_ID,
                instance_id=PLAN_INSTANCE_ID,
                from_date=date(2026, 8, 21),
            )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["planning_status"], "ACTIVE")
        self.assertFalse(
            any("UPDATE public.fitness_training_plan_instances" in sql for sql, _ in connection.cursor_instance.executed)
        )

    def test_new_correction_operations_filter_by_user(self):
        cases = [
            (
                fitness_service.get_recurring_series,
                {"user_id": USER_ID, "series_id": SERIES_ID},
                "Recurring series not found",
            ),
            (
                fitness_service.remove_remaining_recurring_workouts,
                {"user_id": USER_ID, "series_id": SERIES_ID, "from_date": date(2026, 8, 31)},
                "Recurring series not found",
            ),
            (
                fitness_service.remove_unstarted_plan_instance,
                {"user_id": USER_ID, "instance_id": PLAN_INSTANCE_ID},
                "Training plan instance not found",
            ),
        ]
        for func, kwargs, message in cases:
            with self.subTest(func=func.__name__):
                connection, patches = self.patch_connection([([], [])])
                with patches:
                    with self.assertRaisesRegex(fitness_service.FitnessNotFoundError, message):
                        func(**kwargs)
                self.assertIn("user_id = %s", connection.cursor_instance.executed[0][0])

    def test_training_calendar_returns_exact_requested_week_counts(self):
        with patch(
            "backend.services.fitness_service.list_scheduled_workouts",
            return_value=[],
        ), patch(
            "backend.services.fitness_service.current_fitness_date",
            return_value=date(2026, 8, 25),
        ):
            for week_count in [5, 6, 8, 10, 12]:
                with self.subTest(week_count=week_count):
                    start = date(2026, 8, 24)
                    calendar = fitness_service.training_calendar(
                        user_id=USER_ID,
                        start_date=start,
                        end_date=start + timedelta(days=week_count * 7 - 1),
                    )
                    self.assertEqual(len(calendar["weeks"]), week_count)

    def test_schedule_history_exposes_running_completion_result(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [COMPLETED_RUNNING_ROW]),
            ]
        )

        with patches:
            history = fitness_service.list_workout_history(
                user_id=USER_ID,
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 31),
            )

        self.assertIn("scheduled.status <> 'PLANNED'", connection.cursor_instance.executed[0][0])
        self.assertEqual(history[0]["running_result"]["planned_distance_miles"], 5.0)
        self.assertEqual(history[0]["running_result"]["duration_seconds"], 1860)

    def test_template_completed_workouts_query_uses_template_id(self):
        connection, patches = self.patch_connection(
            [
                (TEMPLATE_COLUMNS, [RUNNING_TEMPLATE_ROW]),
                (SCHEDULED_COLUMNS, [COMPLETED_RUNNING_ROW]),
            ]
        )

        with patches:
            history = fitness_service.list_completed_workouts_for_template(
                user_id=USER_ID,
                template_id=TEMPLATE_ID,
            )

        sql, params = connection.cursor_instance.executed[1]
        self.assertIn("scheduled.workout_template_id = %s", sql)
        self.assertIn("scheduled.status = 'COMPLETED'", sql)
        self.assertNotIn("workout_name", sql.split("WHERE", 1)[1])
        self.assertEqual(params, (USER_ID, TEMPLATE_ID))
        self.assertEqual(history[0]["id"], SCHEDULED_ID)
        self.assertEqual(history[0]["running_result"]["duration_seconds"], 1860)

    def test_template_completed_workouts_excludes_identically_named_other_templates(self):
        second_completed = list(COMPLETED_RUNNING_ROW)
        second_completed[0] = SECOND_SCHEDULED_ID
        second_completed[2] = SECOND_TEMPLATE_ID
        second_completed[10] = "Long Run"
        connection, patches = self.patch_connection(
            [
                (TEMPLATE_COLUMNS, [RUNNING_TEMPLATE_ROW]),
                (SCHEDULED_COLUMNS, [COMPLETED_RUNNING_ROW]),
            ]
        )

        with patches:
            history = fitness_service.list_completed_workouts_for_template(
                user_id=USER_ID,
                template_id=TEMPLATE_ID,
            )

        sql, params = connection.cursor_instance.executed[1]
        self.assertIn("scheduled.workout_template_id = %s", sql)
        self.assertEqual(params[1], TEMPLATE_ID)
        self.assertEqual([workout["id"] for workout in history], [SCHEDULED_ID])
        self.assertNotIn(SECOND_SCHEDULED_ID, [workout["id"] for workout in history])

    def test_template_completed_workouts_handles_completed_without_running_result(self):
        completed_without_result = list(COMPLETED_RUNNING_ROW)
        for index in range(15, 21):
            completed_without_result[index] = None
        connection, patches = self.patch_connection(
            [
                (TEMPLATE_COLUMNS, [RUNNING_TEMPLATE_ROW]),
                (SCHEDULED_COLUMNS, [tuple(completed_without_result)]),
            ]
        )

        with patches:
            history = fitness_service.list_completed_workouts_for_template(
                user_id=USER_ID,
                template_id=TEMPLATE_ID,
            )

        self.assertEqual(history[0]["status"], "COMPLETED")
        self.assertIsNone(history[0]["running_result"])

    def test_running_result_exposes_persisted_garmin_metrics(self):
        workout = dict(zip(SCHEDULED_COLUMNS, COMPLETED_RUNNING_ROW))
        workout.update(
            {
                "external_provider": "GARMIN",
                "external_activity_id": "garmin-1",
                "external_activity_uuid": "uuid-1",
                "external_activity_name": "Morning Run",
                "moving_duration_seconds": 1799,
                "average_speed_meters_per_second": Decimal("2.777778"),
                "average_hr": Decimal("150"),
                "max_hr": Decimal("180"),
                "training_load": Decimal("52.5"),
                "aerobic_training_effect": Decimal("3.2"),
                "anaerobic_training_effect": Decimal("0.4"),
                "training_effect_label": "MAINTAINING",
                "vo2_max": Decimal("45"),
                "hr_zone_1_seconds": 11,
                "hr_zone_2_seconds": 20,
                "hr_zone_3_seconds": 31,
                "hr_zone_4_seconds": 40,
                "hr_zone_5_seconds": 51,
                "average_cadence_spm": Decimal("172"),
                "average_power_watts": Decimal("245"),
                "average_stride_length_meters": Decimal("0.8809"),
                "elevation_gain_meters": Decimal("25.2"),
                "elevation_loss_meters": Decimal("24.7"),
                "calories": Decimal("410"),
                "steps": 6400,
            }
        )

        hydrated = fitness_service._with_running_result(workout)

        self.assertEqual(hydrated["running_result"]["external_provider"], "GARMIN")
        self.assertEqual(hydrated["running_result"]["external_activity_id"], "garmin-1")
        self.assertEqual(hydrated["running_result"]["average_stride_length_meters"], Decimal("0.8809"))

    def historical_effort_row(
        self,
        scheduled_id=SCHEDULED_ID,
        scheduled_date=date(2026, 8, 29),
        completed_distance_miles=Decimal("1.70"),
        duration_seconds=1502,
        total_efforts=3,
        average_hr=Decimal("149"),
    ):
        return (
            scheduled_id,
            scheduled_date,
            "COMPLETED",
            completed_distance_miles,
            duration_seconds,
            1490,
            average_hr,
            Decimal("172"),
            Decimal("51.5"),
            Decimal("3.1"),
            Decimal("0.2"),
            Decimal("44"),
            10,
            20,
            30,
            40,
            50,
            Decimal("171.5"),
            Decimal("240"),
            Decimal("0.8809"),
            Decimal("25.2"),
            total_efforts,
        )

    def test_historical_efforts_enforces_reference_ownership_before_query(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, []),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessNotFoundError, "Scheduled workout not found"):
                fitness_service.get_historical_efforts(
                    user_id=USER_ID,
                    scheduled_workout_id=SCHEDULED_ID,
                )

        self.assertEqual(len(connection.cursor_instance.executed), 1)
        self.assertEqual(connection.cursor_instance.executed[0][1], (SCHEDULED_ID, USER_ID))

    def test_historical_efforts_rejects_unsupported_workout_type(self):
        lifting_row = list(PLANNED_RUNNING_ROW)
        lifting_row[10] = "Full Body"
        lifting_row[11] = "LIFTING"
        lifting_row[15] = None
        lifting_row[16] = None
        lifting_row[17] = None
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [tuple(lifting_row)]),
            ]
        )

        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessValidationError, "not supported for LIFTING"):
                fitness_service.get_historical_efforts(
                    user_id=USER_ID,
                    scheduled_workout_id=SCHEDULED_ID,
                )

        self.assertEqual(len(connection.cursor_instance.executed), 1)

    def test_historical_efforts_running_cohort_query_uses_template_and_snapshot(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                (HISTORICAL_EFFORT_COLUMNS, [self.historical_effort_row()]),
            ]
        )

        with patches, patch(
            "backend.services.fitness_service.garmin_activity_provider.resolve_running_activities"
        ) as resolve_running, patch(
            "backend.services.fitness_service.garmin_activity_provider.find_running_activity"
        ) as find_running:
            result = fitness_service.get_historical_efforts(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
            )

        sql, params = connection.cursor_instance.executed[1]
        self.assertIn("JOIN public.fitness_running_workout_results AS result", sql)
        self.assertIn("scheduled.status = 'COMPLETED'", sql)
        self.assertIn("template.workout_type = 'RUNNING'", sql)
        self.assertIn("scheduled.workout_template_id = %s", sql)
        self.assertIn("scheduled.planned_distance_miles = %s", sql)
        self.assertIn("ORDER BY scheduled.scheduled_date DESC, scheduled.updated_at DESC, scheduled.id DESC", sql)
        self.assertEqual(params, (USER_ID, TEMPLATE_ID, 5.0, 5))
        resolve_running.assert_not_called()
        find_running.assert_not_called()
        self.assertEqual(result["workout"]["workout_template_id"], TEMPLATE_ID)
        self.assertEqual(result["workout"]["workout_type"], "RUNNING")
        self.assertEqual(result["workout"]["planned_distance_miles"], 5.0)

    def test_historical_efforts_returns_total_count_before_limit_and_metrics(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                (
                    HISTORICAL_EFFORT_COLUMNS,
                    [
                        self.historical_effort_row(
                            scheduled_id="33333333-3333-4333-8333-333333333331",
                            scheduled_date=date(2026, 8, 29),
                            total_efforts=17,
                        ),
                        self.historical_effort_row(
                            scheduled_id="33333333-3333-4333-8333-333333333330",
                            scheduled_date=date(2026, 8, 27),
                            completed_distance_miles=Decimal("2.00"),
                            duration_seconds=1800,
                            total_efforts=17,
                            average_hr=None,
                        ),
                    ],
                ),
            ]
        )

        with patches:
            result = fitness_service.get_historical_efforts(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
                limit="5",
            )

        self.assertEqual(result["total_efforts"], 17)
        self.assertEqual(len(result["efforts"]), 2)
        self.assertEqual(result["efforts"][0]["scheduled_date"], "2026-08-29")
        values = result["efforts"][0]["values"]
        self.assertAlmostEqual(values["pace_seconds_per_mile"], 883.5294117647059)
        self.assertEqual(values["average_hr"], 149.0)
        self.assertEqual(result["efforts"][1]["values"]["average_hr"], None)
        metrics = {metric["key"]: metric for metric in result["metrics"]}
        self.assertEqual(metrics["pace_seconds_per_mile"]["format"], "PACE_PER_MILE")
        self.assertEqual(metrics["pace_seconds_per_mile"]["lower_is_better"], True)
        self.assertIsNone(metrics["average_hr"]["lower_is_better"])
        self.assertIsNone(metrics["training_load"]["lower_is_better"])
        self.assertIsNone(metrics["average_power_watts"]["lower_is_better"])

    def test_historical_efforts_includes_manual_runs_and_nulls_zero_distance_pace(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                (
                    HISTORICAL_EFFORT_COLUMNS,
                    [
                        self.historical_effort_row(
                            scheduled_id="33333333-3333-4333-8333-333333333331",
                            completed_distance_miles=Decimal("0.00"),
                            duration_seconds=1200,
                            average_hr=None,
                        ),
                        self.historical_effort_row(
                            scheduled_id="33333333-3333-4333-8333-333333333330",
                            completed_distance_miles=None,
                            duration_seconds=1200,
                            average_hr=None,
                        ),
                    ],
                ),
            ]
        )

        with patches:
            result = fitness_service.get_historical_efforts(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
                limit="all",
            )

        sql, params = connection.cursor_instance.executed[1]
        self.assertNotIn("LIMIT %s", sql)
        self.assertEqual(params, (USER_ID, TEMPLATE_ID, 5.0))
        self.assertEqual(result["efforts"][0]["values"]["average_hr"], None)
        self.assertIsNone(result["efforts"][0]["values"]["pace_seconds_per_mile"])
        self.assertIsNone(result["efforts"][1]["values"]["pace_seconds_per_mile"])

    def test_historical_effort_limits_are_validated(self):
        self.assertEqual(fitness_service._normalize_historical_effort_limit(None), 5)
        self.assertEqual(fitness_service._normalize_historical_effort_limit("10"), 10)
        self.assertIsNone(fitness_service._normalize_historical_effort_limit("all"))
        with self.assertRaisesRegex(fitness_service.FitnessValidationError, "limit must be one of"):
            fitness_service._normalize_historical_effort_limit("0")

    def test_historical_efforts_limit_ten_is_applied(self):
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                (HISTORICAL_EFFORT_COLUMNS, []),
            ]
        )

        with patches:
            result = fitness_service.get_historical_efforts(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
                limit="10",
            )

        self.assertEqual(result["total_efforts"], 0)
        self.assertEqual(connection.cursor_instance.executed[1][1], (USER_ID, TEMPLATE_ID, 5.0, 10))

    def test_skip_planned_workout_and_reject_repeated_transition(self):
        skipped_row = list(PLANNED_RUNNING_ROW)
        skipped_row[6] = "SKIPPED"
        completed_row = list(PLANNED_RUNNING_ROW)
        completed_row[6] = "COMPLETED"
        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
                ([], []),
                (SCHEDULED_COLUMNS, [tuple(skipped_row)]),
            ]
        )

        with patches:
            skipped = fitness_service.skip_scheduled_workout(
                user_id=USER_ID,
                scheduled_workout_id=SCHEDULED_ID,
            )

        self.assertEqual(skipped["status"], "SKIPPED")
        self.assertIn("FOR UPDATE", connection.cursor_instance.executed[0][0])
        self.assertIn("AND status = 'PLANNED'", connection.cursor_instance.executed[1][0])

        connection, patches = self.patch_connection(
            [
                (SCHEDULED_COLUMNS, [tuple(completed_row)]),
            ]
        )
        with patches:
            with self.assertRaisesRegex(fitness_service.FitnessConflictError, "planned"):
                fitness_service.skip_scheduled_workout(
                    user_id=USER_ID,
                    scheduled_workout_id=SCHEDULED_ID,
                )

    def test_cross_user_access_is_rejected_by_owner_filters(self):
        cases = [
            (
                fitness_service.get_workout_template,
                {"user_id": USER_ID, "template_id": TEMPLATE_ID},
                "Workout template not found",
            ),
            (
                fitness_service.get_plan_template,
                {"user_id": USER_ID, "plan_template_id": PLAN_TEMPLATE_ID},
                "Training plan template not found",
            ),
            (
                fitness_service.get_plan_instance,
                {"user_id": USER_ID, "instance_id": PLAN_INSTANCE_ID},
                "Training plan instance not found",
            ),
            (
                fitness_service.get_scheduled_workout,
                {"user_id": USER_ID, "scheduled_workout_id": SCHEDULED_ID},
                "Scheduled workout not found",
            ),
        ]
        for func, kwargs, message in cases:
            with self.subTest(func=func.__name__):
                connection, patches = self.patch_connection([([], [])])
                with patches:
                    with self.assertRaisesRegex(fitness_service.FitnessNotFoundError, message):
                        func(**kwargs)
                self.assertIn("user_id = %s", connection.cursor_instance.executed[0][0])

    def test_cross_user_scheduled_mutations_are_rejected_by_owner_filters(self):
        cases = [
            (
                fitness_service.complete_scheduled_workout,
                {
                    "user_id": USER_ID,
                    "scheduled_workout_id": SCHEDULED_ID,
                    "running": {
                        "completed_distance_miles": Decimal("5.1"),
                        "duration_seconds": 1860,
                    },
                },
            ),
            (
                fitness_service.skip_scheduled_workout,
                {"user_id": USER_ID, "scheduled_workout_id": SCHEDULED_ID},
            ),
            (
                fitness_service.reschedule_scheduled_workout,
                {
                    "user_id": USER_ID,
                    "scheduled_workout_id": SCHEDULED_ID,
                    "scheduled_date": date(2026, 8, 22),
                },
            ),
        ]
        for func, kwargs in cases:
            with self.subTest(func=func.__name__):
                connection, patches = self.patch_connection([([], [])])
                with patches:
                    with self.assertRaisesRegex(
                        fitness_service.FitnessNotFoundError,
                        "Scheduled workout not found",
                    ):
                        func(**kwargs)
                self.assertIn("scheduled.user_id = %s", connection.cursor_instance.executed[0][0])
                self.assertIn("FOR UPDATE", connection.cursor_instance.executed[0][0])
                self.assertEqual(connection.rollbacks, 1)

    def test_cross_user_workout_template_mutations_are_rejected_without_commit(self):
        cases = [
            (
                fitness_service.update_workout_template,
                {"user_id": USER_ID, "template_id": TEMPLATE_ID, "name": "Other"},
                [0],
            ),
            (
                fitness_service.set_workout_template_active,
                {"user_id": USER_ID, "template_id": TEMPLATE_ID, "active": False},
                [0],
            ),
        ]
        for func, kwargs, rowcounts in cases:
            with self.subTest(func=func.__name__):
                connection, patches = self.patch_connection([([], [])], rowcounts=rowcounts)
                with patches:
                    with self.assertRaisesRegex(
                        fitness_service.FitnessNotFoundError,
                        "Workout template not found",
                    ):
                        func(**kwargs)

                sql, params = connection.cursor_instance.executed[0]
                self.assertIn("WHERE id = %s", sql)
                self.assertIn("AND user_id = %s", sql)
                self.assertEqual(params[-2:], (TEMPLATE_ID, USER_ID))
                self.assertEqual(connection.commits, 0)
                self.assertEqual(connection.rollbacks, 1)

    def test_cross_user_lifting_membership_replacement_is_rejected_without_commit(self):
        connection, patches = self.patch_connection([([], [])])

        with patches:
            with self.assertRaisesRegex(
                fitness_service.FitnessNotFoundError,
                "Workout template not found",
            ):
                fitness_service.replace_lifting_template_exercises(
                    user_id=USER_ID,
                    template_id=TEMPLATE_ID,
                    exercises=[
                        {
                            "exercise_id": EXERCISE_ID,
                            "display_order": 1,
                        }
                    ],
                )

        lookup_sql, lookup_params = connection.cursor_instance.executed[0]
        self.assertIn("template.user_id = %s", lookup_sql)
        self.assertEqual(lookup_params, (TEMPLATE_ID, USER_ID))
        self.assertFalse(
            any("DELETE FROM public.fitness_lifting_template_exercises" in sql for sql, _ in connection.cursor_instance.executed)
        )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_cross_user_plan_template_mutations_are_rejected_without_commit(self):
        cases = [
            (
                fitness_service.update_plan_template,
                {"user_id": USER_ID, "plan_template_id": PLAN_TEMPLATE_ID, "name": "Other"},
                [0],
            ),
            (
                fitness_service.set_plan_template_active,
                {"user_id": USER_ID, "plan_template_id": PLAN_TEMPLATE_ID, "active": False},
                [0],
            ),
        ]
        for func, kwargs, rowcounts in cases:
            with self.subTest(func=func.__name__):
                connection, patches = self.patch_connection([([], [])], rowcounts=rowcounts)
                with patches:
                    with self.assertRaisesRegex(
                        fitness_service.FitnessNotFoundError,
                        "Training plan template not found",
                    ):
                        func(**kwargs)

                sql, params = connection.cursor_instance.executed[0]
                self.assertIn("WHERE id = %s", sql)
                self.assertIn("AND user_id = %s", sql)
                self.assertEqual(params[-2:], (PLAN_TEMPLATE_ID, USER_ID))
                self.assertEqual(connection.commits, 0)
                self.assertEqual(connection.rollbacks, 1)

    def test_cross_user_plan_item_replacement_is_rejected_without_commit(self):
        connection, patches = self.patch_connection([([], [])])

        with patches:
            with self.assertRaisesRegex(
                fitness_service.FitnessNotFoundError,
                "Training plan template not found",
            ):
                fitness_service.replace_plan_template_items(
                    user_id=USER_ID,
                    plan_template_id=PLAN_TEMPLATE_ID,
                    items=[
                        {
                            "workout_template_id": TEMPLATE_ID,
                            "day_offset": 0,
                            "display_order": 1,
                        }
                    ],
                )

        lookup_sql, lookup_params = connection.cursor_instance.executed[0]
        self.assertIn("WHERE id = %s", lookup_sql)
        self.assertIn("AND user_id = %s", lookup_sql)
        self.assertEqual(lookup_params, (PLAN_TEMPLATE_ID, USER_ID))
        self.assertFalse(
            any("DELETE FROM public.fitness_training_plan_template_items" in sql for sql, _ in connection.cursor_instance.executed)
        )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_cross_user_plan_instance_mutations_are_rejected_without_commit(self):
        cases = [
            (
                fitness_service.instantiate_plan_template,
                {
                    "user_id": USER_ID,
                    "plan_template_id": PLAN_TEMPLATE_ID,
                    "start_date": date(2026, 8, 21),
                },
                "Training plan template not found",
                "AND user_id = %s",
            ),
            (
                fitness_service.complete_plan_instance,
                {"user_id": USER_ID, "instance_id": PLAN_INSTANCE_ID},
                "Training plan instance not found",
                "AND instance.user_id = %s",
            ),
        ]
        for func, kwargs, message, owner_filter in cases:
            with self.subTest(func=func.__name__):
                connection, patches = self.patch_connection([([], [])])
                with patches:
                    with self.assertRaisesRegex(
                        fitness_service.FitnessNotFoundError,
                        message,
                    ):
                        func(**kwargs)

                self.assertIn(owner_filter, connection.cursor_instance.executed[0][0])
                self.assertEqual(connection.commits, 0)
                self.assertEqual(connection.rollbacks, 1)

    def test_get_plan_instance_includes_generated_scheduled_workouts(self):
        connection, patches = self.patch_connection(
            [
                (PLAN_INSTANCE_COLUMNS, [PLAN_INSTANCE_ROW]),
                (SCHEDULED_COLUMNS, [PLANNED_RUNNING_ROW]),
            ]
        )

        with patches:
            instance = fitness_service.get_plan_instance(
                user_id=USER_ID,
                instance_id=PLAN_INSTANCE_ID,
            )

        self.assertEqual(instance["id"], PLAN_INSTANCE_ID)
        self.assertEqual(len(instance["scheduled_workouts"]), 1)
        self.assertEqual(instance["scheduled_workouts"][0]["planned_distance_miles"], 5.0)

    def test_get_plan_instance_uses_exact_instance_id_for_workouts(self):
        same_named_other_instance_id = "44444444-4444-4444-8444-444444444444"
        same_named_other_instance = list(COMPLETED_RUNNING_ROW)
        same_named_other_instance[3] = same_named_other_instance_id
        same_named_other_instance[10] = "Long Run"
        included = list(COMPLETED_RUNNING_ROW)
        included[3] = PLAN_INSTANCE_ID
        connection, patches = self.patch_connection(
            [
                (PLAN_INSTANCE_COLUMNS, [PLAN_INSTANCE_ROW]),
                (SCHEDULED_COLUMNS, [tuple(included)]),
            ]
        )

        with patches:
            instance = fitness_service.get_plan_instance(
                user_id=USER_ID,
                instance_id=PLAN_INSTANCE_ID,
            )

        sql, params = connection.cursor_instance.executed[1]
        self.assertIn("scheduled.plan_instance_id = %s", sql)
        self.assertNotIn("workout_name", sql.split("WHERE", 1)[1])
        self.assertEqual(params, (USER_ID, PLAN_INSTANCE_ID))
        self.assertEqual([workout["id"] for workout in instance["scheduled_workouts"]], [SCHEDULED_ID])
        self.assertNotIn(
            same_named_other_instance_id,
            [workout["plan_instance_id"] for workout in instance["scheduled_workouts"]],
        )

    def test_complete_plan_instance_finishes_active_instance(self):
        connection, patches = self.patch_connection(
            [
                (PLAN_INSTANCE_COLUMNS, [PLAN_INSTANCE_ROW]),
                ([], []),
                (PLAN_INSTANCE_COLUMNS, [COMPLETED_PLAN_INSTANCE_ROW]),
                (SCHEDULED_COLUMNS, []),
            ]
        )

        with patches:
            instance = fitness_service.complete_plan_instance(
                user_id=USER_ID,
                instance_id=PLAN_INSTANCE_ID,
            )

        self.assertIn("AND status = 'ACTIVE'", connection.cursor_instance.executed[1][0])
        self.assertEqual(instance["status"], "COMPLETED")

    def test_lifting_template_rejects_another_users_weightlifting_exercise(self):
        connection, patches = self.patch_connection(
            [
                (["id"], [(TEMPLATE_ID,)]),
                (TEMPLATE_COLUMNS, [LIFTING_TEMPLATE_ROW]),
                (["id"], []),
            ]
        )

        with patches:
            with self.assertRaisesRegex(
                fitness_service.FitnessValidationError,
                "belong to the user",
            ):
                fitness_service.create_workout_template(
                    user_id=USER_ID,
                    name="Full Body",
                    workout_type="LIFTING",
                    exercises=[
                        {
                            "exercise_id": EXERCISE_ID,
                            "display_order": 1,
                        }
                    ],
                )

        owner_sql, owner_params = connection.cursor_instance.executed[2]
        self.assertIn("WHERE user_id = %s", owner_sql)
        self.assertEqual(owner_params[0], USER_ID)


if __name__ == "__main__":
    unittest.main()

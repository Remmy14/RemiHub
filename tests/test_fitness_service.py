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
        raise RuntimeError("database access is disabled during Fitness service tests")

    def putconn(self, _connection):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool

from backend.services import fitness_service


USER_ID = "11111111-1111-4111-8111-111111111111"
SCHEDULED_ID = "33333333-3333-4333-8333-333333333333"
TEMPLATE_ID = "22222222-2222-4222-8222-222222222222"
EXERCISE_ID = "77777777-7777-4777-8777-777777777777"
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
PLAN_TEMPLATE_ID = "66666666-6666-4666-8666-666666666666"
SECOND_TEMPLATE_ID = "88888888-8888-4888-8888-888888888888"
SECOND_SCHEDULED_ID = "99999999-9999-4999-8999-999999999999"
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
    "planned_distance_miles",
    "workout_name",
    "type",
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
    Decimal("5.00"),
    "Long Run",
    "RUNNING",
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
COMPLETED_RUNNING_ROW[11] = Decimal("5.00")
COMPLETED_RUNNING_ROW[12] = Decimal("5.10")
COMPLETED_RUNNING_ROW[13] = 1860
COMPLETED_RUNNING_ROW[14] = "felt good"
COMPLETED_RUNNING_ROW[15] = NOW
COMPLETED_RUNNING_ROW[16] = NOW
COMPLETED_RUNNING_ROW = tuple(COMPLETED_RUNNING_ROW)

PLAN_INSTANCE_COLUMNS = [
    "id",
    "user_id",
    "plan_template_id",
    "plan_template_name",
    "start_date",
    "status",
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
    NOW,
    NOW,
)
COMPLETED_PLAN_INSTANCE_ROW = list(PLAN_INSTANCE_ROW)
COMPLETED_PLAN_INSTANCE_ROW[5] = "COMPLETED"
COMPLETED_PLAN_INSTANCE_ROW = tuple(COMPLETED_PLAN_INSTANCE_ROW)


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
            params[3]
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
        second_row[10] = "LIFTING"
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
        self.assertEqual(insert_params[5], 5.0)
        self.assertIn("FOR UPDATE", connection.cursor_instance.executed[0][0])
        self.assertIn("AND status = 'PLANNED'", connection.cursor_instance.executed[2][0])
        self.assertEqual(result["replacement"]["planned_distance_miles"], 5.0)

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

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from psycopg2 import pool


class OfflineThreadedConnectionPool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise RuntimeError("database access is disabled during Mead tests")

    def putconn(self, _connection):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool

from backend.services import mead_service


USER_ID = "11111111-1111-4111-8111-111111111111"
BATCH_ID = "22222222-2222-4222-8222-222222222222"
TASK_ID = "33333333-3333-4333-8333-333333333333"
EVENT_ID = "44444444-4444-4444-8444-444444444444"
NOW = datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc)

TASK_COLUMNS = [
    "id",
    "batch_id",
    "task_type",
    "title",
    "description",
    "due_at",
    "status",
    "completed_at",
    "notified_at",
    "notified_due_at",
    "source",
    "source_key",
    "metadata",
    "created_at",
    "updated_at",
]
BATCH_COLUMNS = [
    "id",
    "user_id",
    "name",
    "start_at",
    "stage",
    "volume",
    "volume_unit",
    "original_gravity",
    "target_final_gravity",
    "notes",
    "recipe_notes",
    "tosna_enabled",
    "tosna_nutrient_name",
    "tosna_total_amount",
    "tosna_unit",
    "created_at",
    "updated_at",
]
EVENT_COLUMNS = [
    "id",
    "batch_id",
    "event_at",
    "event_type",
    "gravity",
    "notes",
    "metadata",
    "created_at",
    "updated_at",
]
RECIPE_COLUMNS = [
    "id",
    "batch_id",
    "name",
    "amount",
    "unit",
    "notes",
    "display_order",
    "created_at",
    "updated_at",
]


def task_row(*, status="pending", completed_at=None, due_at=None, metadata=None):
    return (
        TASK_ID,
        BATCH_ID,
        "add_nutrients",
        "Add TOSNA Nutrients - Blackberry Mead",
        "Dose 2 of 4: add 1.2 g Fermaid O.",
        due_at or NOW,
        status,
        completed_at,
        None,
        None,
        "tosna",
        "dose_2_of_4",
        metadata or {"dose_number": 2, "per_dose_amount": "1.2"},
        NOW,
        NOW,
    )


def batch_row(*, stage="primary", tosna_enabled=True, start_at=NOW):
    return (
        BATCH_ID,
        USER_ID,
        "Blackberry Mead",
        start_at,
        stage,
        Decimal("1.0"),
        "gal",
        Decimal("1.112"),
        Decimal("1.010"),
        "notes",
        "recipe notes",
        tosna_enabled,
        "Fermaid O",
        Decimal("4.8"),
        "g",
        NOW,
        NOW,
    )


class FakeCursor:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.executed = []
        self.description = []
        self._rows = []
        self.rowcount = 1

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
    def __init__(self, responses=None):
        self.cursor_instance = FakeCursor(responses)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class MeadCalculationTests(unittest.TestCase):
    def test_current_abv_uses_server_formula(self):
        result = mead_service.calculate_abv(Decimal("1.112"), Decimal("1.034"))

        self.assertEqual(result, Decimal("10.24"))

    def test_missing_abv_inputs_return_none(self):
        self.assertIsNone(mead_service.calculate_abv(Decimal("1.112"), None))

    def test_tosna_total_is_divided_into_four_decimal_doses(self):
        result = mead_service.calculate_tosna_per_dose(Decimal("4.8"))

        self.assertEqual(result, Decimal("1.2"))
        self.assertEqual(mead_service._serialize_decimal(result), "1.2")

    def test_tosna_generated_tasks_use_elapsed_hours_from_batch_start(self):
        start = datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc)

        tasks = [
            mead_service.build_tosna_task(
                batch_name="Blackberry Mead",
                start_at=start,
                dose_number=dose,
                total_amount=Decimal("4.8"),
                unit="g",
                nutrient_name="Fermaid O",
            )
            for dose in (2, 3, 4)
        ]

        self.assertEqual(
            [task["due_at"] for task in tasks],
            [
                datetime(2026, 8, 18, 19, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 19, 19, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 20, 19, 0, tzinfo=timezone.utc),
            ],
        )
        self.assertEqual(tasks[0]["source_key"], "dose_2_of_4")
        self.assertEqual(tasks[0]["description"], "Dose 2 of 4: add 1.2 g Fermaid O.")

    def test_tosna_sync_generates_only_future_doses_idempotently(self):
        cursor = FakeCursor()
        batch = {
            "id": "11111111-1111-4111-8111-111111111111",
            "name": "Blackberry Mead",
            "start_at": "2026-08-17T19:00:00+00:00",
            "tosna_enabled": True,
            "tosna_total_amount": "4.8",
            "tosna_unit": "g",
            "tosna_nutrient_name": "Fermaid O",
        }

        mead_service._sync_tosna_tasks(cursor, batch)

        insert_statements = [
            sql for sql, _params in cursor.executed if "INSERT INTO public.mead_tasks" in sql
        ]
        source_keys = [params[6] for _sql, params in cursor.executed]
        self.assertEqual(len(insert_statements), 3)
        self.assertEqual(source_keys, ["dose_2_of_4", "dose_3_of_4", "dose_4_of_4"])
        self.assertIn("ON CONFLICT (batch_id, source, source_key)", insert_statements[0])
        self.assertIn("WHERE mead_tasks.status <> 'completed'", insert_statements[0])
        self.assertNotIn("dose_1_of_4", source_keys)

    def test_completed_tosna_sync_rows_are_not_rewritten_by_later_edits(self):
        cursor = FakeCursor()
        batch = {
            "id": BATCH_ID,
            "name": "Renamed Mead",
            "start_at": "2026-08-20T19:00:00+00:00",
            "tosna_enabled": True,
            "tosna_total_amount": "9.6",
            "tosna_unit": "g",
            "tosna_nutrient_name": "Fermaid K",
        }

        mead_service._sync_tosna_tasks(cursor, batch)

        sql, params = cursor.executed[0]
        self.assertIn("WHERE mead_tasks.status <> 'completed'", sql)
        self.assertIn("task_type = EXCLUDED.task_type", sql)
        self.assertIn("title = EXCLUDED.title", sql)
        self.assertIn("description = EXCLUDED.description", sql)
        self.assertEqual(params[2], "Add TOSNA Nutrients - Renamed Mead")
        self.assertEqual(params[3], "Dose 2 of 4: add 2.4 g Fermaid K.")

    def test_disabling_tosna_cancels_pending_generated_tasks_only(self):
        cursor = FakeCursor()

        mead_service._sync_tosna_tasks(
            cursor,
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "tosna_enabled": False,
            },
        )

        sql, params = cursor.executed[0]
        self.assertIn("source = 'tosna'", sql)
        self.assertIn("status = 'pending'", sql)
        self.assertEqual(params, ("11111111-1111-4111-8111-111111111111",))

    def test_batch_detail_derives_latest_gravity_abv_recipe_timeline_and_tasks(self):
        connection = FakeConnection(
            [
                (BATCH_COLUMNS, [batch_row()]),
                (
                    EVENT_COLUMNS,
                    [
                        (
                            EVENT_ID,
                            BATCH_ID,
                            datetime(2026, 8, 19, 19, 0, tzinfo=timezone.utc),
                            "gravity_reading",
                            Decimal("1.034"),
                            "latest",
                            {},
                            NOW,
                            NOW,
                        )
                    ],
                ),
                (TASK_COLUMNS, [task_row()]),
                (
                    RECIPE_COLUMNS,
                    [
                        (
                            "55555555-5555-4555-8555-555555555555",
                            BATCH_ID,
                            "Orange blossom honey",
                            Decimal("3.25"),
                            "lb",
                            None,
                            1,
                            NOW,
                            NOW,
                        )
                    ],
                ),
                (
                    EVENT_COLUMNS,
                    [
                        (
                            "66666666-6666-4666-8666-666666666666",
                            BATCH_ID,
                            datetime(2026, 8, 18, 19, 0, tzinfo=timezone.utc),
                            "gravity_reading",
                            Decimal("1.050"),
                            "older",
                            {},
                            NOW,
                            NOW,
                        ),
                        (
                            EVENT_ID,
                            BATCH_ID,
                            datetime(2026, 8, 19, 19, 0, tzinfo=timezone.utc),
                            "gravity_reading",
                            Decimal("1.034"),
                            "latest",
                            {},
                            NOW,
                            NOW,
                        ),
                    ],
                ),
                (TASK_COLUMNS, [task_row()]),
            ]
        )

        with patch.multiple(
            mead_service,
            get_db_conn=lambda: connection,
            put_db_conn=lambda _connection: None,
        ):
            detail = mead_service.get_batch(user_id=USER_ID, batch_id=BATCH_ID)

        self.assertEqual(detail["latest_gravity"], "1.034")
        self.assertEqual(detail["estimated_current_abv"], "10.24")
        self.assertEqual(detail["projected_final_abv"], "13.39")
        self.assertEqual(detail["recipe_items"][0]["amount"], "3.25")
        self.assertEqual(len(detail["timeline"]), 2)
        self.assertEqual(detail["next_pending_task"]["id"], TASK_ID)

    def test_list_batches_scopes_to_owner_and_excludes_archived_by_default(self):
        connection = FakeConnection(
            [
                (BATCH_COLUMNS, [batch_row()]),
                ([], []),
                ([], []),
            ]
        )

        with patch.multiple(
            mead_service,
            get_db_conn=lambda: connection,
            put_db_conn=lambda _connection: None,
        ):
            batches = mead_service.list_batches(user_id=USER_ID)

        sql, params = connection.cursor_instance.executed[0]
        self.assertIn("WHERE user_id = %s", sql)
        self.assertIn("stage <> 'archived'", sql)
        self.assertEqual(params, (USER_ID,))
        self.assertEqual(batches[0]["name"], "Blackberry Mead")

    def test_replace_recipe_items_round_trips_decimal_rows_in_stable_order(self):
        connection = FakeConnection(
            [
                (BATCH_COLUMNS, [batch_row()]),
                ([], []),
                ([], []),
                ([], []),
                (
                    RECIPE_COLUMNS,
                    [
                        (
                            "55555555-5555-4555-8555-555555555555",
                            BATCH_ID,
                            "Honey",
                            Decimal("3.25"),
                            "lb",
                            None,
                            1,
                            NOW,
                            NOW,
                        ),
                        (
                            "66666666-6666-4666-8666-666666666666",
                            BATCH_ID,
                            "Fermaid O",
                            Decimal("4.8"),
                            "g",
                            None,
                            2,
                            NOW,
                            NOW,
                        ),
                    ],
                ),
            ]
        )

        with patch.multiple(
            mead_service,
            get_db_conn=lambda: connection,
            put_db_conn=lambda _connection: None,
        ):
            items = mead_service.replace_recipe_items(
                user_id=USER_ID,
                batch_id=BATCH_ID,
                items=[
                    {"name": "Honey", "amount": "3.25", "unit": "lb"},
                    {"name": "Fermaid O", "amount": "4.8", "unit": "g"},
                ],
            )

        self.assertEqual([item["name"] for item in items], ["Honey", "Fermaid O"])
        self.assertEqual(items[0]["amount"], "3.25")
        self.assertEqual(items[1]["amount"], "4.8")

    def test_stage_update_persists_stage_change_event(self):
        updated = batch_row(stage="secondary")
        connection = FakeConnection(
            [
                (BATCH_COLUMNS, [batch_row(stage="primary")]),
                (BATCH_COLUMNS, [updated]),
                ([], []),
                ([], []),
                ([], []),
                ([], []),
                ([], []),
            ]
        )

        with patch.multiple(
            mead_service,
            get_db_conn=lambda: connection,
            put_db_conn=lambda _connection: None,
        ):
            detail = mead_service.update_batch(
                USER_ID,
                BATCH_ID,
                stage="secondary",
            )

        executed_sql = "\n".join(sql for sql, _params in connection.cursor_instance.executed)
        self.assertIn("event_type", executed_sql)
        self.assertIn("'stage_change'", executed_sql)
        self.assertEqual(detail["stage"], "secondary")

    def test_backdated_tosna_due_times_remain_based_on_original_start(self):
        start = datetime(2026, 8, 15, 7, 0, tzinfo=timezone.utc)

        self.assertEqual(
            [mead_service.tosna_due_at(start, dose) for dose in (2, 3, 4)],
            [
                datetime(2026, 8, 16, 7, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 17, 7, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 18, 7, 0, tzinfo=timezone.utc),
            ],
        )

    def test_reenable_tosna_reconciles_cancelled_pending_generated_tasks(self):
        cursor = FakeCursor()
        mead_service._sync_tosna_tasks(
            cursor,
            {
                "id": BATCH_ID,
                "name": "Blackberry Mead",
                "start_at": "2026-08-17T19:00:00+00:00",
                "tosna_enabled": True,
                "tosna_total_amount": "4.8",
                "tosna_unit": "g",
                "tosna_nutrient_name": "Fermaid O",
            },
        )

        sql, _params = cursor.executed[0]
        self.assertIn("status = 'pending'", sql)

    def test_complete_pending_tosna_task_creates_one_nutrient_event(self):
        completed_at = datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc)
        connection = FakeConnection(
            [
                (TASK_COLUMNS, [task_row(status="pending")]),
                (TASK_COLUMNS, [task_row(status="completed", completed_at=completed_at)]),
                ([], []),
            ]
        )

        with patch.multiple(
            mead_service,
            get_db_conn=lambda: connection,
            put_db_conn=lambda _connection: None,
        ):
            result = mead_service.complete_task(
                user_id=USER_ID,
                task_id=TASK_ID,
                completed_at=completed_at,
            )

        executed_sql = "\n".join(sql for sql, _params in connection.cursor_instance.executed)
        self.assertIn("FOR UPDATE", executed_sql)
        self.assertIn("AND status = 'pending'", executed_sql)
        self.assertEqual(executed_sql.count("INSERT INTO public.mead_events"), 1)
        self.assertEqual(result["status"], "completed")

    def test_complete_completed_tosna_task_is_idempotent_without_second_event(self):
        completed_at = datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc)
        connection = FakeConnection(
            [
                (TASK_COLUMNS, [task_row(status="completed", completed_at=completed_at)]),
            ]
        )

        with patch.multiple(
            mead_service,
            get_db_conn=lambda: connection,
            put_db_conn=lambda _connection: None,
        ):
            result = mead_service.complete_task(
                user_id=USER_ID,
                task_id=TASK_ID,
                completed_at=datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc),
            )

        executed_sql = "\n".join(sql for sql, _params in connection.cursor_instance.executed)
        self.assertIn("FOR UPDATE", executed_sql)
        self.assertNotIn("UPDATE public.mead_tasks", executed_sql)
        self.assertNotIn("INSERT INTO public.mead_events", executed_sql)
        self.assertEqual(result["completed_at"], completed_at.isoformat())

    def test_repeated_tosna_completion_creates_only_one_nutrient_event(self):
        completed_at = datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc)
        first_connection = FakeConnection(
            [
                (TASK_COLUMNS, [task_row(status="pending")]),
                (TASK_COLUMNS, [task_row(status="completed", completed_at=completed_at)]),
                ([], []),
            ]
        )
        second_connection = FakeConnection(
            [
                (TASK_COLUMNS, [task_row(status="completed", completed_at=completed_at)]),
            ]
        )
        connections = iter([first_connection, second_connection])

        with patch.multiple(
            mead_service,
            get_db_conn=lambda: next(connections),
            put_db_conn=lambda _connection: None,
        ):
            first = mead_service.complete_task(
                user_id=USER_ID,
                task_id=TASK_ID,
                completed_at=completed_at,
            )
            second = mead_service.complete_task(
                user_id=USER_ID,
                task_id=TASK_ID,
                completed_at=datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc),
            )

        all_sql = "\n".join(
            sql
            for connection in (first_connection, second_connection)
            for sql, _params in connection.cursor_instance.executed
        )
        self.assertEqual(all_sql.count("INSERT INTO public.mead_events"), 1)
        self.assertEqual(all_sql.count("UPDATE public.mead_tasks"), 1)
        self.assertEqual(first["completed_at"], completed_at.isoformat())
        self.assertEqual(second["completed_at"], completed_at.isoformat())

    def test_cancel_pending_task_transitions_to_cancelled(self):
        connection = FakeConnection(
            [
                (TASK_COLUMNS, [task_row(status="pending")]),
                (TASK_COLUMNS, [task_row(status="cancelled")]),
            ]
        )

        with patch.multiple(
            mead_service,
            get_db_conn=lambda: connection,
            put_db_conn=lambda _connection: None,
        ):
            result = mead_service.cancel_task(user_id=USER_ID, task_id=TASK_ID)

        executed_sql = "\n".join(sql for sql, _params in connection.cursor_instance.executed)
        self.assertIn("FOR UPDATE", executed_sql)
        self.assertIn("AND status = 'pending'", executed_sql)
        self.assertEqual(result["status"], "cancelled")

    def test_cancel_completed_task_is_refused(self):
        connection = FakeConnection(
            [
                (TASK_COLUMNS, [task_row(status="completed", completed_at=NOW)]),
            ]
        )

        with patch.multiple(
            mead_service,
            get_db_conn=lambda: connection,
            put_db_conn=lambda _connection: None,
        ):
            with self.assertRaises(mead_service.MeadConflictError):
                mead_service.cancel_task(user_id=USER_ID, task_id=TASK_ID)

        executed_sql = "\n".join(sql for sql, _params in connection.cursor_instance.executed)
        self.assertNotIn("SET status = 'cancelled'", executed_sql)

    def test_cancel_already_cancelled_task_is_idempotent_success(self):
        connection = FakeConnection(
            [
                (TASK_COLUMNS, [task_row(status="cancelled")]),
            ]
        )

        with patch.multiple(
            mead_service,
            get_db_conn=lambda: connection,
            put_db_conn=lambda _connection: None,
        ):
            result = mead_service.cancel_task(user_id=USER_ID, task_id=TASK_ID)

        self.assertEqual(result["status"], "cancelled")
        executed_sql = "\n".join(sql for sql, _params in connection.cursor_instance.executed)
        self.assertNotIn("UPDATE public.mead_tasks", executed_sql)

    def test_reschedule_pending_task_resets_notification_state(self):
        new_due = datetime(2026, 8, 20, 19, 0, tzinfo=timezone.utc)
        connection = FakeConnection(
            [
                (TASK_COLUMNS, [task_row(status="pending")]),
                (TASK_COLUMNS, [task_row(status="pending", due_at=new_due)]),
            ]
        )

        with patch.multiple(
            mead_service,
            get_db_conn=lambda: connection,
            put_db_conn=lambda _connection: None,
        ):
            result = mead_service.reschedule_task(
                user_id=USER_ID,
                task_id=TASK_ID,
                due_at=new_due,
            )

        executed_sql = "\n".join(sql for sql, _params in connection.cursor_instance.executed)
        self.assertIn("notified_at = NULL", executed_sql)
        self.assertIn("notified_due_at = NULL", executed_sql)
        self.assertEqual(result["due_at"], new_due.isoformat())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from psycopg2 import pool


class OfflineThreadedConnectionPool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise RuntimeError("database access is disabled during Fitness worker tests")

    def putconn(self, _connection):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool

from backend.tasks import fitness_notification_worker as fitness_worker


USER_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"
WORKOUT_ID = "33333333-3333-4333-8333-333333333333"


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
    def __init__(self, cursors):
        self.cursors = list(cursors)
        self.used_cursors = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        cursor = self.cursors.pop(0)
        self.used_cursors.append(cursor)
        return cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FitnessNotificationWorkerTests(unittest.TestCase):
    def test_fitness_date_uses_configured_local_timezone(self):
        with patch.dict(
            "os.environ",
            {"REMIHUB_FITNESS_TIMEZONE": "America/New_York"},
            clear=True,
        ):
            target_date, timezone_name = fitness_worker.fitness_date(
                datetime(2026, 8, 22, 2, 30, tzinfo=timezone.utc)
            )

        self.assertEqual(target_date.isoformat(), "2026-08-21")
        self.assertEqual(timezone_name, "America/New_York")

    def test_notification_phase_uses_configured_local_hours(self):
        with patch.dict(
            "os.environ",
            {
                "REMIHUB_FITNESS_TIMEZONE": "America/New_York",
                "REMIHUB_FITNESS_MORNING_HOUR": "8",
                "REMIHUB_FITNESS_EVENING_HOUR": "20",
            },
            clear=True,
        ):
            morning = fitness_worker.eligible_phase(
                datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc)
            )
            evening = fitness_worker.eligible_phase(
                datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)
            )
            off_hour = fitness_worker.eligible_phase(
                datetime(2026, 8, 21, 11, 0, tzinfo=timezone.utc)
            )

        self.assertEqual(morning, "morning")
        self.assertEqual(evening, "evening")
        self.assertIsNone(off_hour)

    def test_notification_phase_catches_up_between_configured_hours(self):
        with patch.dict(
            "os.environ",
            {
                "REMIHUB_FITNESS_TIMEZONE": "America/New_York",
                "REMIHUB_FITNESS_MORNING_HOUR": "8",
                "REMIHUB_FITNESS_EVENING_HOUR": "20",
            },
            clear=True,
        ):
            late_morning = fitness_worker.eligible_phase(
                datetime(2026, 8, 21, 19, 0, tzinfo=timezone.utc)
            )
            late_evening = fitness_worker.eligible_phase(
                datetime(2026, 8, 22, 3, 30, tzinfo=timezone.utc)
            )

        self.assertEqual(late_morning, "morning")
        self.assertEqual(late_evening, "evening")

    def test_combined_morning_notification_is_inserted_once_in_caller_transaction(self):
        workout_cursor = FakeCursor(
            columns=[
                "id",
                "scheduled_date",
                "planned_distance_miles",
                "name",
                "workout_type",
                "exercise_count",
            ],
            rows=[
                (WORKOUT_ID, "2026-08-21", 2.5, "C25K W1D1", "RUNNING", 0),
                (
                    "44444444-4444-4444-8444-444444444444",
                    "2026-08-21",
                    None,
                    "Full Body",
                    "LIFTING",
                    6,
                ),
            ],
        )
        lock_cursor = FakeCursor(
            columns=["id", "status", "notification_id"],
            rows=[(RUN_ID, "inserted", None)],
        )
        notification_cursor = FakeCursor(columns=["id"], rows=[(10,)])
        update_cursor = FakeCursor()
        conn = SequenceConnection(
            [
                workout_cursor,
                lock_cursor,
                notification_cursor,
                update_cursor,
            ]
        )

        inserted = fitness_worker.process_fitness_notifications_for_user(
            conn,
            user_id=USER_ID,
            target_date=datetime(2026, 8, 21).date(),
            timezone_name="America/New_York",
            phase="morning",
        )

        self.assertTrue(inserted)
        self.assertIn("ON CONFLICT", lock_cursor.executed[0][0])
        self.assertIn("FOR UPDATE", lock_cursor.executed[1][0])
        self.assertIn("RETURNING id", notification_cursor.executed[0][0])
        params = notification_cursor.executed[0][1]
        self.assertEqual(params[0], "Today's workouts")
        self.assertIn("C25K W1D1 - 2.5 mi", params[1])
        self.assertIn("Full Body - 6 exercises", params[1])
        self.assertIn("notification_id = %s", update_cursor.executed[0][0])
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 0)

    def test_evening_notification_is_combined_and_idempotent(self):
        first_conn = SequenceConnection(
            [
                FakeCursor(
                    columns=[
                        "id",
                        "scheduled_date",
                        "planned_distance_miles",
                        "name",
                        "workout_type",
                        "exercise_count",
                    ],
                    rows=[
                        (WORKOUT_ID, "2026-08-21", 2.5, "C25K W1D1", "RUNNING", 0),
                        (
                            "44444444-4444-4444-8444-444444444444",
                            "2026-08-21",
                            None,
                            "Full Body",
                            "LIFTING",
                            6,
                        ),
                    ],
                ),
                FakeCursor(
                    columns=["id", "status", "notification_id"],
                    rows=[(RUN_ID, "inserted", None)],
                ),
                FakeCursor(columns=["id"], rows=[(10,)]),
                FakeCursor(),
            ]
        )

        inserted = fitness_worker.process_fitness_notifications_for_user(
            first_conn,
            user_id=USER_ID,
            target_date=datetime(2026, 8, 21).date(),
            timezone_name="America/New_York",
            phase="evening",
        )

        notification_params = first_conn.used_cursors[2].executed[0][1]
        self.assertTrue(inserted)
        self.assertEqual(notification_params[0], "2 workouts still incomplete")
        self.assertIn("Full Body - 6 exercises", notification_params[1])

        second_conn = SequenceConnection(
            [
                FakeCursor(
                    columns=[
                        "id",
                        "scheduled_date",
                        "planned_distance_miles",
                        "name",
                        "workout_type",
                        "exercise_count",
                    ],
                    rows=[
                        (WORKOUT_ID, "2026-08-21", 2.5, "C25K W1D1", "RUNNING", 0),
                    ],
                ),
                FakeCursor(
                    columns=["id", "status", "notification_id"],
                    rows=[(RUN_ID, "inserted", 10)],
                ),
            ]
        )

        duplicate = fitness_worker.process_fitness_notifications_for_user(
            second_conn,
            user_id=USER_ID,
            target_date=datetime(2026, 8, 21).date(),
            timezone_name="America/New_York",
            phase="evening",
        )

        self.assertFalse(duplicate)
        self.assertEqual(len(second_conn.used_cursors), 2)

    def test_existing_inserted_run_does_not_insert_duplicate_notification(self):
        workout_cursor = FakeCursor(
            columns=[
                "id",
                "scheduled_date",
                "planned_distance_miles",
                "name",
                "workout_type",
                "exercise_count",
            ],
            rows=[
                (WORKOUT_ID, "2026-08-21", 2.5, "C25K W1D1", "RUNNING", 0),
            ],
        )
        lock_cursor = FakeCursor(
            columns=["id", "status", "notification_id"],
            rows=[(RUN_ID, "inserted", 10)],
        )
        conn = SequenceConnection([workout_cursor, lock_cursor])

        inserted = fitness_worker.process_fitness_notifications_for_user(
            conn,
            user_id=USER_ID,
            target_date=datetime(2026, 8, 21).date(),
            timezone_name="America/New_York",
            phase="morning",
        )

        self.assertFalse(inserted)
        self.assertEqual(len(conn.used_cursors), 2)

    def test_no_planned_workouts_records_no_workouts_without_notification(self):
        workout_cursor = FakeCursor(
            columns=[
                "id",
                "scheduled_date",
                "planned_distance_miles",
                "name",
                "workout_type",
                "exercise_count",
            ],
            rows=[],
        )
        lock_cursor = FakeCursor(
            columns=["id", "status", "notification_id"],
            rows=[(RUN_ID, "no_workouts", None)],
        )
        conn = SequenceConnection([workout_cursor, lock_cursor])

        inserted = fitness_worker.process_fitness_notifications_for_user(
            conn,
            user_id=USER_ID,
            target_date=datetime(2026, 8, 21).date(),
            timezone_name="America/New_York",
            phase="evening",
        )

        self.assertFalse(inserted)
        insert_params = lock_cursor.executed[0][1]
        self.assertEqual(insert_params[3], "no_workouts")
        self.assertEqual(len(conn.used_cursors), 2)

    def test_planned_workout_query_excludes_non_planned_statuses(self):
        cursor = FakeCursor(columns=["id"], rows=[])
        conn = SequenceConnection([cursor])

        fitness_worker.get_planned_workouts(
            conn,
            user_id=USER_ID,
            target_date=datetime(2026, 8, 21).date(),
        )

        sql, params = cursor.executed[0]
        self.assertIn("scheduled.status = 'PLANNED'", sql)
        self.assertEqual(params[0], USER_ID)


if __name__ == "__main__":
    unittest.main()

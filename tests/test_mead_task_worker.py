from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from psycopg2 import pool


class OfflineThreadedConnectionPool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise RuntimeError("database access is disabled during Mead worker tests")

    def putconn(self, _connection):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool

from backend.tasks import mead_task_worker
from backend.tasks import notification_worker
from backend.notifications.notifications import Notification, insert_notification


TASK_ID = "33333333-3333-4333-8333-333333333333"
BATCH_ID = "22222222-2222-4222-8222-222222222222"
USER_ID = "11111111-1111-4111-8111-111111111111"
DUE_AT = datetime(2026, 8, 18, 19, 0, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, columns=None, rows=None):
        self.description = [(column,) for column in (columns or [])]
        self.rows = list(rows or [])
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        rows = self.rows
        self.rows = []
        return rows


class FakeConnection:
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


class InsertNotificationCursor(FakeCursor):
    def __init__(self):
        super().__init__(columns=["id"], rows=[(42,)])

    def execute(self, sql, params=None):
        super().execute(sql, params)

    def fetchone(self):
        if not self.rows:
            return None
        return self.rows.pop(0)


class MeadTaskWorkerTests(unittest.TestCase):
    def due_task(self):
        return {
            "id": TASK_ID,
            "batch_id": BATCH_ID,
            "task_type": "add_nutrients",
            "title": "Add TOSNA Nutrients - Blackberry Mead",
            "description": "Dose 2 of 4: add 1.2 g Fermaid O.",
            "due_at": DUE_AT,
            "status": "pending",
            "notified_at": None,
            "notified_due_at": None,
            "source": "tosna",
            "source_key": "dose_2_of_4",
            "metadata": {},
            "user_id": USER_ID,
        }

    def test_notification_payload_is_actionable_for_mead_task(self):
        notice = mead_task_worker.mead_task_notification(self.due_task())

        self.assertEqual(notice.module, "Mead")
        self.assertEqual(notice.title, "Add TOSNA Nutrients - Blackberry Mead")
        self.assertEqual(notice.body, "Dose 2 of 4: add 1.2 g Fermaid O.")
        self.assertEqual(notice.data["type"], "mead_task")
        self.assertEqual(notice.data["batch_id"], BATCH_ID)
        self.assertEqual(notice.data["task_id"], TASK_ID)
        self.assertEqual(notice.data["user_id"], USER_ID)

    def test_insert_notification_does_not_commit_caller_owned_connection(self):
        cursor = InsertNotificationCursor()
        conn = FakeConnection(cursor)

        notification_id = insert_notification(
            Notification(
                title="Workout still incomplete",
                body="Full Body has not been completed.",
                module="Fitness",
                data={"user_id": USER_ID},
            ),
            conn=conn,
        )

        sql, _params = cursor.executed[0]
        self.assertIn("RETURNING id", sql)
        self.assertEqual(notification_id, 42)
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 0)

    def test_due_query_selects_only_pending_unnotified_due_tasks(self):
        cursor = FakeCursor(columns=["id"], rows=[(TASK_ID,)])
        conn = FakeConnection(cursor)

        rows = mead_task_worker.get_due_mead_tasks(conn, now=DUE_AT, limit=10)

        sql, params = cursor.executed[0]
        self.assertIn("task.status = 'pending'", sql)
        self.assertIn("JOIN public.mead_batches AS batch", sql)
        self.assertIn("task.due_at <= %s", sql)
        self.assertIn("task.notified_due_at IS NULL", sql)
        self.assertIn("task.notified_due_at <> task.due_at", sql)
        self.assertEqual(params, (DUE_AT, 10))
        self.assertEqual(rows, [{"id": TASK_ID}])

    @patch("backend.tasks.mead_task_worker.insert_notification")
    def test_due_task_sends_one_notification_and_marks_due_at_notified(self, insert_notification):
        task = self.due_task()
        conn = MagicMock()

        with patch.multiple(
            mead_task_worker,
            get_db_conn=lambda: conn,
            put_db_conn=lambda _conn: None,
            get_due_mead_tasks=lambda _conn, now=None, limit=25: [task],
        ):
            processed = mead_task_worker.process_due_mead_tasks_once(now=DUE_AT)

        self.assertEqual(processed, 1)
        insert_notification.assert_called_once()
        conn.commit.assert_called_once()

    def test_mark_task_notified_preserves_task_until_completed_or_cancelled(self):
        cursor = FakeCursor()
        conn = FakeConnection(cursor)

        mead_task_worker.mark_task_notified(conn, task_id=TASK_ID, due_at=DUE_AT)

        sql, params = cursor.executed[0]
        self.assertIn("SET notified_at = CURRENT_TIMESTAMP", sql)
        self.assertIn("status = 'pending'", sql)
        self.assertIn("due_at = %s", sql)
        self.assertEqual(params, (DUE_AT, TASK_ID, DUE_AT))

    def test_rescheduled_task_is_eligible_when_notified_due_at_no_longer_matches(self):
        cursor = FakeCursor(columns=["id"], rows=[])
        conn = FakeConnection(cursor)

        mead_task_worker.get_due_mead_tasks(conn, now=DUE_AT, limit=25)

        sql, _params = cursor.executed[0]
        self.assertIn("task.notified_due_at <> task.due_at", sql)

    def test_notification_worker_filters_tokens_when_user_id_is_present(self):
        cursor = FakeCursor(rows=[])
        conn = FakeConnection(cursor)

        notification_worker.get_active_device_tokens(conn, user_id=USER_ID)

        sql, params = cursor.executed[0]
        self.assertIn("AND user_id = %s", sql)
        self.assertEqual(params, (USER_ID,))

    def test_targeted_unroutable_notification_does_not_starve_deliverable_rows(self):
        cursor = FakeCursor(
            columns=["id", "title", "body", "data"],
            rows=[
                (
                    2,
                    "Global notice",
                    "Deliverable",
                    {},
                )
            ],
        )
        conn = FakeConnection(cursor)

        rows = notification_worker.get_unsent_notifications(conn)

        sql, _params = cursor.executed[0]
        self.assertIn("notifications.sent = FALSE", sql)
        self.assertIn("NOT (notifications.data ? 'user_id')", sql)
        self.assertIn("EXISTS", sql)
        self.assertIn("device_push_tokens.user_id::text", sql)
        self.assertIn("LIMIT 10", sql)
        self.assertEqual(rows, [(2, "Global notice", "Deliverable", {})])

    @patch("backend.tasks.notification_worker.mark_notification_sent")
    @patch("backend.tasks.notification_worker.send_fcm_notification")
    def test_targeted_notification_with_no_token_is_never_broadcast(
        self,
        send_fcm_notification,
        mark_notification_sent,
    ):
        conn = FakeConnection(FakeCursor(rows=[]))

        notification_worker.process_notification(
            conn,
            1,
            "Add TOSNA Nutrients - Blackberry Mead",
            "Dose 2 of 4: add 1.2 g Fermaid O.",
            data={"user_id": USER_ID},
        )

        sql, params = conn.cursor_instance.executed[0]
        self.assertIn("AND user_id = %s", sql)
        self.assertEqual(params, (USER_ID,))
        send_fcm_notification.assert_not_called()
        mark_notification_sent.assert_not_called()

    def test_notification_worker_keeps_global_notifications_broadcast(self):
        cursor = FakeCursor(rows=[])
        conn = FakeConnection(cursor)

        notification_worker.get_active_device_tokens(conn)

        sql, _params = cursor.executed[0]
        self.assertIn("WHERE is_active = TRUE", sql)
        self.assertNotIn("AND user_id = %s", sql)


if __name__ == "__main__":
    unittest.main()

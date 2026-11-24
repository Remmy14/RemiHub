"""Transactional promotion tests using isolated on-disk SQLite connections.

The adapter translates PostgreSQL placeholders/casts and models FOR UPDATE as
BEGIN IMMEDIATE. It exercises the real service insert and commit/rollback paths
without connecting to a RemiHub database.
"""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.services import agent_backlog_service as backlog


ITEM_ID = "f67f7c7a-70cf-42bf-a4ae-6868981cc537"
USER_ID = "c346f3f4-3867-4ddb-83ea-7d24db8817bc"
SECOND_ID = "888a6d64-10e7-4fee-a28c-0ec7906d615f"
PROMPT = "First line\n\nSecond line\n"


class SqliteCursor:
    lock_attempt_hook = None

    def __init__(self, connection):
        self.connection = connection
        self.cursor = connection.raw.cursor()

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.cursor.close()

    @property
    def description(self):
        return self.cursor.description

    def execute(self, sql, values=()):
        if ("FOR UPDATE" in sql or "FOR SHARE" in sql or "INSERT INTO agent.backlog_order_state" in sql) and type(self).lock_attempt_hook is not None:
            type(self).lock_attempt_hook()
        if not self.connection.raw.in_transaction:
            # SQLite obtains a database write lock here. This models the row
            # lock held by PostgreSQL SELECT ... FOR UPDATE through commit.
            self.connection.raw.execute("BEGIN IMMEDIATE")
        translated = sql.replace("%s", "?").replace("::jsonb", "").replace("FOR UPDATE", "").replace("FOR SHARE", "")
        return self.cursor.execute(translated, values)

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()


class SqliteConnection:
    def __init__(self, path):
        self.raw = sqlite3.connect(":memory:", timeout=5, isolation_level=None)
        self.raw.execute("ATTACH DATABASE ? AS agent", (path,))

    def cursor(self):
        return SqliteCursor(self)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


class BacklogTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = str(Path(self.temp.name) / "agent.sqlite")
        with sqlite3.connect(self.db_path) as db:
            db.executescript("""
                CREATE TABLE backlog_items (
                    id text PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))), title text NOT NULL, prompt text NOT NULL,
                    priority text NOT NULL, created_by text NOT NULL,
                    promoted_card_id text UNIQUE, promoted_at text,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    sort_order integer
                );
                CREATE TABLE backlog_order_state (singleton boolean PRIMARY KEY, revision integer NOT NULL DEFAULT 0,
                    initialized boolean NOT NULL DEFAULT true);
                INSERT INTO backlog_order_state (singleton, revision) VALUES (true, 0);
                CREATE TABLE cards (id text PRIMARY KEY, title text, description text,
                    status text, created_by text);
                CREATE TABLE messages (id text PRIMARY KEY, card_id text, author_type text,
                    content text, created_by text, client_message_id text);
                CREATE TABLE runs (id text PRIMARY KEY, card_id text, phase text, status text,
                    card_revision integer, input_message_id text, requested_by text,
                    review_cycle text, review_attempt integer, result_metadata text);
                CREATE TABLE events (id text PRIMARY KEY, card_id text, event_type text,
                    actor_type text, actor_user_id text, payload text);
            """)
            db.execute("""INSERT INTO backlog_items
                (id, title, prompt, priority, created_by, created_at, updated_at, sort_order)
                VALUES (?, ?, ?, 'Medium', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)""",
                (ITEM_ID, "Stored request", PROMPT, USER_ID))
        getter = patch.object(backlog.agent_service, "get_db_conn", side_effect=lambda: SqliteConnection(self.db_path))
        putter = patch.object(backlog.agent_service, "put_db_conn", side_effect=lambda conn: conn.close())
        getter.start()
        putter.start()
        self.addCleanup(getter.stop)
        self.addCleanup(putter.stop)

    def snapshot(self):
        with sqlite3.connect(self.db_path) as db:
            counts = [db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("cards", "messages", "runs", "events")]
            link = db.execute("SELECT promoted_card_id FROM backlog_items WHERE id = ?", (ITEM_ID,)).fetchone()[0]
            card = db.execute("SELECT title, description, status FROM cards").fetchone()
            run = db.execute("SELECT phase, status FROM runs").fetchone()
            return counts, link, card, run

    def add_second(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("""INSERT INTO backlog_items
                (id, title, prompt, priority, created_by, created_at, updated_at, sort_order)
                VALUES (?, 'Second', 'Prompt', 'High', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 2)""",
                (SECOND_ID, USER_ID))

    def test_reorder_persists_and_keeps_priority_and_planning_inactive(self):
        self.add_second()
        original, revision = backlog.list_active()
        self.assertEqual([item["id"] for item in original], [ITEM_ID, SECOND_ID])
        self.assertEqual(backlog.reorder([SECOND_ID, ITEM_ID], revision=revision), 1)
        ordered, revision = backlog.list_active()
        self.assertEqual([item["id"] for item in ordered], [SECOND_ID, ITEM_ID])
        self.assertEqual([item["priority"] for item in ordered], ["High", "Medium"])
        self.assertEqual(backlog.list_active(order="priority")[0][0]["id"], SECOND_ID)
        self.assertEqual(self.snapshot()[0], [0, 0, 0, 0])

    def test_stale_reorder_and_new_item_placement(self):
        self.add_second()
        _, revision = backlog.list_active()
        backlog.reorder([SECOND_ID, ITEM_ID], revision=revision)
        with self.assertRaisesRegex(Exception, "order changed"):
            backlog.reorder([ITEM_ID, SECOND_ID], revision=revision)
        created = backlog.create(title="Newest", prompt="New prompt", priority="Low", created_by=USER_ID)
        ordered, current = backlog.list_active()
        self.assertEqual([item["id"] for item in ordered], [SECOND_ID, ITEM_ID, created["id"]])
        with self.assertRaisesRegex(Exception, "order changed"):
            backlog.reorder([ITEM_ID, SECOND_ID], revision=current - 1)
        with self.assertRaisesRegex(Exception, "items changed"):
            backlog.reorder([ITEM_ID, SECOND_ID], revision=current)

    def test_concurrent_reorders_only_one_wins(self):
        self.add_second()
        _, revision = backlog.list_active()
        start = threading.Barrier(3)

        def attempt(ids):
            start.wait()
            try:
                backlog.reorder(ids, revision=revision)
                return "saved"
            except Exception as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(attempt, [ITEM_ID, SECOND_ID])
            second = pool.submit(attempt, [SECOND_ID, ITEM_ID])
            start.wait()
            outcomes = [first.result(timeout=5), second.result(timeout=5)]
        self.assertEqual(outcomes.count("saved"), 1)
        self.assertEqual(sum("order changed" in outcome for outcome in outcomes), 1)
        self.assertEqual(backlog.list_active()[1], revision + 1)

    def test_retry_after_committed_response_returns_same_card(self):
        first = backlog.send_to_planning(ITEM_ID, created_by=USER_ID)
        # Simulate a lost HTTP response: the caller never uses `first` before retrying.
        second = backlog.send_to_planning(ITEM_ID, created_by=USER_ID)
        self.assertEqual(first, second)
        counts, link, card, run = self.snapshot()
        self.assertEqual(counts, [1, 1, 1, 1])
        self.assertEqual(link, first["card_id"])
        self.assertEqual(card, ("Stored request", PROMPT, "planning_queued"))
        self.assertEqual(run, ("planning", "queued"))
        self.assertEqual(backlog.list_active()[0], [])

    def test_concurrent_requests_wait_and_create_one_card(self):
        inserted = threading.Event()
        second_waiting = threading.Event()
        release_first = threading.Event()
        original = backlog.agent_service._insert_initial_card

        def pause_after_first_insert(*args, **kwargs):
            card_id = original(*args, **kwargs)
            inserted.set()
            self.assertTrue(release_first.wait(3), "second request was not started")
            return card_id

        def record_lock_attempt():
            if inserted.is_set():
                second_waiting.set()

        with patch.object(SqliteCursor, "lock_attempt_hook", record_lock_attempt), \
             patch.object(backlog.agent_service, "_insert_initial_card", side_effect=pause_after_first_insert):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(backlog.send_to_planning, ITEM_ID, created_by=USER_ID)
                self.assertTrue(inserted.wait(3))
                second = pool.submit(backlog.send_to_planning, ITEM_ID, created_by=USER_ID)
                self.assertTrue(second_waiting.wait(3))
                self.assertFalse(second.done(), "second request must wait on the transaction lock")
                release_first.set()
                results = [first.result(timeout=5), second.result(timeout=5)]
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.snapshot()[0], [1, 1, 1, 1])

    def test_failure_after_card_inserts_rolls_back_and_draft_recovers(self):
        with patch.object(backlog.agent_service, "_insert_event", side_effect=RuntimeError("event failed")):
            with self.assertRaisesRegex(RuntimeError, "event failed"):
                backlog.send_to_planning(ITEM_ID, created_by=USER_ID)
        counts, link, _card, _run = self.snapshot()
        self.assertEqual(counts, [0, 0, 0, 0])
        self.assertIsNone(link)
        self.assertEqual(len(backlog.list_active()[0]), 1)
        result = backlog.send_to_planning(ITEM_ID, created_by=USER_ID)
        self.assertEqual(self.snapshot()[1], result["card_id"])

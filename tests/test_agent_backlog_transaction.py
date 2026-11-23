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
        if "FOR UPDATE" in sql and type(self).lock_attempt_hook is not None:
            type(self).lock_attempt_hook()
        if not self.connection.raw.in_transaction:
            # SQLite obtains a database write lock here. This models the row
            # lock held by PostgreSQL SELECT ... FOR UPDATE through commit.
            self.connection.raw.execute("BEGIN IMMEDIATE")
        translated = sql.replace("%s", "?").replace("::jsonb", "").replace("FOR UPDATE", "")
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
                    id text PRIMARY KEY, title text NOT NULL, prompt text NOT NULL,
                    priority text NOT NULL, created_by text NOT NULL,
                    promoted_card_id text UNIQUE, promoted_at text,
                    created_at text NOT NULL, updated_at text NOT NULL
                );
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
                (id, title, prompt, priority, created_by, created_at, updated_at)
                VALUES (?, ?, ?, 'Medium', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
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
        self.assertEqual(backlog.list_active(), [])

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
        self.assertEqual(len(backlog.list_active()), 1)
        result = backlog.send_to_planning(ITEM_ID, created_by=USER_ID)
        self.assertEqual(self.snapshot()[1], result["card_id"])

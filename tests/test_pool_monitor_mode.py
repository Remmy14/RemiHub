import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from psycopg2 import pool


class OfflineThreadedConnectionPool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise RuntimeError("database access is disabled during pool mode tests")

    def putconn(self, _connection):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool

from backend.scripts import pool_watch_meta
from backend.tasks import swimming_pool_monitor


class PoolMonitorModeTests(unittest.TestCase):
    def setUp(self):
        self.config = {"Pool Monitor": {"url": "unused"}}

    @patch.object(swimming_pool_monitor, "save_data_to_database")
    @patch.object(swimming_pool_monitor, "fetch_raymote_temperatures")
    @patch.object(pool_watch_meta, "get_summer_mode", return_value=False)
    def test_winter_mode_skips_scrape_and_insert(self, get_mode, fetch, save):
        self.assertFalse(swimming_pool_monitor.run_pool_monitor_once(self.config))

        get_mode.assert_called_once_with()
        fetch.assert_not_called()
        save.assert_not_called()

    @patch.object(swimming_pool_monitor, "save_data_to_database")
    @patch.object(swimming_pool_monitor, "fetch_raymote_temperatures")
    @patch.object(pool_watch_meta, "get_summer_mode", return_value=True)
    def test_summer_mode_scrapes_and_saves(self, get_mode, fetch, save):
        fetch.return_value = {"inlet": 80.0}

        self.assertTrue(swimming_pool_monitor.run_pool_monitor_once(self.config))

        get_mode.assert_called_once_with()
        fetch.assert_called_once_with(self.config["Pool Monitor"])
        save.assert_called_once_with(fetch.return_value)

    @patch.object(swimming_pool_monitor, "save_data_to_database")
    @patch.object(swimming_pool_monitor, "fetch_raymote_temperatures")
    @patch.object(pool_watch_meta, "get_summer_mode", side_effect=RuntimeError("mode unavailable"))
    def test_mode_lookup_failure_does_not_scrape(self, _get_mode, fetch, save):
        with self.assertRaisesRegex(RuntimeError, "mode unavailable"):
            swimming_pool_monitor.run_pool_monitor_once(self.config)

        fetch.assert_not_called()
        save.assert_not_called()


class PoolModePersistenceTests(unittest.TestCase):
    @patch.object(pool_watch_meta, "put_db_conn")
    @patch.object(pool_watch_meta, "get_db_conn")
    def test_set_mode_upserts_winter_into_database(self, get_conn, put_conn):
        conn = MagicMock()
        updated_at = datetime(2026, 10, 19, tzinfo=timezone.utc)
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (
            False, updated_at
        )
        get_conn.return_value = conn

        result = pool_watch_meta.set_summer_mode(False)

        cursor = conn.cursor.return_value.__enter__.return_value
        sql, params = cursor.execute.call_args.args
        self.assertIn("INSERT INTO pool_watch_meta", sql)
        self.assertIn("ON CONFLICT (id)", sql)
        self.assertEqual(params, (False,))
        self.assertEqual(result, {"summerMode": False, "updatedAt": updated_at.isoformat()})
        conn.commit.assert_called_once_with()
        put_conn.assert_called_once_with(conn)

    @patch.object(pool_watch_meta, "put_db_conn")
    @patch.object(pool_watch_meta, "get_db_conn")
    def test_get_mode_reads_winter_from_database(self, get_conn, put_conn):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (False,)
        get_conn.return_value = conn

        self.assertFalse(pool_watch_meta.get_summer_mode())

        cursor = conn.cursor.return_value.__enter__.return_value
        self.assertIn("FROM pool_watch_meta", cursor.execute.call_args.args[0])
        conn.commit.assert_not_called()
        put_conn.assert_called_once_with(conn)


if __name__ == "__main__":
    unittest.main()

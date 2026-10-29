"""Snapshot replacement tests using a transactional in-memory SQL boundary."""

import copy
import importlib
import sys
import types
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch


MONTH = date(2026, 10, 1)
ASSET = {"id": "manual-1", "label": "Savings", "asset_category": "cash",
         "current_value": Decimal("25.00"), "as_of_date": MONTH,
         "notes": None, "updated_at": None, "valuation_key": None}


def old_snapshot(status="success"):
    return {"id": "original", "status": status, "snapshot_month": MONTH,
            "total_assets": Decimal("100.00"), "total_liabilities": Decimal("10.00"),
            "net_worth": Decimal("90.00"), "created_at": "original-time",
            "items": [{"id": "old-item", "source_account_id": "old-account",
                       "value": Decimal("100.00")}]}


class Store:
    def __init__(self, snapshot=None):
        self.snapshot = copy.deepcopy(snapshot)
        self.statements = []
        self.fail_item = False
        self.rollbacks = 0

    def connection(self):
        return Connection(self)


class Connection:
    def __init__(self, store):
        self.store = store
        self.snapshot = copy.deepcopy(store.snapshot)
        self.row = None

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, args=None):
        self.store.statements.append(sql.strip())
        if "pg_advisory_xact_lock" in sql:
            return
        if "SELECT id, status FROM finance_snapshots" in sql:
            self.row = ((self.snapshot["id"], self.snapshot["status"])
                        if self.snapshot else None)
        elif "DELETE FROM finance_snapshots" in sql:
            assert self.snapshot["id"] == args[0]
            self.snapshot = None
        elif "INSERT INTO finance_snapshots" in sql:
            self.snapshot = {"id": args[0], "snapshot_month": args[1],
                             "status": args[2], "total_assets": args[3],
                             "total_liabilities": args[4], "net_worth": args[5],
                             "created_at": "new-time", "items": []}
        elif "INSERT INTO finance_snapshot_items" in sql:
            if self.store.fail_item:
                raise RuntimeError("simulated child insert failure")
            self.snapshot["items"].append({"id": args[0],
                                           "source_account_id": args[3],
                                           "value": args[8]})
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.row

    def commit(self):
        self.store.snapshot = copy.deepcopy(self.snapshot)

    def rollback(self):
        self.store.rollbacks += 1


class SnapshotIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        original = sys.modules.get("backend.database.database")
        fake = types.ModuleType("backend.database.database")
        fake.get_db_conn = lambda: None
        fake.put_db_conn = lambda _conn: None
        sys.modules["backend.database.database"] = fake
        try:
            cls.worker = importlib.import_module("backend.tasks.finance_worker")
        finally:
            if original is None:
                sys.modules.pop("backend.database.database", None)
            else:
                sys.modules["backend.database.database"] = original

    def run_snapshot(self, store, *, force=False, plaid_errors=None, assets=None,
                     valuations=None, on_collect=None):
        worker = self.worker

        def collect_plaid():
            if on_collect:
                on_collect()
            return [], plaid_errors or []

        with patch.object(worker, "get_db_conn", side_effect=store.connection), \
             patch.object(worker, "put_db_conn"), \
             patch.object(worker, "collect_plaid_snapshot_items", side_effect=collect_plaid), \
             patch.object(worker, "_get_configured_plaid_accounts", return_value=[]), \
             patch.object(worker, "get_manual_assets", return_value=assets if assets is not None else [ASSET]), \
             patch.object(worker, "collect_stubbed_valuation_items", return_value=valuations or []), \
             patch.dict(worker.finance_config, {}, clear=True):
            return worker.create_monthly_finance_snapshot(target_date=MONTH, force=force)

    def test_successful_snapshot_skips_scheduled_collection(self):
        store = Store(old_snapshot())
        before = copy.deepcopy(store.snapshot)
        result = self.run_snapshot(store)
        self.assertTrue(result["skipped"])
        self.assertEqual(store.snapshot, before)
        self.assertFalse(any("DELETE FROM" in sql for sql in store.statements))

    def test_successful_snapshot_rejects_forced_incomplete_collection(self):
        store = Store(old_snapshot())
        before = copy.deepcopy(store.snapshot)
        result = self.run_snapshot(store, force=True, plaid_errors=["missing account"])
        self.assertEqual(result["status"], "blocked_incomplete")
        self.assertTrue(result["preserved"])
        self.assertEqual(result["snapshot_id"], before["id"])
        self.assertEqual(store.snapshot, before)
        with patch.object(self.worker, "insert_notification") as notice:
            self.worker.send_finance_snapshot_notification(result)
        notice.assert_not_called()

    def test_successful_snapshot_forced_complete_replacement(self):
        store = Store(old_snapshot())
        result = self.run_snapshot(store, force=True)
        self.assertTrue(result["success"])
        self.assertEqual(store.snapshot["status"], "success")
        self.assertNotEqual(store.snapshot["id"], "original")
        self.assertEqual(store.snapshot["total_assets"], Decimal("25.00"))
        self.assertEqual(store.snapshot["items"][0]["source_account_id"], "manual-1")

    def test_partial_upgrades_only_with_complete_collection(self):
        store = Store(old_snapshot("partial_success"))
        before = copy.deepcopy(store.snapshot)
        rejected = self.run_snapshot(store, plaid_errors=["missing account"])
        self.assertTrue(rejected["preserved"])
        self.assertEqual(store.snapshot, before)
        upgraded = self.run_snapshot(store)
        self.assertTrue(upgraded["success"])
        self.assertEqual(store.snapshot["status"], "success")
        self.assertEqual(store.snapshot["net_worth"], Decimal("25.00"))
        self.assertEqual(store.snapshot["items"][0]["value"], Decimal("25.00"))

    def test_new_incomplete_collection_is_blocked(self):
        store = Store()
        result = self.run_snapshot(store, plaid_errors=["missing account"])
        self.assertFalse(result["success"])
        self.assertIsNone(store.snapshot)
        self.assertIn("pg_advisory_xact_lock", store.statements[0])

    def test_missing_and_nonfinite_manual_values_block_but_zero_is_valid(self):
        for value in (None, "bad", "NaN", "Infinity"):
            with self.subTest(value=value):
                store = Store(old_snapshot())
                before = copy.deepcopy(store.snapshot)
                result = self.run_snapshot(store, force=True,
                                           assets=[{**ASSET, "current_value": value}])
                self.assertFalse(result["success"])
                self.assertEqual(store.snapshot, before)
        store = Store(old_snapshot())
        result = self.run_snapshot(store, force=True,
                                   assets=[{**ASSET, "current_value": Decimal("0") }])
        self.assertTrue(result["success"])
        self.assertEqual(store.snapshot["total_assets"], Decimal("0.00"))

    def test_configured_valuation_requires_valid_live_or_manual_fallback(self):
        worker = self.worker
        with patch.dict(worker.finance_config, {"zillow_url": "configured"}, clear=True), \
             patch.object(worker, "_get_configured_plaid_accounts", return_value=[]), \
             patch.object(worker, "get_manual_assets", return_value=[]):
            with self.assertRaisesRegex(ValueError, "no valid value or manual fallback"):
                worker._complete_snapshot_items([], [])
        fallback = {**ASSET, "valuation_key": "home_primary", "current_value": "0"}
        with patch.dict(worker.finance_config, {"zillow_url": "configured"}, clear=True), \
             patch.object(worker, "_get_configured_plaid_accounts", return_value=[]), \
             patch.object(worker, "get_manual_assets", return_value=[fallback]):
            items = worker._complete_snapshot_items([], [])
            self.assertEqual(items[0]["value"], Decimal("0.00"))
            bad_live = {"source": "zillow", "source_account_id": "home_primary",
                        "valuation_key": "home_primary", "value": "NaN"}
            items = worker._complete_snapshot_items([], [bad_live])
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["source"], "manual")
            good_live = {**bad_live, "value": "150.00"}
            items = worker._complete_snapshot_items([], [good_live])
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["value"], Decimal("150.00"))

    def test_missing_included_plaid_account_blocks_complete_status(self):
        worker = self.worker
        account = {"source_account_id": "required", "include_in_net_worth": True}
        with patch.object(worker, "_get_configured_plaid_accounts", return_value=[account]):
            with self.assertRaisesRegex(ValueError, "included Plaid accounts"):
                worker._complete_snapshot_items([], [])

    def test_valuation_provider_failure_preserves_snapshot(self):
        worker = self.worker
        store = Store(old_snapshot())
        before = copy.deepcopy(store.snapshot)
        with patch.object(worker, "get_db_conn", side_effect=store.connection), \
             patch.object(worker, "put_db_conn"), \
             patch.object(worker, "collect_plaid_snapshot_items", return_value=([], [])), \
             patch.object(worker, "collect_stubbed_valuation_items",
                          side_effect=RuntimeError("provider unavailable")):
            result = worker.create_monthly_finance_snapshot(target_date=MONTH, force=True)
        self.assertEqual(result["status"], "blocked_incomplete")
        self.assertIn("provider unavailable", result["errors"][0])
        self.assertEqual(store.snapshot, before)

    def test_failed_child_write_rolls_back_parent_and_entries(self):
        store = Store(old_snapshot())
        before = copy.deepcopy(store.snapshot)
        store.fail_item = True
        with self.assertRaisesRegex(RuntimeError, "child insert failure"):
            self.run_snapshot(store, force=True)
        self.assertEqual(store.snapshot, before)
        self.assertEqual(store.rollbacks, 1)

    def test_interleaved_attempt_rechecks_success_after_collection(self):
        store = Store()

        def competing_worker():
            # Another worker commits while this worker is collecting. The
            # final locked read must see it and decline an automated replace.
            store.snapshot = old_snapshot()

        result = self.run_snapshot(store, on_collect=competing_worker)
        self.assertTrue(result["skipped"])
        self.assertEqual(store.snapshot, old_snapshot())
        locks = [sql for sql in store.statements if "pg_advisory_xact_lock" in sql]
        self.assertEqual(len(locks), 2)


if __name__ == "__main__":
    unittest.main()

"""HTTP contract and validation tests without a live database or provider network."""

import importlib
import os
import sys
import types
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.core.auth import AuthenticatedPrincipal, get_current_principal, require_current_principal
from backend.models.finance_models import ManualAccountCreate, ManualAccountPatch


ACCOUNT_ID = "b0f18a35-eec0-4bea-ab71-a93cc080087e"
ACCOUNT = {
    "id": ACCOUNT_ID, "name": "Arbol 401(k) — Voya / ADP TotalSource",
    "classification": "asset", "asset_category": "retirement",
    "current_balance": "12345.67", "as_of_date": "2026-10-18",
    "updated_at": "2026-10-19T13:00:00+00:00",
    "include_in_net_worth": True, "notes": None, "active": True,
}
CREATE = {key: ACCOUNT[key] for key in (
    "name", "classification", "asset_category", "current_balance",
    "as_of_date", "include_in_net_worth", "notes",
)}
ADMIN = AuthenticatedPrincipal("1", "admin-uid", "admin@example.test", None, "admin")
MEMBER = AuthenticatedPrincipal("2", "member-uid", "member@example.test", None, "member")


class ManualAccountValidationTests(unittest.TestCase):
    def test_voya_creation_and_balance(self):
        request = ManualAccountCreate.model_validate(CREATE)
        self.assertEqual(request.asset_category, "retirement")
        self.assertEqual(request.current_balance, "12345.67")

    def test_invalid_values(self):
        for changes in (
            {"name": " Arbol"}, {"name": ""}, {"classification": "liability"},
            {"asset_category": "liability"}, {"current_balance": "-1.00"},
            {"current_balance": "1.001"}, {"current_balance": "NaN"},
            {"current_balance": 12.34}, {"as_of_date": "2999-01-01"},
            {"asset_category": "unknown"}, {"notes": "x" * 2001},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ManualAccountCreate.model_validate({**CREATE, **changes})

    def test_patch_validation(self):
        for invalid in ({}, {"name": None}, {"current_balance": "1.001"},
                        {"valuation_key": "home_primary"}, {"active": None}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                ManualAccountPatch.model_validate(invalid)
        self.assertIsNone(ManualAccountPatch.model_validate({"notes": None}).notes)
        self.assertEqual(ManualAccountPatch.model_validate(
            {"as_of_date": date.today().isoformat()}).as_of_date, date.today())


@unittest.skipUnless(os.environ.get("REMIHUB_HTTP_TESTS") == "1",
                     "HTTP TestClient requires a runnable local socket; enable with REMIHUB_HTTP_TESTS=1")
class ManualAccountHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The production database module opens a connection during import.
        # Replace only that boundary while importing the finance router.
        original = sys.modules.get("backend.database.database")
        fake = types.ModuleType("backend.database.database")
        fake.get_db_conn = lambda: None
        fake.put_db_conn = lambda _conn: None
        sys.modules["backend.database.database"] = fake
        try:
            cls.finance = importlib.import_module("backend.routers.finance")
        finally:
            if original is None:
                sys.modules.pop("backend.database.database", None)
            else:
                sys.modules["backend.database.database"] = original
        cls.app = FastAPI()
        cls.app.include_router(cls.finance.router, dependencies=[Depends(get_current_principal)])
        cls.client = TestClient(cls.app)

    def test_admin_crud_contract(self):
        self.app.dependency_overrides[require_current_principal] = lambda: ADMIN
        try:
            service = self.finance.manual_service
            with patch.object(service, "list_accounts", return_value=[ACCOUNT]) as listing, \
                 patch.object(service, "create_account", return_value=ACCOUNT) as creating, \
                 patch.object(service, "get_account", return_value=ACCOUNT) as getting, \
                 patch.object(service, "update_account", return_value=ACCOUNT) as updating:
                self.assertEqual(self.client.get("/finance/manual-accounts").json()["data"], [ACCOUNT])
                self.assertEqual(self.client.get("/finance/manual-accounts?include_archived=true").status_code, 200)
                listing.assert_called_with(include_archived=True)
                response = self.client.post("/finance/manual-accounts", json=CREATE)
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json()["data"]["id"], ACCOUNT_ID)
                self.assertIsInstance(creating.call_args.args[0], ManualAccountCreate)
                self.assertEqual(self.client.get(f"/finance/manual-accounts/{ACCOUNT_ID}").status_code, 200)
                getting.assert_called_with(UUID(ACCOUNT_ID))
                response = self.client.patch(f"/finance/manual-accounts/{ACCOUNT_ID}",
                                             json={"current_balance": "13000.00", "as_of_date": "2026-10-19"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(updating.call_args.args[1].current_balance, "13000.00")
                for active in (False, True):
                    self.assertEqual(self.client.patch(f"/finance/manual-accounts/{ACCOUNT_ID}",
                                                       json={"active": active}).status_code, 200)
        finally:
            self.app.dependency_overrides.clear()

    def test_admin_errors(self):
        self.app.dependency_overrides[require_current_principal] = lambda: ADMIN
        try:
            service = self.finance.manual_service
            for error, expected in ((service.AccountNotFound("missing"), 404),
                                    (service.AccountConflict("duplicate"), 409)):
                with patch.object(service, "get_account", side_effect=error):
                    self.assertEqual(self.client.get(f"/finance/manual-accounts/{ACCOUNT_ID}").status_code, expected)
            self.assertEqual(self.client.get("/finance/manual-accounts/not-a-uuid").status_code, 422)
            self.assertEqual(self.client.post("/finance/manual-accounts", json={**CREATE, "current_balance": "NaN"}).status_code, 422)
            self.assertEqual(self.client.patch(f"/finance/manual-accounts/{ACCOUNT_ID}", json={}).status_code, 422)
        finally:
            self.app.dependency_overrides.clear()

    def test_authorization_in_transition_and_required_modes(self):
        for mode in ("transition", "required"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"REMIHUB_AUTH_MODE": mode}):
                self.assertEqual(self.client.get("/finance/manual-accounts").status_code, 401)
                self.assertEqual(self.client.post("/finance/manual-accounts", json=CREATE).status_code, 401)
                self.app.dependency_overrides[require_current_principal] = lambda: MEMBER
                try:
                    self.assertEqual(self.client.get("/finance/manual-accounts").status_code, 403)
                finally:
                    self.app.dependency_overrides.clear()


class ManualSnapshotTests(unittest.TestCase):
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

    def test_voya_liability_and_valuation_precedence(self):
        worker = self.worker
        records = [
            {"id": ACCOUNT_ID, "label": ACCOUNT["name"], "asset_category": "retirement",
             "current_value": Decimal("13000.00"), "as_of_date": date(2026, 10, 19),
             "notes": None, "updated_at": None, "valuation_key": None},
            {"id": "loan-id", "label": "Auto loan", "asset_category": "liability",
             "current_value": Decimal("5000.00"), "as_of_date": date(2026, 10, 19),
             "notes": None, "updated_at": None, "valuation_key": None},
            {"id": "home-id", "label": "Home", "asset_category": "home",
             "current_value": Decimal("100000.00"), "as_of_date": date(2026, 10, 19),
             "notes": None, "updated_at": None, "valuation_key": "home_primary"},
        ]
        with patch.object(worker, "get_manual_assets", return_value=records):
            items = worker.collect_manual_snapshot_items(excluded_valuation_keys={"home_primary"})
        self.assertEqual([item["source_account_id"] for item in items].count(ACCOUNT_ID), 1)
        self.assertNotIn("home-id", [item["source_account_id"] for item in items])
        self.assertEqual(items[1]["asset_category"], "liability")
        self.assertEqual(worker._snapshot_totals(items),
                         (Decimal("13000.00"), Decimal("5000.00"), Decimal("8000.00")))

    def test_monthly_snapshot_does_not_change_historical_record(self):
        worker = self.worker
        old_snapshot = {"total_assets": Decimal("200.00"), "net_worth": Decimal("200.00")}
        historical_copy = old_snapshot.copy()
        captured = []

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def execute(self, sql, args=None): captured.append((sql, args))

        class Connection:
            def cursor(self): return Cursor()
            def commit(self): pass

        item = {"source": "manual", "source_account_id": ACCOUNT_ID,
                "label": ACCOUNT["name"], "institution_label": None,
                "asset_category": "retirement", "value": Decimal("13000.00"),
                "include_in_net_worth": True, "confidence": "manual", "raw_payload": {}}
        with patch.object(worker, "_get_existing_snapshot_id", return_value=None), \
             patch.object(worker, "collect_plaid_snapshot_items", return_value=([], [])), \
             patch.object(worker, "collect_stubbed_valuation_items", return_value=[]), \
             patch.object(worker, "collect_manual_snapshot_items", return_value=[item]), \
             patch.object(worker, "get_db_conn", return_value=Connection()), \
             patch.object(worker, "put_db_conn"):
            result = worker.create_monthly_finance_snapshot(target_date=date(2026, 11, 1))
        self.assertEqual(result["item_count"], 1)
        self.assertEqual(old_snapshot, historical_copy)
        self.assertEqual(sum("INSERT INTO finance_snapshot_items" in sql for sql, _ in captured), 1)
        self.assertFalse(any("DELETE FROM finance_snapshots" in sql for sql, _ in captured))

    def test_legacy_maintenance_does_not_insert_after_rename(self):
        worker = self.worker
        queries = []

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def execute(self, sql, args=None): queries.append(sql)
            def fetchall(self): return []

        class Connection:
            def cursor(self): return Cursor()
            def rollback(self): pass

        with patch.object(worker, "get_db_conn", return_value=Connection()), \
             patch.object(worker, "put_db_conn"):
            with self.assertRaisesRegex(ValueError, "use its UUID"):
                worker.upsert_manual_asset(label="Old account name", asset_category="retirement",
                                           current_value="1.00", as_of_date="2026-10-19")
        self.assertFalse(any("INSERT" in sql for sql in queries))

    def test_maintenance_targets_uuid_after_rename(self):
        worker = self.worker
        statements = []

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def execute(self, sql, args=None): statements.append((sql, args))
            def fetchone(self):
                if len(statements) == 1:
                    return (ACCOUNT_ID,)
                return (None, "retirement")

        class Connection:
            def cursor(self): return Cursor()
            def commit(self): pass
            def rollback(self): pass

        with patch.object(worker, "get_db_conn", return_value=Connection()), \
             patch.object(worker, "put_db_conn"):
            worker.upsert_manual_asset(asset_id=ACCOUNT_ID, label="Old account name",
                                       asset_category="retirement", current_value="20.00",
                                       as_of_date="2026-10-19")
        self.assertEqual(statements[0][1], (ACCOUNT_ID,))
        self.assertIn("UPDATE finance_manual_assets", statements[-1][0])
        self.assertFalse(any("INSERT" in sql for sql, _ in statements))


class ManualServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        original = sys.modules.get("backend.database.database")
        fake = types.ModuleType("backend.database.database")
        fake.get_db_conn = lambda: None
        fake.put_db_conn = lambda _conn: None
        sys.modules["backend.database.database"] = fake
        try:
            cls.service = importlib.import_module("backend.services.manual_finance_service")
        finally:
            if original is None:
                sys.modules.pop("backend.database.database", None)
            else:
                sys.modules["backend.database.database"] = original

    def test_classification_change_updates_category_in_same_statement(self):
        service = self.service
        queries = []

        class Cursor:
            description = [(field,) for field in (
                "id", "label", "classification", "asset_category", "current_value",
                "as_of_date", "updated_at", "include_in_net_worth", "notes",
                "active", "valuation_key")]

            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def execute(self, sql, args=None): queries.append((sql, args))
            def fetchone(self):
                if len(queries) == 1:
                    return (ACCOUNT["name"], "retirement", None)
                return (UUID(ACCOUNT_ID), ACCOUNT["name"], "liability", "liability",
                        Decimal("5000.00"), date(2026, 10, 19),
                        date(2026, 10, 19), True, None, True, None)

        class Connection:
            def cursor(self): return Cursor()
            def commit(self): pass
            def rollback(self): pass

        with patch.object(service, "get_db_conn", return_value=Connection()), \
             patch.object(service, "put_db_conn"):
            result = service.update_account(UUID(ACCOUNT_ID),
                                            ManualAccountPatch.model_validate({"classification": "liability"}))
        self.assertEqual(result["asset_category"], "liability")
        self.assertEqual(result["current_balance"], "5000.00")
        self.assertIn("asset_category = %s", queries[1][0])
        self.assertEqual(queries[1][1][0], "liability")

    def test_contradictory_partial_update_is_rejected(self):
        service = self.service

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def execute(self, *_args): pass
            def fetchone(self): return ("Auto loan", "liability", None)

        class Connection:
            def cursor(self): return Cursor()
            def rollback(self): pass

        with patch.object(service, "get_db_conn", return_value=Connection()), \
             patch.object(service, "put_db_conn"):
            with self.assertRaises(service.AccountInvalid):
                service.update_account(UUID(ACCOUNT_ID),
                                       ManualAccountPatch.model_validate({"classification": "asset"}))


if __name__ == "__main__":
    unittest.main()

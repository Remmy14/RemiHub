"""Finance preflight uses stored evidence only and never calls providers or writers."""

import importlib
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi import Depends, FastAPI, HTTPException

from backend.core.auth import AuthenticatedPrincipal, get_current_principal, require_admin_principal, require_current_principal
original_database = sys.modules.get("backend.database.database")
fake_database = types.ModuleType("backend.database.database")
fake_database.get_db_conn = lambda: None
fake_database.put_db_conn = lambda _connection: None
sys.modules["backend.database.database"] = fake_database
try:
    finance = importlib.import_module("backend.routers.finance")
    health = importlib.import_module("backend.services.finance_health_service")
    worker = importlib.import_module("backend.tasks.finance_worker")
finally:
    if original_database is None:
        sys.modules.pop("backend.database.database", None)
    else:
        sys.modules["backend.database.database"] = original_database


NOW = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
MONTH = date(2026, 11, 1)


def plaid_account(**changes):
    return {"id": "account-1", "source": "plaid", "source_account_id": "plaid-1",
            "institution_label": "Bank", "name": "Checking", "display_name": "Checking",
            "asset_category": "cash", "include_in_net_worth": True, **changes}


def manual_asset(**changes):
    return {"id": "manual-1", "label": "Retirement", "asset_category": "retirement",
            "current_value": Decimal("100.00"), "as_of_date": NOW.date(),
            "updated_at": NOW, "include_in_net_worth": True,
            "active": True, "valuation_key": None, **changes}


def evidence(*, plaid=True, manual=False, snapshot=True):
    return {
        "accounts": [plaid_account()] if plaid else [],
        "items": [{"id": "item-1", "label": "Bank", "enabled": True,
                   "last_success_at": NOW - timedelta(days=1), "last_error": None}] if plaid else [],
        "manual": [manual_asset()] if manual else [],
        "snapshot": {"id": "snap-1", "snapshot_month": MONTH,
                     "status": "success", "created_at": NOW - timedelta(hours=2)} if snapshot else None,
        "snapshot_items": [{"source": "plaid", "source_account_id": "plaid-1",
                            "value": Decimal("10.00"), "asset_category": "cash",
                            "include_in_net_worth": True}] if plaid and snapshot else ([] if snapshot else None),
        "errors": [],
    }


def assess(data, *, check=lambda _plaid, _valuations: []):
    return health.assess_evidence(data, now=NOW, config={}, completeness_check=check)


class HealthAssessmentTests(unittest.TestCase):
    def test_recent_plaid_and_ready_snapshot(self):
        result = assess(evidence())
        self.assertEqual(result["accounts"][0]["health"], "HEALTHY")
        self.assertEqual(result["readiness"], "READY")
        self.assertTrue(result["sufficient_evidence"])
        self.assertEqual(result["data_as_of"], NOW - timedelta(hours=2))

    def test_login_required_is_blocked_without_leaking_error(self):
        data = evidence()
        data["items"][0]["last_error"] = "access_token=secret ITEM_LOGIN_REQUIRED plaid response"
        result = assess(data)
        self.assertEqual(result["accounts"][0]["health"], "BLOCKED")
        self.assertEqual(result["readiness"], "BLOCKED")
        self.assertIn("PLAID_REAUTH_REQUIRED", [issue["code"] for issue in result["actionable_issues"]])
        self.assertNotIn("secret", str(result))

    def test_stale_plaid_connection_warns_without_balance_change_heuristic(self):
        data = evidence(snapshot=False)
        data["items"][0]["last_success_at"] = NOW - timedelta(days=8)
        result = assess(data)
        self.assertEqual(result["accounts"][0]["health"], "WARNING")
        self.assertEqual(result["readiness"], "UNKNOWN")
        self.assertEqual(result["accounts"][0]["freshness"], "STALE")

    def test_recent_item_success_does_not_refresh_stale_account_collection(self):
        data = evidence()
        data["snapshot"]["created_at"] = NOW - timedelta(days=8)
        data["items"][0]["last_success_at"] = NOW - timedelta(hours=1)
        result = assess(data)
        account = result["accounts"][0]
        self.assertEqual(account["last_successful_update"], data["snapshot"]["created_at"])
        self.assertEqual(account["freshness"], "STALE")
        self.assertEqual(account["health"], "WARNING")
        self.assertEqual(result["readiness"], "WARNING")

    def test_recent_item_success_without_account_proof_is_unknown(self):
        result = assess(evidence(snapshot=False))
        self.assertEqual(result["accounts"][0]["freshness"], "UNKNOWN")
        self.assertEqual(result["accounts"][0]["health"], "UNKNOWN")

    def test_missing_required_balance_and_account_block(self):
        for rows, code in (([{"source": "plaid", "source_account_id": "plaid-1", "value": None}], "PLAID_BALANCE_MISSING"),
                           ([], "PLAID_ACCOUNT_MISSING")):
            with self.subTest(code=code):
                data = evidence()
                data["snapshot_items"] = rows
                result = assess(data)
                self.assertEqual(result["readiness"], "BLOCKED")
                self.assertIn(code, [issue["code"] for issue in result["accounts"][0]["issues"]])

    def test_worker_persisted_missing_account_and_balance_block_without_snapshot(self):
        # collect_plaid_snapshot_items() stores these on finance_plaid_items
        # after rejecting the month, so no new snapshot row is available.
        failures = (
            ("missing included account plaid-1 (Checking)", "PLAID_ACCOUNT_MISSING"),
            ("included account plaid-1 (Checking): missing balances.current", "PLAID_BALANCE_MISSING"),
            ("included account plaid-1 (Checking): invalid balances.current", "PLAID_BALANCE_MISSING"),
        )
        for detail, code in failures:
            with self.subTest(code=code, detail=detail):
                data = evidence(snapshot=False)
                data["items"][0]["last_error"] = f"Bank (connection item-1): {detail}"
                result = assess(data)
                self.assertEqual(result["accounts"][0]["health"], "BLOCKED")
                self.assertEqual(result["readiness"], "BLOCKED")
                self.assertIn(code, [issue["code"] for issue in result["snapshot"]["blocking_conditions"]])
                self.assertNotIn("item-1", str(result))

    def test_worker_generated_failure_evidence_blocks_without_snapshot(self):
        item = {"id": "item-1", "label": "Bank", "environment": "production",
                "access_token_encrypted": "encrypted", "enabled": True}
        configured = {"source_account_id": "plaid-1", "institution_label": "Bank",
                      "name": "Checking", "include_in_net_worth": True}
        for accounts, expected_code in (
            ([], "PLAID_ACCOUNT_MISSING"),
            ([{"account_id": "plaid-1", "name": "Checking",
               "balances": {"current": None}}], "PLAID_BALANCE_MISSING"),
        ):
            with self.subTest(code=expected_code):
                client = MagicMock()
                client.accounts_get.return_value.to_dict.return_value = {"accounts": accounts}
                with patch.object(worker, "get_enabled_plaid_items", return_value=[item]), \
                     patch.object(worker, "_get_configured_plaid_accounts", return_value=[configured]), \
                     patch.object(worker, "get_plaid_client", return_value=client), \
                     patch.object(worker, "decrypt_access_token", return_value="unused"), \
                     patch.object(worker, "mark_plaid_item_success") as mark_success, \
                     patch.object(worker, "mark_plaid_item_error") as mark_error, \
                     patch.object(worker, "upsert_finance_account") as upsert:
                    collected, errors = worker.collect_plaid_snapshot_items()
                self.assertEqual(collected, [])
                self.assertEqual(len(errors), 1)
                mark_success.assert_not_called()
                upsert.assert_not_called()
                mark_error.assert_called_once_with("item-1", errors[0])
                data = evidence(snapshot=False)
                data["items"][0]["last_error"] = errors[0]
                result = assess(data)
                self.assertEqual(result["readiness"], "BLOCKED")
                self.assertIn(expected_code, [issue["code"] for issue in result["snapshot"]["blocking_conditions"]])

    def test_provider_failure_with_appended_missing_account_is_not_verified_absence(self):
        data = evidence(snapshot=False)
        data["items"][0]["last_error"] = (
            "Bank (connection item-1): INSTITUTION_DOWN; "
            "missing included account plaid-1 (Checking)"
        )
        result = assess(data)
        self.assertEqual(result["accounts"][0]["health"], "WARNING")
        self.assertEqual(result["readiness"], "UNKNOWN")
        self.assertNotIn("PLAID_ACCOUNT_MISSING", [i["code"] for i in result["snapshot"]["blocking_conditions"]])

    def test_other_accounts_worker_failure_is_not_attributed_to_this_account(self):
        data = evidence(snapshot=False)
        data["items"][0]["last_error"] = "Bank (connection item-1): missing included account other-id (Other)"
        result = assess(data)
        self.assertEqual(result["accounts"][0]["health"], "WARNING")
        self.assertEqual(result["readiness"], "UNKNOWN")

    def test_missing_connection_is_blocked(self):
        data = evidence(snapshot=False)
        data["items"] = []
        result = assess(data)
        self.assertEqual(result["readiness"], "BLOCKED")
        self.assertEqual(result["accounts"][0]["health"], "BLOCKED")

    def test_temporary_failure_is_not_reauthentication(self):
        data = evidence()
        data["items"][0]["last_error"] = "INSTITUTION_DOWN"
        result = assess(data)
        self.assertEqual(result["accounts"][0]["health"], "WARNING")
        self.assertEqual(result["readiness"], "WARNING")
        self.assertNotIn("PLAID_REAUTH_REQUIRED", [i["code"] for i in result["actionable_issues"]])

    def test_unknown_plaid_freshness_and_no_current_snapshot(self):
        data = evidence(snapshot=False)
        data["items"][0]["last_success_at"] = None
        result = assess(data)
        self.assertEqual(result["accounts"][0]["health"], "UNKNOWN")
        self.assertEqual(result["readiness"], "UNKNOWN")
        self.assertFalse(result["sufficient_evidence"])

    def test_completed_snapshot_proves_collection_when_item_timestamp_is_missing(self):
        data = evidence()
        data["items"][0]["last_success_at"] = None
        result = assess(data)
        self.assertEqual(result["accounts"][0]["health"], "HEALTHY")
        self.assertEqual(result["accounts"][0]["last_successful_update"], NOW - timedelta(hours=2))

    def test_empty_inventory_is_indeterminate(self):
        data = evidence(plaid=False, manual=False, snapshot=False)
        self.assertEqual(assess(data)["readiness"], "UNKNOWN")

    def test_manual_account_and_review_due(self):
        data = evidence(plaid=False, manual=True, snapshot=False)
        fresh = assess(data)
        self.assertEqual(fresh["accounts"][0]["health"], "HEALTHY")
        self.assertEqual(fresh["readiness"], "READY")
        data["manual"][0]["as_of_date"] = NOW.date() - timedelta(days=91)
        stale = assess(data)
        self.assertEqual(stale["accounts"][0]["health"], "WARNING")
        self.assertEqual(stale["readiness"], "WARNING")

    def test_manual_missing_value_blocks_but_zero_is_valid(self):
        data = evidence(plaid=False, manual=True, snapshot=False)
        data["manual"][0]["current_value"] = None
        self.assertEqual(assess(data)["readiness"], "BLOCKED")
        data["manual"][0]["current_value"] = Decimal("0")
        self.assertEqual(assess(data)["readiness"], "READY")

    def test_vehicle_uses_distinct_review_policy_and_no_monthly_change_rule(self):
        data = evidence(plaid=False, manual=True, snapshot=False)
        data["manual"][0]["asset_category"] = "vehicle"
        data["manual"][0]["valuation_key"] = "vehicle_telluride"
        data["manual"][0]["as_of_date"] = NOW.date() - timedelta(days=100)
        result = assess(data)
        self.assertEqual(result["accounts"][0]["source_type"], "vehicle")
        self.assertEqual(result["accounts"][0]["health"], "HEALTHY")
        data["manual"][0]["as_of_date"] = NOW.date() - timedelta(days=181)
        self.assertEqual(assess(data)["accounts"][0]["health"], "WARNING")

    def test_invalid_vehicle_fallback_is_nonblocking_when_live_valuation_covers_it(self):
        data = evidence(plaid=False, manual=True)
        data["manual"][0]["asset_category"] = "vehicle"
        data["manual"][0]["valuation_key"] = "vehicle_telluride"
        data["manual"][0]["current_value"] = None
        data["snapshot_items"] = [{"source": "kbb", "source_account_id": "vehicle_telluride",
                                  "valuation_key": "vehicle_telluride", "value": Decimal("12000") }]
        with patch.dict(worker.finance_config, {"vehicle_kia_url": "configured"}, clear=True):
            result = assess(data)
        self.assertEqual(result["accounts"][0]["health"], "WARNING")
        self.assertEqual(result["readiness"], "WARNING")
        self.assertNotIn("MANUAL_VALUE_MISSING", [i["code"] for i in result["snapshot"]["blocking_conditions"]])

    def test_provider_only_vehicle_is_reported_from_successful_snapshot(self):
        data = evidence(plaid=False)
        data["snapshot_items"] = [{"source": "kbb", "source_account_id": "vehicle_ram",
                                  "valuation_key": "vehicle_ram", "asset_category": "vehicle",
                                  "value": Decimal("20000") }]
        result = assess(data)
        self.assertEqual(result["accounts"][0]["source_type"], "vehicle")
        self.assertEqual(result["accounts"][0]["account_id"], "vehicle_ram")
        self.assertEqual(result["accounts"][0]["health"], "HEALTHY")

    def test_live_vehicle_with_excluded_keyed_manual_asset_is_reported(self):
        data = evidence(plaid=False, manual=True)
        data["manual"][0]["asset_category"] = "vehicle"
        data["manual"][0]["valuation_key"] = "vehicle_ram"
        data["manual"][0]["include_in_net_worth"] = False
        data["snapshot_items"] = [{"source": "kbb", "source_account_id": "vehicle_ram",
                                  "valuation_key": "vehicle_ram", "label": "RAM valuation",
                                  "asset_category": "vehicle", "value": Decimal("20000") }]
        result = assess(data)
        self.assertEqual(len(result["accounts"]), 2)
        provider = next(account for account in result["accounts"] if account["account_id"] == "vehicle_ram")
        self.assertTrue(provider["included_in_net_worth"])
        self.assertEqual(provider["display_name"], "RAM valuation")
        self.assertEqual(provider["health"], "HEALTHY")
        self.assertEqual(result["counts"]["healthy"], 2)

    def test_uncollected_live_valuation_without_fallback_is_indeterminate(self):
        data = evidence(plaid=False, snapshot=False)
        with patch.dict(worker.finance_config, {"vehicle_ram_url": "configured"}, clear=True):
            result = assess(data)
        self.assertEqual(result["readiness"], "UNKNOWN")

    def test_invalid_configured_fallback_without_live_result_is_warning_and_unknown(self):
        data = evidence(plaid=False, manual=True, snapshot=False)
        data["manual"][0]["valuation_key"] = "vehicle_ram"
        data["manual"][0]["current_value"] = None
        with patch.dict(worker.finance_config, {"vehicle_ram_url": "configured"}, clear=True):
            result = assess(data)
        self.assertEqual(result["accounts"][0]["health"], "WARNING")
        self.assertEqual(result["readiness"], "UNKNOWN")

    def test_unknown_manual_date_is_not_healthy(self):
        data = evidence(plaid=False, manual=True, snapshot=False)
        data["manual"][0]["as_of_date"] = None
        self.assertEqual(assess(data)["accounts"][0]["health"], "UNKNOWN")

    def test_existing_completeness_decision_is_used(self):
        data = evidence()
        with patch.object(worker, "_get_configured_plaid_accounts", return_value=[
                {"source_account_id": "other-required", "include_in_net_worth": True}]), \
             patch.object(worker, "get_manual_assets", return_value=[]), \
             patch.dict(worker.finance_config, {}, clear=True):
            result = assess(data, check=worker._complete_snapshot_items)
        self.assertEqual(result["readiness"], "BLOCKED")
        self.assertIn("SNAPSHOT_INCOMPLETE", [i["code"] for i in result["snapshot"]["blocking_conditions"]])

    def test_incomplete_history_never_establishes_ready(self):
        data = evidence()
        data["snapshot"]["status"] = "partial_success"
        self.assertEqual(assess(data)["readiness"], "UNKNOWN")
        data = evidence(snapshot=False)
        self.assertEqual(assess(data)["readiness"], "UNKNOWN")

    def test_mixed_account_states_and_counts(self):
        data = evidence(manual=True)
        data["manual"].append(manual_asset(id="manual-2", label="Old", as_of_date=NOW.date() - timedelta(days=100)))
        data["manual"].append(manual_asset(id="manual-3", label="Unknown", as_of_date=None))
        data["manual"].append(manual_asset(id="manual-4", label="Missing", current_value=None))
        result = assess(data)
        self.assertEqual(result["counts"], {"healthy": 2, "warning": 1, "blocked": 1, "unknown": 1})
        self.assertEqual(result["readiness"], "BLOCKED")

    def test_read_evidence_uses_selects_only_and_no_providers(self):
        class Cursor:
            description = [("id",)]
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def execute(self, sql, _params=()):
                self.outer.sql.append(sql.strip())
            def fetchall(self): return []
        class Connection:
            def cursor(self):
                cursor = Cursor()
                cursor.outer = self
                return cursor
        connection = Connection()
        connection.sql = []
        with patch.object(health.finance_service, "get_finance_accounts", return_value=[]), \
             patch.object(health, "get_db_conn", return_value=connection), \
             patch.object(health, "put_db_conn"), \
             patch.object(worker, "get_plaid_client", side_effect=AssertionError("provider called")), \
             patch.object(worker, "create_monthly_finance_snapshot", side_effect=AssertionError("writer called")):
            result = health.read_evidence()
        self.assertEqual(result["accounts"], [])
        self.assertTrue(all(sql.upper().startswith("SELECT") for sql in connection.sql))


class PreflightRouteTests(unittest.TestCase):
    def test_admin_only_preflight_contract(self):
        app = FastAPI()
        app.include_router(finance.router, dependencies=[Depends(get_current_principal)])
        route = next(route for route in app.routes if getattr(route, "path", None) == "/finance/preflight")
        self.assertEqual(route.methods, {"GET"})
        self.assertIn(require_admin_principal, {dependency.call for dependency in route.dependant.dependencies})
        with self.assertRaises(HTTPException) as unauthenticated:
            require_current_principal(None)
        self.assertEqual(unauthenticated.exception.status_code, 401)
        member = AuthenticatedPrincipal("1", "member", "member@example.test", None, "member")
        with self.assertRaises(HTTPException) as forbidden:
            require_admin_principal(member)
        self.assertEqual(forbidden.exception.status_code, 403)
        with patch.object(finance.finance_health_service, "assess_finance_health", return_value=assess(evidence())) as service:
            response = finance.get_preflight()
        self.assertEqual(response["data"]["readiness"], "READY")
        service.assert_called_once_with()
        self.assertIn("FinanceHealthResponse", app.openapi()["components"]["schemas"])

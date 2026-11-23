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
from backend.models.finance_models import FinanceHealthResponse
original_database = sys.modules.get("backend.database.database")
fake_database = types.ModuleType("backend.database.database")
fake_database.get_db_conn = lambda: None
fake_database.put_db_conn = lambda _connection: None
sys.modules["backend.database.database"] = fake_database
try:
    finance = importlib.import_module("backend.routers.finance")
    health = importlib.import_module("backend.services.finance_health_service")
    verification = importlib.import_module("backend.services.finance_verification_service")
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
        self.assertEqual(result["accounts"][0]["health"], "UNKNOWN")
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

    def test_cached_inventory_does_not_clear_prior_login_required_error(self):
        data = evidence()
        item = data["items"][0]
        item.update(last_error="ITEM_LOGIN_REQUIRED", updated_at=NOW - timedelta(hours=2),
                    verification_attempted_at=NOW - timedelta(hours=1),
                    verification_succeeded_at=NOW - timedelta(hours=1),
                    verification_method="accounts_get", verification_outcome="VERIFIED",
                    connection_status="UNKNOWN", inventory_status="AVAILABLE",
                    available_account_ids=["plaid-1"])
        account = assess(data)["accounts"][0]
        self.assertEqual(account["health"], "BLOCKED")
        self.assertEqual(account["verification"]["connection_status"], "AUTH_REQUIRED")
        self.assertEqual(account["verification"]["collection_readiness"], "BLOCKED")
        self.assertIn("PLAID_REAUTH_REQUIRED", {issue["code"] for issue in account["issues"]})

        item.update(verification_method="item_get+accounts_get", connection_status="VERIFIED")
        account = assess(data)["accounts"][0]
        self.assertEqual(account["health"], "HEALTHY")
        self.assertNotIn("PLAID_REAUTH_REQUIRED", {issue["code"] for issue in account["issues"]})

        item.update(updated_at=NOW, verification_succeeded_at=NOW - timedelta(hours=1))
        self.assertEqual(assess(data)["accounts"][0]["health"], "BLOCKED")
        item.update(last_error=None, connection_status="UNKNOWN", verification_method="accounts_get")
        self.assertNotIn("PLAID_REAUTH_REQUIRED", {issue["code"] for issue in assess(data)["accounts"][0]["issues"]})

    def test_stale_plaid_connection_warns_without_balance_change_heuristic(self):
        data = evidence(snapshot=False)
        data["items"][0]["last_success_at"] = NOW - timedelta(days=8)
        result = assess(data)
        self.assertEqual(result["accounts"][0]["health"], "UNKNOWN")
        self.assertEqual(result["readiness"], "UNKNOWN")
        self.assertEqual(result["accounts"][0]["freshness"], "UNKNOWN")

    def test_recent_item_success_does_not_refresh_stale_account_collection(self):
        data = evidence()
        data["snapshot"]["created_at"] = NOW - timedelta(days=8)
        data["items"][0]["last_success_at"] = NOW - timedelta(hours=1)
        result = assess(data)
        account = result["accounts"][0]
        self.assertEqual(account["last_successful_update"], data["snapshot"]["created_at"])
        self.assertEqual(account["freshness"], "CURRENT")
        self.assertEqual(account["health"], "UNKNOWN")
        self.assertEqual(result["readiness"], "READY")

    def test_recent_item_success_without_account_proof_is_unknown(self):
        result = assess(evidence(snapshot=False))
        self.assertEqual(result["accounts"][0]["freshness"], "UNKNOWN")
        self.assertEqual(result["accounts"][0]["health"], "UNKNOWN")

    def test_cached_inventory_verifies_account_but_not_live_balance(self):
        data = evidence()
        data["items"][0].update(connection_status="VERIFIED", inventory_status="AVAILABLE",
                                 verification_outcome="VERIFIED", available_account_ids=["plaid-1"],
                                 verification_attempted_at=NOW, verification_succeeded_at=NOW,
                                 verification_method="item_get+accounts_get", live_balance_tested=False)
        account = assess(data)["accounts"][0]
        self.assertEqual(account["health"], "HEALTHY")
        self.assertEqual(account["verification"]["collection_readiness"], "CACHED_INVENTORY_AVAILABLE")
        self.assertFalse(account["verification"]["live_balance_tested"])

    def test_recent_cached_only_verification_is_healthy_without_snapshot(self):
        data = evidence(snapshot=False)
        data["items"][0].update(connection_status="UNKNOWN", inventory_status="AVAILABLE",
                                 verification_outcome="VERIFIED", available_account_ids=["plaid-1"],
                                 missing_account_ids=[], invalid_balance_ids=[],
                                 verification_attempted_at=NOW, verification_succeeded_at=NOW,
                                 verification_method="accounts_get", live_balance_tested=False)
        result = assess(data)
        account = result["accounts"][0]
        self.assertEqual(account["health"], "HEALTHY")
        self.assertEqual(account["issues"], [])
        self.assertIsNone(account["recommended_action"])
        self.assertNotIn("not been verified", account["explanation"])
        self.assertEqual(result["counts"]["healthy"], 1)
        self.assertEqual(account["freshness"], "UNKNOWN")  # No monthly collection occurred.
        self.assertEqual(account["verification"]["outcome"], "VERIFIED")
        self.assertEqual(account["verification"]["connection_status"], "UNKNOWN")
        self.assertEqual(account["verification"]["inventory_status"], "AVAILABLE")
        self.assertEqual(account["verification"]["cached_balance_status"], "VALID")
        self.assertEqual(account["verification"]["collection_readiness"], "CACHED_INVENTORY_AVAILABLE")
        self.assertEqual(account["verification"]["method"], "accounts_get")
        self.assertFalse(account["verification"]["live_balance_tested"])
        self.assertEqual(account["verification"]["succeeded_at"], NOW)

        # A repeated monthly value is not a health warning.
        data["snapshot"] = evidence()["snapshot"]
        data["snapshot_items"] = evidence()["snapshot_items"]
        self.assertEqual(assess(data)["accounts"][0]["health"], "HEALTHY")
        self.assertEqual(assess(data)["accounts"][0]["issues"], [])

    def test_cached_only_health_needs_recent_complete_successful_evidence(self):
        data = evidence(snapshot=False)
        item = data["items"][0]
        item.update(connection_status="UNKNOWN", inventory_status="AVAILABLE",
                    verification_outcome="VERIFIED", available_account_ids=["plaid-1"],
                    verification_attempted_at=NOW, verification_succeeded_at=NOW,
                    verification_method="accounts_get")
        for change in (
                {"verification_attempted_at": NOW - timedelta(hours=13),
                 "verification_succeeded_at": NOW - timedelta(hours=13)},
                {"verification_attempted_at": NOW, "verification_succeeded_at": None},
                {"verification_attempted_at": NOW, "verification_succeeded_at": NOW,
                 "verification_method": "item_get"},
                {"verification_attempted_at": NOW, "verification_succeeded_at": NOW,
                 "verification_method": "accounts_get", "verification_outcome": "TRANSIENT"}):
            with self.subTest(change=change):
                item.update(change)
                account = assess(data)["accounts"][0]
                self.assertEqual(account["health"], "UNKNOWN")
                self.assertIn("PLAID_VERIFICATION_UNKNOWN", {issue["code"] for issue in account["issues"]})

    def test_cached_only_success_does_not_hide_ownership_or_collection_warning(self):
        data = evidence(snapshot=False)
        item = data["items"][0]
        item.update(connection_status="UNKNOWN", inventory_status="AVAILABLE",
                    verification_outcome="VERIFIED", available_account_ids=["plaid-1"],
                    verification_attempted_at=NOW, verification_succeeded_at=NOW,
                    verification_method="accounts_get")
        item["last_error"] = "Bank (connection item-1): temporary collection problem"
        item["updated_at"] = NOW + timedelta(minutes=1)
        account = assess(data)["accounts"][0]
        self.assertEqual(account["health"], "WARNING")
        self.assertIn("PLAID_COLLECTION_ERROR", {issue["code"] for issue in account["issues"]})

        item["last_error"] = None
        data["items"].append({"id": "item-2", "label": "Bank", "enabled": True})
        account = assess(data)["accounts"][0]
        self.assertEqual(account["health"], "UNKNOWN")
        self.assertIn("PLAID_OWNERSHIP_UNKNOWN", {issue["code"] for issue in account["issues"]})

    def test_invalid_cached_balance_only_blocks_affected_account(self):
        data = evidence(snapshot=False)
        data["accounts"].append(plaid_account(id="account-2", source_account_id="plaid-2",
                                               name="Savings", display_name="Savings"))
        data["items"][0].update(connection_status="UNKNOWN", inventory_status="AVAILABLE",
                                 verification_outcome="VERIFIED", available_account_ids=["plaid-1", "plaid-2"],
                                 invalid_balance_ids=["plaid-1"], verification_attempted_at=NOW,
                                 verification_succeeded_at=NOW, verification_method="accounts_get")
        accounts = {account["account_id"]: account for account in assess(data)["accounts"]}
        self.assertEqual(accounts["account-1"]["verification"]["cached_balance_status"], "INVALID")
        self.assertEqual(accounts["account-1"]["verification"]["collection_readiness"], "BLOCKED")
        self.assertEqual(accounts["account-1"]["health"], "BLOCKED")
        self.assertEqual(accounts["account-2"]["verification"]["cached_balance_status"], "VALID")
        self.assertEqual(accounts["account-2"]["verification"]["collection_readiness"], "CACHED_INVENTORY_AVAILABLE")
        self.assertEqual(accounts["account-2"]["health"], "HEALTHY")

    def test_missing_account_only_blocks_its_own_cached_readiness(self):
        data = evidence(snapshot=False)
        data["accounts"].append(plaid_account(id="account-2", source_account_id="plaid-2"))
        data["items"][0].update(connection_status="UNKNOWN", inventory_status="MISSING",
                                 verification_outcome="VERIFIED", available_account_ids=["plaid-2"],
                                 missing_account_ids=["plaid-1"], invalid_balance_ids=[],
                                 verification_attempted_at=NOW, verification_succeeded_at=NOW,
                                 verification_method="accounts_get")
        accounts = {account["account_id"]: account for account in assess(data)["accounts"]}
        missing = accounts["account-1"]
        available = accounts["account-2"]
        self.assertEqual(missing["verification"]["inventory_status"], "MISSING")
        self.assertEqual(missing["verification"]["collection_readiness"], "BLOCKED")
        self.assertEqual(missing["health"], "BLOCKED")
        self.assertEqual(available["verification"]["item_inventory_status"], "MISSING")
        self.assertEqual(available["verification"]["inventory_status"], "AVAILABLE")
        self.assertEqual(available["verification"]["cached_balance_status"], "VALID")
        self.assertEqual(available["verification"]["collection_readiness"], "CACHED_INVENTORY_AVAILABLE")
        self.assertEqual(available["health"], "HEALTHY")

    def test_verified_missing_account_overrides_historical_snapshot(self):
        data = evidence()
        data["items"][0].update(connection_status="VERIFIED", inventory_status="MISSING",
                                 verification_outcome="VERIFIED", missing_account_ids=["plaid-1"])
        account = assess(data)["accounts"][0]
        self.assertEqual(account["health"], "BLOCKED")
        self.assertIn("PLAID_ACCOUNT_MISSING", {issue["code"] for issue in account["issues"]})

    def test_november_collection_age_does_not_create_warning(self):
        data = evidence()
        data["snapshot"]["created_at"] = datetime(2026, 11, 1, tzinfo=timezone.utc)
        result = assess(data)
        self.assertEqual(result["accounts"][0]["last_successful_update"], data["snapshot"]["created_at"])
        self.assertEqual(result["accounts"][0]["health"], "UNKNOWN")
        self.assertNotIn("PLAID_DATA_STALE", {issue["code"] for issue in result["actionable_issues"]})

    def test_prior_month_collection_timestamp_is_preserved(self):
        data = evidence()
        collected_at = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        data["snapshot"]["created_at"] = collected_at
        data["last_collections"] = [{"source_account_id": "plaid-1",
                                     "collected_at": collected_at,
                                     "collected_month": date(2026, 11, 1)}]
        result = health.assess_evidence(
            data, now=datetime(2026, 12, 2, tzinfo=timezone.utc),
            config={}, completeness_check=lambda _p, _v: [],
            calendar_date=date(2026, 12, 2))
        account = result["accounts"][0]
        self.assertEqual(account["last_monthly_balance_collection"], collected_at)
        self.assertEqual(account["freshness"], "STALE")
        self.assertNotIn("PLAID_DATA_STALE", {issue["code"] for issue in account["issues"]})

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
        self.assertEqual(result["accounts"][0]["health"], "UNKNOWN")
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
        self.assertEqual(result["counts"], {"healthy": 1, "warning": 1, "blocked": 1, "unknown": 2})
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

    def test_preflight_response_accepts_cached_only_healthy_account(self):
        data = evidence(snapshot=False)
        data["items"][0].update(connection_status="UNKNOWN", inventory_status="AVAILABLE",
                                 verification_outcome="VERIFIED", available_account_ids=["plaid-1"],
                                 verification_attempted_at=NOW, verification_succeeded_at=NOW,
                                 verification_method="accounts_get")
        with patch.object(finance.finance_health_service, "assess_finance_health",
                          return_value=assess(data)):
            response = FinanceHealthResponse.model_validate(finance.get_preflight())
        self.assertEqual(response.data.accounts[0].health, "HEALTHY")
        self.assertEqual(response.data.accounts[0].verification.connection_status, "UNKNOWN")


class LightweightVerificationTests(unittest.TestCase):
    def test_cached_result_persistence_preserves_confirmed_auth(self):
        conn = MagicMock()
        result = {"outcome": "VERIFIED", "connection_status": "UNKNOWN",
                  "inventory_status": "AVAILABLE", "available_account_ids": ["one"],
                  "missing_account_ids": [], "invalid_balance_ids": [], "method": "accounts_get"}
        with patch.object(verification, "get_db_conn", return_value=conn), \
             patch.object(verification, "put_db_conn") as release:
            verification._persist("item-1", result, NOW)
        sql = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
        self.assertIn("connection_status = CASE", sql)
        self.assertIn("public.finance_plaid_verification.connection_status = 'AUTH_REQUIRED'", sql)
        self.assertIn("EXCLUDED.method = 'accounts_get'", sql)
        self.assertIn("EXCLUDED.connection_status = 'UNKNOWN'", sql)
        self.assertIn("THEN public.finance_plaid_verification.failure_at", sql)
        conn.commit.assert_called_once()
        release.assert_called_once_with(conn)

    def test_default_free_cached_check_does_not_call_item_get(self):
        client = MagicMock()
        client.accounts_get.return_value.to_dict.return_value = {
            "accounts": [{"account_id": "one", "balances": {"current": 10}}]}
        item = {"environment": "sandbox", "access_token_encrypted": "encrypted"}
        with patch.object(worker, "get_plaid_client", return_value=client), \
             patch.object(worker, "decrypt_access_token", return_value="token"):
            result = verification._check_item(item, {"one"})
        client.item_get.assert_not_called()
        self.assertEqual(result["connection_status"], "UNKNOWN")
        self.assertEqual(result["inventory_status"], "AVAILABLE")
        self.assertEqual(result["method"], "accounts_get")

    def test_cached_balance_validator_accepts_zero_and_rejects_bad_values(self):
        client = MagicMock()
        item = {"environment": "sandbox", "access_token_encrypted": "encrypted"}
        with patch.object(worker, "get_plaid_client", return_value=client), \
             patch.object(worker, "decrypt_access_token", return_value="token"):
            for value, invalid in ((0, []), (None, ["one"]),
                                   (float("nan"), ["one"]), (float("inf"), ["one"])):
                with self.subTest(value=value):
                    client.accounts_get.return_value.to_dict.return_value = {
                        "accounts": [{"account_id": "one", "balances": {"current": value}}]}
                    result = verification._check_item(item, {"one"})
                    self.assertEqual(result["outcome"], "VERIFIED")
                    self.assertEqual(result["invalid_balance_ids"], invalid)
                    if value == 0:
                        data = evidence(snapshot=False)
                        data["accounts"][0]["source_account_id"] = "one"
                        data["items"][0].update(
                            verification_outcome=result["outcome"],
                            connection_status=result["connection_status"],
                            inventory_status=result["inventory_status"],
                            available_account_ids=result["available_account_ids"],
                            invalid_balance_ids=result["invalid_balance_ids"],
                            verification_method=result["method"],
                            verification_attempted_at=NOW,
                            verification_succeeded_at=NOW)
                        self.assertEqual(assess(data)["accounts"][0]["health"], "HEALTHY")
        client.item_get.assert_not_called()
        client.accounts_balance_get.assert_not_called()

    def test_november_reminder_days_request_fresh_verification(self):
        for day, priority in ((27, False), (28, True), (29, False), (30, True)):
            with self.subTest(day=day), patch.object(verification, "_claim", return_value=False) as claim:
                verification.verify_enabled_items(
                    now=datetime(2026, 11, day, 18, tzinfo=timezone.utc))
                self.assertEqual(claim.call_args.kwargs["reminder_day"], priority)

    def test_existing_lease_skips_duplicate_cycle(self):
        with patch.object(verification, "_claim", return_value=False), \
             patch.object(worker, "get_enabled_plaid_items") as items:
            self.assertFalse(verification.verify_enabled_items(now=NOW))
        items.assert_not_called()

    def test_failed_inventory_read_releases_lease(self):
        with patch.object(verification, "_claim", return_value=True), \
             patch.object(verification, "_release") as release, \
             patch.object(worker, "get_enabled_plaid_items", side_effect=RuntimeError("storage")):
            with self.assertRaises(RuntimeError):
                verification.verify_enabled_items(now=NOW)
        release.assert_called_once()

    def test_shared_item_checks_once_and_never_calls_live_balance(self):
        client = MagicMock()
        client.item_get.return_value.to_dict.return_value = {"item": {"error": None}}
        client.accounts_get.return_value.to_dict.return_value = {
            "accounts": [{"account_id": "one", "balances": {"current": 10}},
                         {"account_id": "two", "balances": {"current": 20}}]}
        item = {"id": "item-1", "label": "Bank", "environment": "sandbox",
                "access_token_encrypted": "encrypted"}
        accounts = [{"source_account_id": account_id, "institution_label": "Bank",
                     "include_in_net_worth": True} for account_id in ("one", "two")]
        with patch.object(verification, "_claim", return_value=True), \
             patch.object(verification, "_renew", return_value=True), \
             patch.object(verification, "_release") as release, \
             patch.object(verification, "_persist") as persist, \
             patch.object(worker, "get_enabled_plaid_items", return_value=[item]), \
             patch.object(worker, "_get_configured_plaid_accounts", return_value=accounts), \
             patch.object(worker, "finance_config", {"health_plaid_item_get_enabled": "true"}), \
             patch.object(worker, "get_plaid_client", return_value=client), \
             patch.object(worker, "decrypt_access_token", return_value="token"):
            self.assertTrue(verification.verify_enabled_items(now=NOW))
        client.item_get.assert_called_once()
        client.accounts_get.assert_called_once()
        client.accounts_balance_get.assert_not_called()
        self.assertEqual(persist.call_args.args[1]["inventory_status"], "AVAILABLE")
        self.assertEqual(persist.call_args.args[1]["invalid_balance_ids"], [])
        release.assert_called_once()

    def test_invalid_cached_balance_is_reported_without_writing_value(self):
        client = MagicMock()
        client.item_get.return_value.to_dict.return_value = {"item": {"error": None}}
        client.accounts_get.return_value.to_dict.return_value = {
            "accounts": [{"account_id": "one", "balances": {"current": None}}]}
        item = {"environment": "sandbox", "access_token_encrypted": "encrypted"}
        with patch.object(worker, "get_plaid_client", return_value=client), \
             patch.object(worker, "decrypt_access_token", return_value="token"):
            result = verification._check_item(item, {"one"}, check_item_status=True)
        self.assertEqual(result["inventory_status"], "AVAILABLE")
        self.assertEqual(result["invalid_balance_ids"], ["one"])
        client.accounts_balance_get.assert_not_called()

    def test_item_authentication_failure_skips_inventory(self):
        client = MagicMock()
        client.item_get.return_value.to_dict.return_value = {
            "item": {"error": {"error_code": "ITEM_LOGIN_REQUIRED"}}}
        item = {"environment": "sandbox", "access_token_encrypted": "encrypted"}
        with patch.object(worker, "get_plaid_client", return_value=client), \
             patch.object(worker, "decrypt_access_token", return_value="token"):
            result = verification._check_item(item, {"one"}, check_item_status=True)
        self.assertEqual(result["connection_status"], "AUTH_REQUIRED")
        client.accounts_get.assert_not_called()

    def test_transient_failure_does_not_assert_missing_account(self):
        client = MagicMock()
        client.item_get.side_effect = TimeoutError()
        item = {"environment": "sandbox", "access_token_encrypted": "encrypted"}
        with patch.object(worker, "get_plaid_client", return_value=client), \
             patch.object(worker, "decrypt_access_token", return_value="token"):
            result = verification._check_item(item, {"one"}, check_item_status=True)
        self.assertEqual(result["outcome"], "TRANSIENT")
        self.assertEqual(result["missing_account_ids"], [])

"""Offline API and execution tests for manual Finance verification."""

from datetime import datetime, timedelta, timezone
import importlib
import os
import sys
import types
import unittest
from unittest import TestCase
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.core.auth import AuthenticatedPrincipal, require_admin_principal, require_current_principal
from backend.models.finance_models import (FinanceVerificationExecutionResponse,
                                           FinanceVerificationRunRequest)
original_database = sys.modules.get("backend.database.database")
fake_database = types.ModuleType("backend.database.database")
fake_database.get_db_conn = lambda: None
fake_database.put_db_conn = lambda _connection: None
sys.modules["backend.database.database"] = fake_database
try:
    finance = importlib.import_module("backend.routers.finance")
    manual = importlib.import_module("backend.services.finance_manual_verification_service")
    verification = importlib.import_module("backend.services.finance_verification_service")
finally:
    if original_database is None:
        sys.modules.pop("backend.database.database", None)
    else:
        sys.modules["backend.database.database"] = original_database


NOW = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
ADMIN = AuthenticatedPrincipal("1", "admin", "admin@example.test", None, "admin")
MEMBER = AuthenticatedPrincipal("2", "member", "member@example.test", None, "member")


@unittest.skipUnless(os.environ.get("REMIHUB_HTTP_TESTS") == "1",
                     "HTTP TestClient needs a local socket in this sandbox")
class ManualRouteTests(TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(finance.router)
        self.client = TestClient(self.app)

    def test_admin_queues_and_reads_run(self):
        self.app.dependency_overrides[require_admin_principal] = lambda: ADMIN
        run_id = str(uuid4())
        data = {"execution_id": run_id, "trigger": "manual", "status": "pending",
                "requested_at": NOW, "reused": False, "summary": {},
                "results": {"connections": [], "accounts": []}}
        with patch.object(manual, "request_run", return_value=("new", data, "owner")), \
             patch.object(manual, "run_manual") as execute, \
             patch.object(manual, "get_run", return_value=data):
            response = self.client.post("/finance/verification/run", json={})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["data"]["execution_id"], run_id)
            execute.assert_called_once()
            self.assertEqual(self.client.get(f"/finance/verification/runs/{run_id}").status_code, 200)

    def test_unauthenticated_and_nonadmin_rejected(self):
        self.assertEqual(self.client.post("/finance/verification/run", json={}).status_code, 401)
        self.assertEqual(self.client.get(f"/finance/verification/runs/{uuid4()}").status_code, 401)
        self.app.dependency_overrides[require_current_principal] = lambda: MEMBER
        self.assertEqual(self.client.post("/finance/verification/run", json={}).status_code, 403)

    def test_client_cannot_supply_token_method_or_force(self):
        self.app.dependency_overrides[require_admin_principal] = lambda: ADMIN
        with patch.object(manual, "request_run") as claim:
            for body in ({"access_token": "secret"}, {"method": "accounts/balance/get"},
                         {"force": True}, {"account_id": "other"}):
                self.assertEqual(self.client.post("/finance/verification/run", json=body).status_code, 422)
        claim.assert_not_called()

    def test_cooldown_is_visible(self):
        self.app.dependency_overrides[require_admin_principal] = lambda: ADMIN
        with patch.object(manual, "request_run", return_value=(
                "cooldown", {"retry_at": NOW + timedelta(minutes=30)}, None)):
            response = self.client.post("/finance/verification/run", json={})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["detail"]["code"], "FINANCE_VERIFICATION_COOLDOWN")
        self.assertIn("retry_at", response.json()["detail"])


class SharedVerifierTests(TestCase):
    def test_execution_account_label_uses_only_short_mask(self):
        self.assertEqual(manual._safe_account_label({"institution_label": "Bank",
                                                     "mask": "1234"}), "Bank ••••1234")
        self.assertEqual(manual._safe_account_label({"institution_label": "Bank",
                                                     "mask": "123456789"}), "Bank")

    def test_direct_route_queues_only_new_work_and_reports_cooldown(self):
        run_id = str(uuid4())
        data = {"execution_id": run_id, "trigger": "manual", "status": "pending",
                "requested_at": NOW, "results": {"connections": [], "accounts": []}}
        tasks = BackgroundTasks()
        with patch.object(manual, "request_run", return_value=("new", data, "owner")):
            response = finance.run_verification(FinanceVerificationRunRequest(), tasks)
        self.assertEqual(response["data"]["execution_id"], run_id)
        self.assertEqual(len(tasks.tasks), 1)
        FinanceVerificationExecutionResponse.model_validate(response)
        with patch.object(manual, "request_run", return_value=("existing", data, None)):
            finance.run_verification(FinanceVerificationRunRequest(), tasks)
        self.assertEqual(len(tasks.tasks), 2)
        self.assertIs(tasks.tasks[1].func, manual._recover_scheduled_handoff)
        with patch.object(manual, "request_run", return_value=(
                "cooldown", {"retry_at": NOW + timedelta(minutes=30)}, None)):
            with self.assertRaises(HTTPException) as rejected:
                finance.run_verification(FinanceVerificationRunRequest(), tasks)
        self.assertEqual(rejected.exception.status_code, 429)
        status_tasks = BackgroundTasks()
        with patch.object(manual, "get_run", return_value=data):
            finance.get_verification_run(uuid4(), status_tasks)
        self.assertEqual(len(status_tasks.tasks), 1)
        self.assertIs(status_tasks.tasks[0].func, manual._recover_scheduled_handoff)

    def test_atomic_manual_claim_keeps_scheduled_clock(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [(None, None, None, None, None), None]
        with patch.object(manual, "get_db_conn", return_value=conn), \
             patch.object(manual, "put_db_conn") as release, \
             patch.object(manual, "_recover_scheduled_handoff"), \
             patch.object(manual, "_retry_scheduled_monitoring") as retry:
            outcome, data, owner = manual.request_run()
        self.assertEqual(outcome, "new")
        self.assertEqual(data["status"], "pending")
        self.assertIsNotNone(owner)
        updates = [call.args[0] for call in cursor.execute.call_args_list
                   if "UPDATE public.finance_plaid_verification_lease" in call.args[0]]
        self.assertEqual(len(updates), 1)
        self.assertNotIn("started_at =", updates[0])
        conn.commit.assert_called_once()
        release.assert_called_once_with(conn)
        retry.assert_called_once_with()

    def test_running_schedule_is_reused_without_new_manual_call(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("scheduled-owner", datetime.now(timezone.utc)
                                        + timedelta(minutes=10), "scheduled", None, NOW)
        with patch.object(manual, "get_db_conn", return_value=conn), \
             patch.object(manual, "put_db_conn"), \
             patch.object(manual, "_recover_scheduled_handoff"), \
             patch.object(manual, "_retry_scheduled_monitoring"):
            outcome, data, owner = manual.request_run()
        self.assertEqual(outcome, "existing")
        self.assertEqual(data["trigger"], "scheduled")
        self.assertTrue(data["reused"])
        self.assertIsNone(owner)
        self.assertFalse(any("INSERT INTO public.finance_verification_runs" in c.args[0]
                             for c in cursor.execute.call_args_list))

    def test_running_manual_is_reused_without_duplicate_provider_work(self):
        run_id = uuid4()
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.description = [(column,) for column in (
            "id", "run_trigger", "status", "requested_at", "started_at",
            "completed_at", "summary", "results", "error_code")]
        cursor.fetchone.side_effect = [
            ("manual-owner", datetime.now(timezone.utc) + timedelta(minutes=10),
             "manual", run_id, None),
            (run_id, "manual", "running", NOW, NOW, None, {},
             {"connections": [], "accounts": []}, None),
        ]
        with patch.object(manual, "get_db_conn", return_value=conn), \
             patch.object(manual, "put_db_conn"), \
             patch.object(manual, "_recover_scheduled_handoff"), \
             patch.object(manual, "_retry_scheduled_monitoring"):
            outcome, data, owner = manual.request_run()
        self.assertEqual((outcome, data["status"], owner), ("existing", "running", None))
        self.assertEqual(data["execution_id"], str(run_id))
        self.assertFalse(any("INSERT INTO public.finance_verification_runs" in c.args[0]
                             for c in cursor.execute.call_args_list))

    def test_manual_cooldown_is_server_enforced(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [(None, None, None, None, None),
                                       (datetime.now(timezone.utc) - timedelta(minutes=5),)]
        with patch.object(manual, "get_db_conn", return_value=conn), \
             patch.object(manual, "put_db_conn"), \
             patch.object(manual, "_recover_scheduled_handoff"), \
             patch.object(manual, "_retry_scheduled_monitoring"):
            outcome, data, owner = manual.request_run()
        self.assertEqual(outcome, "cooldown")
        self.assertIsNone(owner)
        self.assertGreater(data["retry_at"], datetime.now(timezone.utc))

    def test_new_claim_reconciles_stale_interrupted_run_after_releasing_connection(self):
        old_id = uuid4()
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            ("old-owner", datetime.now(timezone.utc) - timedelta(hours=1),
             "manual", old_id, NOW),
            (datetime.now(timezone.utc) - timedelta(minutes=5),),
        ]

        def reconcile(run_id):
            self.assertEqual(run_id, old_id)
            release.assert_called_once_with(conn)

        with patch.object(manual, "get_db_conn", return_value=conn), \
             patch.object(manual, "put_db_conn") as release, \
             patch.object(manual, "_reconcile_unmonitored_run",
                          side_effect=reconcile) as recovered, \
             patch.object(manual, "_recover_scheduled_handoff"), \
             patch.object(manual, "_retry_scheduled_monitoring"):
            outcome, _, owner = manual.request_run()
        self.assertEqual(outcome, "cooldown")
        self.assertIsNone(owner)
        recovered.assert_called_once_with(old_id)

    def test_expired_run_is_recoverably_marked_interrupted(self):
        run_id = uuid4()
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        columns = ("id", "run_trigger", "status", "requested_at", "started_at",
                   "completed_at", "summary", "results", "error_code")
        cursor.description = [(column,) for column in columns]
        pending = (run_id, "manual", "running", NOW, NOW, None, {},
                   {"connections": [], "accounts": []}, None)
        failed = (run_id, "manual", "failed", NOW, NOW, NOW + timedelta(minutes=21),
                  {}, {"connections": [], "accounts": []}, "EXECUTION_INTERRUPTED")
        cursor.fetchone.side_effect = [pending, None, failed]
        with patch.object(manual, "get_db_conn", return_value=conn), \
             patch.object(manual, "put_db_conn") as release, \
             patch.object(manual, "_reconcile_unmonitored_run") as reconcile, \
             patch.object(manual, "_recover_scheduled_handoff"), \
             patch.object(manual, "_retry_scheduled_monitoring"):
            result = manual.get_run(run_id)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "EXECUTION_INTERRUPTED")
        conn.commit.assert_called_once()
        release.assert_called_once_with(conn)
        reconcile.assert_called_once_with(run_id)

    def test_due_scheduled_verification_is_replayed_after_manual_release(self):
        issues = {"item-2": "resolved"}
        order = []

        def reconcile(*, send_reminders):
            self.assertFalse(send_reminders)
            issues["item-2"] = "active"

        def check(*, replay_generation_out, require_pending):
            self.assertTrue(require_pending)
            replay_generation_out["generation"] = 8
            order.append("checked")
            return True

        def finish(generation):
            self.assertEqual(generation, 8)
            order.append("finished")

        with patch.object(verification, "_release", return_value=True) as release, \
             patch.object(verification, "verify_enabled_items", side_effect=check) as scheduled, \
             patch.object(manual, "_finish_scheduled_monitoring_attempt",
                          side_effect=finish) as finished, \
             patch.object(manual, "_retry_scheduled_monitoring",
                          side_effect=lambda: reconcile(send_reminders=False)) as retry:
            manual._release_and_replay("manual-owner")
        release.assert_called_once_with("manual-owner")
        self.assertEqual(scheduled.call_count, 1)
        self.assertEqual(scheduled.call_args.kwargs["replay_generation_out"],
                         {"generation": 8})
        self.assertTrue(scheduled.call_args.kwargs["require_pending"])
        finished.assert_called_once_with(8)
        retry.assert_called_once_with()
        self.assertEqual(order, ["checked", "finished"])
        self.assertEqual(issues["item-2"], "active")

    def test_deferred_replay_monitoring_failure_stays_pending_for_retry(self):
        first_lookup, second_lookup, marker = (MagicMock() for _ in range(3))
        for conn in (first_lookup, second_lookup):
            conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (True, 7, None)
        issues = {"item-2": "resolved"}

        def reconcile(*, send_reminders):
            self.assertFalse(send_reminders)
            issues["item-2"] = "active"

        with patch.object(manual, "get_db_conn", side_effect=[first_lookup, second_lookup,
                                                                marker]), \
             patch.object(manual, "put_db_conn") as release, \
             patch.object(manual.finance_monitor_service, "assess_and_notify",
                          side_effect=[RuntimeError("monitor unavailable"), None]) as monitor:
            manual._retry_scheduled_monitoring()
            self.assertEqual(issues["item-2"], "resolved")
            monitor.side_effect = reconcile
            manual._retry_scheduled_monitoring()
        self.assertEqual(issues["item-2"], "active")
        self.assertEqual(monitor.call_count, 2)
        update = marker.cursor.return_value.__enter__.return_value.execute.call_args
        self.assertIn("monitoring_generation = %s", update.args[0])
        self.assertEqual(update.args[1], (7,))
        self.assertEqual(release.call_count, 3)

    def test_scheduled_claim_atomically_marks_pending_before_item_work(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (True, 8)
        claim = {}
        with patch.object(verification, "get_db_conn", return_value=conn), \
             patch.object(verification, "put_db_conn") as release:
            claimed = verification._claim(NOW, "scheduled-owner", local_day=NOW.date(),
                                          replay_generation_out=claim,
                                          require_pending=True)
        commands = conn.cursor.return_value.__enter__.return_value.execute.call_args_list
        command = next(c for c in commands if "RETURNING scheduled_pending" in c.args[0])
        sql = command.args[0]
        self.assertTrue(claimed)
        self.assertEqual(claim["generation"], 8)
        self.assertIn("monitoring_pending = monitoring_pending OR scheduled_pending", sql)
        self.assertIn("CASE WHEN scheduled_pending THEN 1 ELSE 0 END", sql)
        self.assertIn("monitoring_retry_at = CASE WHEN scheduled_pending THEN %s", sql)
        self.assertIn("scheduled_pending OR started_at IS NULL", sql)
        self.assertIn("AND (NOT %s OR scheduled_pending)", sql)
        conn.commit.assert_called_once()
        release.assert_called_once_with(conn)

    def test_released_handoff_is_replayed_after_process_stop(self):
        release_conn = MagicMock()
        release_cur = release_conn.cursor.return_value.__enter__.return_value
        release_cur.fetchone.return_value = (True, "manual")
        with patch.object(verification, "get_db_conn", return_value=release_conn), \
             patch.object(verification, "put_db_conn"):
            self.assertTrue(verification._release("manual-owner"))
        release_sql = release_cur.execute.call_args.args[0]
        self.assertIn("THEN false ELSE scheduled_pending END", release_sql)
        release_conn.commit.assert_called_once()

        with patch.object(verification, "_release", return_value=True), \
             patch.object(manual, "_run_deferred_scheduled_check",
                          side_effect=SystemExit("stopped after release")):
            with self.assertRaises(SystemExit):
                manual._release_and_replay("manual-owner")

        lookup = MagicMock()
        lookup.cursor.return_value.__enter__.return_value.fetchone.return_value = (
            True, None, None)

        def check(*, replay_generation_out, require_pending):
            self.assertTrue(require_pending)
            replay_generation_out["generation"] = 12
            return True

        with patch.object(manual, "get_db_conn", return_value=lookup), \
             patch.object(manual, "put_db_conn") as returned, \
             patch.object(verification, "verify_enabled_items", side_effect=check) as scheduled, \
             patch.object(manual, "_finish_scheduled_monitoring_attempt") as finish, \
             patch.object(manual, "_retry_scheduled_monitoring") as retry:
            manual._recover_scheduled_handoff()
        scheduled.assert_called_once()
        finish.assert_called_once_with(12)
        retry.assert_called_once_with()
        returned.assert_called_once_with(lookup)

    def test_interrupted_replay_reconciles_after_lease_without_notifications(self):
        committed = []

        def interrupted_check(*, replay_generation_out, require_pending):
            self.assertTrue(require_pending)
            replay_generation_out["generation"] = 9
            committed.append("item evidence")
            raise SystemExit("process stopped after Item commit")

        with patch.object(verification, "_release", return_value=True), \
             patch.object(verification, "verify_enabled_items",
                          side_effect=interrupted_check), \
             patch.object(manual, "_finish_scheduled_monitoring_attempt") as finish, \
             patch.object(manual, "_retry_scheduled_monitoring") as retry:
            with self.assertRaises(SystemExit):
                manual._release_and_replay("manual-owner")
        self.assertEqual(committed, ["item evidence"])
        finish.assert_not_called()
        retry.assert_not_called()

        early_lookup, late_lookup, cleared = (MagicMock() for _ in range(3))
        early_lookup.cursor.return_value.__enter__.return_value.fetchone.return_value = (
            True, 9, datetime.now(timezone.utc) + timedelta(minutes=5))
        late_lookup.cursor.return_value.__enter__.return_value.fetchone.return_value = (
            True, 9, datetime.now(timezone.utc) - timedelta(seconds=1))
        issues = {"item-1": "resolved"}

        def reconcile(*, send_reminders):
            self.assertFalse(send_reminders)
            issues["item-1"] = "active"

        with patch.object(manual, "get_db_conn", side_effect=[early_lookup, late_lookup,
                                                                cleared]), \
             patch.object(manual, "put_db_conn"), \
             patch.object(manual.finance_monitor_service, "assess_and_notify",
                          side_effect=reconcile) as monitor:
            manual._retry_scheduled_monitoring()
            monitor.assert_not_called()
            manual._retry_scheduled_monitoring()
        monitor.assert_called_once_with(send_reminders=False)
        self.assertEqual(issues["item-1"], "active")
        clear = cleared.cursor.return_value.__enter__.return_value.execute.call_args
        self.assertEqual(clear.args[1], (9,))
        self.assertIn("monitoring_retry_at <= clock_timestamp()", clear.args[0])

    def test_interrupted_results_reconcile_once_after_status_recovery(self):
        run_id = uuid4()
        columns = ("id", "run_trigger", "status", "requested_at", "started_at",
                   "completed_at", "summary", "results", "error_code",
                   "monitoring_reconciled")
        evidence = {"connections": [{"connection_id": "item-1", "outcome": "VERIFIED"}],
                    "accounts": []}
        pending = (run_id, "manual", "running", NOW, NOW, None, {}, evidence, None, False)
        interrupted = (run_id, "manual", "partially_completed", NOW, NOW, NOW,
                       {}, evidence, "EXECUTION_INTERRUPTED", False)
        status_conn, lookup_conn, marker_conn = (MagicMock() for _ in range(3))
        status_cur = status_conn.cursor.return_value.__enter__.return_value
        status_cur.description = [(column,) for column in columns]
        status_cur.fetchone.side_effect = [pending, None, interrupted]
        lookup_cur = lookup_conn.cursor.return_value.__enter__.return_value
        lookup_cur.fetchone.return_value = ("partially_completed", "EXECUTION_INTERRUPTED",
                                            NOW, False)
        issues = {"item-1": "active"}

        def reconcile(*, send_reminders):
            self.assertFalse(send_reminders)
            issues["item-1"] = "resolved"

        with patch.object(manual, "get_db_conn", side_effect=[status_conn, lookup_conn,
                                                                marker_conn]), \
             patch.object(manual, "put_db_conn") as release, \
             patch.object(manual, "_recover_scheduled_handoff"), \
             patch.object(manual, "_retry_scheduled_monitoring"), \
             patch.object(manual.finance_monitor_service, "assess_and_notify",
                          side_effect=reconcile) as monitor:
            result = manual.get_run(run_id)
        self.assertEqual(result["status"], "partially_completed")
        self.assertEqual(result["error_code"], "EXECUTION_INTERRUPTED")
        self.assertEqual(issues["item-1"], "resolved")
        monitor.assert_called_once_with(send_reminders=False)
        self.assertIn("SET monitoring_reconciled = true",
                      marker_conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
        for conn in (status_conn, lookup_conn, marker_conn):
            conn.commit.assert_called_once()
            release.assert_any_call(conn)

        with patch.object(manual, "get_db_conn", return_value=lookup_conn), \
             patch.object(manual, "put_db_conn"), \
             patch.object(manual.finance_monitor_service, "assess_and_notify") as repeated:
            lookup_cur.fetchone.return_value = ("partially_completed", "EXECUTION_INTERRUPTED",
                                                NOW, True)
            manual._reconcile_unmonitored_run(run_id)
        repeated.assert_not_called()

    def test_interruption_between_evidence_commit_and_progress_is_reconciled(self):
        run_id = uuid4()
        lookup_conn, marker_conn = MagicMock(), MagicMock()
        lookup_cur = lookup_conn.cursor.return_value.__enter__.return_value
        lookup_cur.fetchone.return_value = ("failed", "EXECUTION_INTERRUPTED", NOW, False)
        issues = {"item-1": "resolved"}

        def reconcile(*, send_reminders):
            self.assertFalse(send_reminders)
            issues["item-1"] = "active"

        with patch.object(manual, "get_db_conn", side_effect=[lookup_conn, marker_conn]), \
             patch.object(manual, "put_db_conn") as release, \
             patch.object(manual.finance_monitor_service, "assess_and_notify",
                          side_effect=reconcile) as monitor:
            manual._reconcile_unmonitored_run(run_id)
        monitor.assert_called_once_with(send_reminders=False)
        self.assertEqual(issues["item-1"], "active")
        self.assertIn("SET monitoring_reconciled = true",
                      marker_conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
        self.assertEqual(release.call_count, 2)

    def test_scheduled_claim_records_due_collision_with_manual_owner(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        cursor.rowcount = 1
        with patch.object(verification, "get_db_conn", return_value=conn), \
             patch.object(verification, "put_db_conn") as release:
            claimed = verification._claim(NOW, "scheduled-owner", local_day=NOW.date())
        self.assertFalse(claimed)
        self.assertTrue(any("SET scheduled_pending = true" in c.args[0]
                            for c in cursor.execute.call_args_list))
        conn.commit.assert_called_once()
        release.assert_called_once_with(conn)

    def test_route_contract_and_restricted_request_model(self):
        app = FastAPI()
        app.include_router(finance.router)
        for path, method in (("/finance/verification/run", "POST"),
                             ("/finance/verification/runs/{run_id}", "GET")):
            route = next(r for r in app.routes if getattr(r, "path", None) == path)
            self.assertIn(method, route.methods)
            self.assertIn(require_admin_principal,
                          {dependency.call for dependency in route.dependant.dependencies})
        FinanceVerificationRunRequest.model_validate({})
        for field in ("access_token", "method", "force", "account_id"):
            with self.assertRaises(ValidationError):
                FinanceVerificationRunRequest.model_validate({field: "forbidden"})

    def test_manual_uses_existing_checker_with_configured_methods_and_no_snapshot(self):
        item = {"id": "item-1", "label": "Bank", "environment": "sandbox",
                "access_token_encrypted": "encrypted"}
        accounts = [{"source_account_id": "one", "institution_label": "Bank",
                     "include_in_net_worth": True}]
        checked = {"outcome": "VERIFIED", "connection_status": "UNKNOWN",
                   "inventory_status": "AVAILABLE", "available_account_ids": ["one"],
                   "missing_account_ids": [], "invalid_balance_ids": [], "method": "accounts_get"}
        with patch.object(verification.worker, "get_enabled_plaid_items", return_value=[item]), \
             patch.object(verification.worker, "_get_configured_plaid_accounts", return_value=accounts), \
             patch.object(verification.worker, "finance_config", {}), \
             patch.object(verification, "_renew", return_value=True), \
             patch.object(verification, "_check_item", return_value=checked) as check, \
             patch.object(verification, "_persist") as persist, \
             patch.object(verification.worker, "create_monthly_finance_snapshot") as snapshot, \
             patch.object(verification.worker, "collect_plaid_snapshot_items") as collect:
            result = verification._run_items("owner", started_at=datetime.now(timezone.utc))
        self.assertEqual(result["enabled_connections"], 1)
        self.assertTrue(result["complete"])
        self.assertEqual(result["results"][0]["live_balance_tested"], False)
        self.assertEqual(result["results"][0]["included_accounts"], 1)
        check.assert_called_once_with(item, {"one"}, check_item_status=False)
        persist.assert_called_once()
        snapshot.assert_not_called()
        collect.assert_not_called()

    def test_manual_reconciliation_never_sends_reminder_or_snapshot(self):
        run_id = uuid4()
        row = {"connection_id": "item-1", "label": "Bank", "outcome": "VERIFIED",
               "connection_status": "UNKNOWN", "inventory_status": "AVAILABLE",
               "method": "accounts_get", "missing_accounts": 0,
               "invalid_cached_balances": 0, "live_balance_tested": False}

        def run(_owner, *, started_at, on_result):
            on_result(row, 1)
            return {"enabled_connections": 1, "included_accounts": 1,
                    "results": [row], "complete": True}

        with patch.object(manual, "_update", return_value=True) as update, \
             patch.object(verification, "_run_items", side_effect=run), \
             patch.object(verification, "_release", return_value=False) as release, \
             patch.object(manual.finance_monitor_service, "assess_and_notify") as monitor, \
             patch.object(manual.finance_health_service, "assess_finance_health",
                          return_value={"accounts": []}), \
             patch.object(manual.finance_health_service, "read_evidence",
                          return_value={"errors": [], "accounts": [], "items": [{"id": "item-1",
                              "connection_status": "AUTH_REQUIRED", "inventory_status": "AVAILABLE"}]}), \
             patch.object(verification.worker, "create_monthly_finance_snapshot") as snapshot:
            manual.run_manual(run_id, "owner")
        monitor.assert_called_once_with(send_reminders=False)
        release.assert_called_once_with("owner")
        snapshot.assert_not_called()
        final = update.call_args.kwargs
        self.assertEqual(final["results"]["connections"][0]["issue_code"],
                         "PLAID_REAUTH_REQUIRED")
        self.assertEqual(final["summary"]["connections_with_problems"], 1)

    def test_partial_item_failure_reconciles_committed_evidence(self):
        run_id = uuid4()
        first = {"connection_id": "item-1", "label": "Bank", "outcome": "VERIFIED",
                 "connection_status": "UNKNOWN", "inventory_status": "AVAILABLE",
                 "method": "accounts_get", "missing_accounts": 0,
                 "invalid_cached_balances": 0, "live_balance_tested": False}

        def run(_owner, *, started_at, on_result):
            on_result(first, 2)
            raise RuntimeError("later Item failed")

        with patch.object(manual, "_update", return_value=True) as update, \
             patch.object(verification, "_run_items", side_effect=run), \
             patch.object(verification, "_release", return_value=False) as release, \
             patch.object(manual.finance_monitor_service, "assess_and_notify") as monitor, \
             patch.object(manual.finance_health_service, "assess_finance_health") as assess, \
             patch.object(verification.worker, "create_monthly_finance_snapshot") as snapshot:
            manual.run_manual(run_id, "owner")

        monitor.assert_called_once_with(send_reminders=False)
        assess.assert_not_called()
        release.assert_called_once_with("owner")
        snapshot.assert_not_called()
        final = update.call_args.kwargs
        self.assertEqual(final["status"], "partially_completed")
        self.assertEqual(final["error_code"], manual.ERROR_CODE)
        self.assertEqual(final["summary"]["connections_uncertain"], 1)
        self.assertEqual(final["results"]["connections"], [first])

    def test_partial_reconciliation_failure_preserves_execution_failure(self):
        row = {"connection_id": "item-1", "outcome": "VERIFIED"}

        def run(_owner, *, started_at, on_result):
            on_result(row, 2)
            raise RuntimeError("later Item failed")

        with patch.object(manual, "_update", return_value=True) as update, \
             patch.object(verification, "_run_items", side_effect=run), \
             patch.object(verification, "_release", return_value=False), \
             patch.object(manual.finance_monitor_service, "assess_and_notify",
                          side_effect=RuntimeError("monitor failed")) as monitor:
            manual.run_manual(uuid4(), "owner")

        monitor.assert_called_once_with(send_reminders=False)
        self.assertEqual(update.call_args.kwargs["status"], "partially_completed")
        self.assertEqual(update.call_args.kwargs["error_code"], manual.ERROR_CODE)
        self.assertFalse(update.call_args.kwargs["monitoring_reconciled"])

    def test_monitoring_failure_after_item_commit_is_retryable_on_lookup(self):
        run_id = uuid4()
        row = {"connection_id": "item-1", "outcome": "VERIFIED"}

        def run(_owner, *, started_at, on_result):
            on_result(row, 1)
            return {"enabled_connections": 1, "included_accounts": 0,
                    "results": [row], "complete": True}

        with patch.object(manual, "_update", return_value=True) as update, \
             patch.object(verification, "_run_items", side_effect=run), \
             patch.object(verification, "_release", return_value=False), \
             patch.object(manual.finance_monitor_service, "assess_and_notify",
                          side_effect=RuntimeError("monitor unavailable")) as monitor:
            manual.run_manual(run_id, "owner")
        self.assertEqual(monitor.call_count, 2)
        final = update.call_args.kwargs
        self.assertEqual(final["status"], "partially_completed")
        self.assertEqual(final["error_code"], manual.ERROR_CODE)
        self.assertFalse(final["monitoring_reconciled"])

        lookup, marker = MagicMock(), MagicMock()
        lookup.cursor.return_value.__enter__.return_value.fetchone.return_value = (
            "partially_completed", manual.ERROR_CODE, NOW, False)
        issues = {"item-1": "resolved"}

        def reconcile(*, send_reminders):
            self.assertFalse(send_reminders)
            issues["item-1"] = "active"

        with patch.object(manual, "get_db_conn", side_effect=[lookup, marker]), \
             patch.object(manual, "put_db_conn") as release, \
             patch.object(manual.finance_monitor_service, "assess_and_notify",
                          side_effect=reconcile) as retry:
            manual._reconcile_unmonitored_run(run_id)
        retry.assert_called_once_with(send_reminders=False)
        self.assertEqual(issues["item-1"], "active")
        self.assertIn("SET monitoring_reconciled = true",
                      marker.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
        self.assertEqual(release.call_count, 2)

    def test_stale_lease_does_not_persist_evidence(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        result = {"outcome": "VERIFIED", "connection_status": "UNKNOWN",
                  "inventory_status": "AVAILABLE", "available_account_ids": [],
                  "missing_account_ids": [], "invalid_balance_ids": [], "method": "accounts_get"}
        with patch.object(verification, "get_db_conn", return_value=conn), \
             patch.object(verification, "put_db_conn") as release:
            with self.assertRaisesRegex(RuntimeError, "lease lost"):
                verification._persist("item-1", result, NOW, owner="old-owner")
        self.assertEqual(cursor.execute.call_count, 1)
        conn.rollback.assert_called_once()
        release.assert_called_once_with(conn)

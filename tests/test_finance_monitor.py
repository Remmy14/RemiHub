"""Offline Finance monitoring tests with transactional notification persistence."""

import copy
from datetime import date, datetime, timedelta, timezone
import importlib
import sys
import threading
import types
import unittest
from unittest.mock import patch


original_db = sys.modules.get("backend.database.database")
fake_db = types.ModuleType("backend.database.database")
fake_db.get_db_conn = lambda: None
fake_db.put_db_conn = lambda _conn: None
sys.modules["backend.database.database"] = fake_db
try:
    monitor = importlib.import_module("backend.services.finance_monitor_service")
    health = importlib.import_module("backend.services.finance_health_service")
finally:
    if original_db is None:
        sys.modules.pop("backend.database.database", None)
    else:
        sys.modules["backend.database.database"] = original_db


ADMIN = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2027, 2, 26, 15, tzinfo=timezone.utc)


def sample_evidence(*, error=None, account_present=True, snapshot=True):
    return {
        "accounts": [{"id": "account-1", "source": "plaid", "source_account_id": "plaid-1",
                      "institution_label": "Bank", "name": "Checking", "display_name": "Checking",
                      "asset_category": "cash", "include_in_net_worth": True}] if account_present else [],
        "items": [{"id": "item-1", "label": "Bank", "enabled": True,
                   "last_success_at": NOW - timedelta(days=1), "last_error": error}],
        "manual": [],
        "snapshot": {"id": "snap-1", "snapshot_month": date(2027, 2, 1),
                     "status": "success", "created_at": NOW - timedelta(hours=2)} if snapshot else None,
        "snapshot_items": [{"source": "plaid", "source_account_id": "plaid-1",
                            "valuation_key": None, "value": 10, "asset_category": "cash",
                            "include_in_net_worth": True}] if snapshot else None,
        "errors": [],
    }


class Store:
    def __init__(self):
        self.state = {"assessed_on": None, "assessed_at": None, "evidence_hash": None,
                      "readiness": None, "previous_readiness": None, "summary": {}}
        self.issues = {}
        self.events = {}
        self.notifications = []
        self.admins = [ADMIN]
        self.lock = threading.Lock()
        self.commits = 0
        self.rollbacks = 0

    def connection(self):
        return Connection(self)


class Connection:
    def __init__(self, store):
        self.store = store
        self.held = False
        self.snapshot = None

    def cursor(self):
        return Cursor(self)

    def begin(self):
        if not self.held:
            self.store.lock.acquire()
            self.held = True
            self.snapshot = (copy.deepcopy(self.store.state),
                             copy.deepcopy(self.store.issues),
                             copy.deepcopy(self.store.events),
                             copy.deepcopy(self.store.notifications))

    def commit(self):
        self.store.commits += 1
        if self.held:
            self.held = False
            self.store.lock.release()

    def rollback(self):
        self.store.rollbacks += 1
        if self.held:
            (self.store.state, self.store.issues, self.store.events,
             self.store.notifications) = self.snapshot
            self.held = False
            self.store.lock.release()


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = []
        self.row = None
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        store = self.conn.store
        self.row, self.rows = None, []
        if text.startswith("INSERT INTO public.finance_health_monitor_state"):
            self.conn.begin()
        elif "FROM public.finance_health_monitor_state WHERE id = true FOR UPDATE" in text:
            self.conn.begin()
            s = store.state
            self.row = (s["assessed_on"], s["evidence_hash"], s["readiness"])
        elif "SELECT * FROM public.finance_health_monitor_issues FOR UPDATE" in text:
            keys = ["issue_key", "source_type", "source_id", "code", "severity",
                    "active", "occurrence", "opened_at", "last_seen_at",
                    "resolved_at", "notified"]
            self.description = [(key,) for key in keys]
            self.rows = [tuple(item.get(key) for key in keys)
                         for item in store.issues.values()]
        elif "SELECT id FROM public.remihub_users" in text:
            self.rows = [(user_id,) for user_id in store.admins]
        elif text.startswith("INSERT INTO public.finance_health_monitor_issues"):
            key, source, source_id, code, severity, occurrence, opened, seen = params
            store.issues[key] = dict(issue_key=key, source_type=source,
                                     source_id=source_id, code=code,
                                     severity=severity, active=True,
                                     occurrence=occurrence, opened_at=opened,
                                     last_seen_at=seen, resolved_at=None,
                                     notified=False)
        elif text.startswith("UPDATE public.finance_health_monitor_issues"):
            if "SET last_seen_at" in text:
                store.issues[params[1]]["last_seen_at"] = params[0]
            elif "SET notified" in text:
                store.issues[params[0]]["notified"] = True
            elif "SET active = false" in text:
                store.issues[params[1]].update(active=False, resolved_at=params[0])
            else:
                raise AssertionError(text)
        elif text.startswith("INSERT INTO public.finance_health_monitor_events"):
            key, kind, issue_key, target, days, user_id = params
            if key not in store.events:
                store.events[key] = dict(kind=kind, issue_key=issue_key,
                                         target=target, days=days,
                                         user_id=user_id, notification_id=None)
                self.row = (key,)
        elif text.startswith("INSERT INTO public.notifications"):
            title, body, module, priority, data = params
            notification_id = len(store.notifications) + 1
            store.notifications.append(dict(id=notification_id, title=title,
                                            body=body, module=module,
                                            priority=priority, data=data.adapted))
            self.row = (notification_id,)
        elif text.startswith("UPDATE public.finance_health_monitor_events"):
            store.events[params[1]]["notification_id"] = params[0]
        elif text.startswith("UPDATE public.finance_health_monitor_state"):
            day, instant, fingerprint, readiness, summary = params
            store.state.update(assessed_on=day, assessed_at=instant,
                               evidence_hash=fingerprint,
                               previous_readiness=store.state["readiness"],
                               readiness=readiness, summary=summary.adapted)
        else:
            raise AssertionError(text)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class FinanceMonitorTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.evidence = sample_evidence()
        self.patches = [
            patch.object(monitor, "get_db_conn", self.store.connection),
            patch.object(monitor, "put_db_conn", lambda _conn: None),
            patch.object(health, "read_evidence", lambda: copy.deepcopy(self.evidence)),
            patch.object(health.worker, "_complete_snapshot_items", lambda _p, _v: []),
            patch.object(health.worker, "_configured_valuation_keys", lambda: set()),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def run_monitor(self, instant=NOW, after_collection=False):
        return monitor.assess_and_notify(now=instant,
                                         after_collection=after_collection)

    def test_daily_restart_and_healthy_no_all_clear(self):
        self.assertTrue(self.run_monitor())
        self.assertFalse(self.run_monitor())
        self.assertFalse(self.store.notifications)
        self.assertEqual(self.store.state["assessed_on"], date(2027, 2, 26))
        self.assertTrue(self.run_monitor(NOW + timedelta(days=1)))

    def test_new_persistent_recovered_and_reopened_reauth(self):
        self.evidence["items"][0]["last_error"] = "ITEM_LOGIN_REQUIRED"
        self.run_monitor()
        self.assertEqual(len(self.store.notifications), 1)
        self.assertEqual(self.store.notifications[0]["data"]["user_id"], ADMIN)
        self.assertEqual(self.store.notifications[0]["priority"], 1)
        self.run_monitor(NOW + timedelta(days=1))
        self.assertEqual(len(self.store.notifications), 1)
        self.evidence["items"][0]["last_error"] = None
        self.evidence["items"][0]["last_success_at"] = NOW + timedelta(days=1, hours=1)
        self.run_monitor(NOW + timedelta(days=1, hours=2), after_collection=True)
        self.assertEqual(len(self.store.notifications), 2)
        self.assertIn("resolved", self.store.notifications[1]["title"].lower())
        self.evidence["items"][0]["last_error"] = "ITEM_LOGIN_REQUIRED"
        self.run_monitor(NOW + timedelta(days=2))
        self.assertEqual(len(self.store.notifications), 3)
        self.assertEqual(next(iter(self.store.issues.values()))["occurrence"], 2)

    def test_post_collection_failure_has_distinct_context(self):
        self.run_monitor()
        self.evidence["items"][0]["last_error"] = "ITEM_LOGIN_REQUIRED"
        self.run_monitor(NOW + timedelta(minutes=5), after_collection=True)
        self.assertEqual(self.store.notifications[-1]["data"]["evidence_phase"],
                         "collection_attempt")

    def test_manual_issues_resolve_independently_and_reopen(self):
        self.evidence["manual"] = [{
            "id": "manual-1", "label": "Savings", "asset_category": "cash",
            "current_value": None, "as_of_date": date(2026, 1, 1),
            "updated_at": NOW, "include_in_net_worth": True,
            "active": True, "valuation_key": None,
        }]
        value_key = "manual:manual-1:MANUAL_VALUE_MISSING"
        review_key = "manual:manual-1:MANUAL_REVIEW_DUE"
        self.run_monitor()
        self.assertTrue(self.store.issues[value_key]["active"])
        self.assertTrue(self.store.issues[review_key]["active"])

        # Correct value while its review date remains stale: only the value
        # problem resolves, despite overall account health still WARNING.
        self.evidence["manual"][0]["current_value"] = 10
        self.run_monitor(NOW + timedelta(hours=1))
        self.assertFalse(self.store.issues[value_key]["active"])
        self.assertTrue(self.store.issues[review_key]["active"])
        self.assertEqual(self.store.issues[value_key]["occurrence"], 1)
        self.assertEqual(sum(n["data"].get("issue_code") == "MANUAL_VALUE_MISSING"
                             for n in self.store.notifications), 2)

        self.evidence["manual"][0]["current_value"] = None
        self.run_monitor(NOW + timedelta(hours=2))
        self.assertTrue(self.store.issues[value_key]["active"])
        self.assertEqual(self.store.issues[value_key]["occurrence"], 2)

        # The review date can be corrected even while the value is missing.
        self.evidence["manual"][0]["as_of_date"] = NOW.date()
        self.run_monitor(NOW + timedelta(hours=3))
        self.assertFalse(self.store.issues[review_key]["active"])
        self.assertTrue(self.store.issues[value_key]["active"])
        self.evidence["manual"][0]["as_of_date"] = date(2026, 1, 1)
        self.run_monitor(NOW + timedelta(hours=4))
        self.assertTrue(self.store.issues[review_key]["active"])
        self.assertEqual(self.store.issues[review_key]["occurrence"], 2)

    def test_archived_manual_asset_closes_issues_without_recovery_notice(self):
        self.evidence["manual"] = [{
            "id": "manual-1", "label": "Savings", "asset_category": "cash",
            "current_value": None, "as_of_date": date(2026, 1, 1),
            "updated_at": NOW, "include_in_net_worth": True,
            "active": True, "valuation_key": None,
        }]
        value_key = "manual:manual-1:MANUAL_VALUE_MISSING"
        review_key = "manual:manual-1:MANUAL_REVIEW_DUE"
        self.run_monitor()
        self.assertTrue(self.store.issues[value_key]["active"])
        self.assertTrue(self.store.issues[review_key]["active"])
        notices = len(self.store.notifications)

        # read_evidence queries active manual assets, so archiving removes it
        # from a successful inventory read and from Card 1's assessment.
        self.evidence["manual"] = []
        self.run_monitor(NOW + timedelta(hours=1))
        self.assertFalse(self.store.issues[value_key]["active"])
        self.assertFalse(self.store.issues[review_key]["active"])
        self.assertEqual(len(self.store.notifications), notices)

        # An unavailable inventory is an operational failure, not a confirmed
        # removal that may silently clear a previously notified problem.
        self.evidence["manual"] = [{
            "id": "manual-1", "label": "Savings", "asset_category": "cash",
            "current_value": None, "as_of_date": date(2026, 1, 1),
            "updated_at": NOW, "include_in_net_worth": True,
            "active": True, "valuation_key": None,
        }]
        self.run_monitor(NOW + timedelta(hours=2))
        self.assertEqual(self.store.issues[value_key]["occurrence"], 2)
        self.evidence["manual"] = None
        self.evidence["errors"] = [{"code": "EVIDENCE_UNAVAILABLE"}]
        with self.assertRaises(RuntimeError):
            self.run_monitor(NOW + timedelta(hours=3))
        self.assertTrue(self.store.issues[value_key]["active"])

    def test_connection_issue_closes_silently_only_after_all_dependents_excluded(self):
        self.evidence = sample_evidence(error="ITEM_LOGIN_REQUIRED", snapshot=False)
        key = "plaid_connection:item-1:PLAID_REAUTH_REQUIRED"
        self.run_monitor()
        self.assertTrue(self.store.issues[key]["active"])
        second = copy.deepcopy(self.evidence["accounts"][0])
        second.update(id="account-2", source_account_id="plaid-2")
        self.evidence["accounts"].append(second)
        self.evidence["accounts"][0]["include_in_net_worth"] = False
        self.run_monitor(NOW + timedelta(hours=1))
        self.assertTrue(self.store.issues[key]["active"])
        self.assertEqual(self.store.issues[key]["occurrence"], 1)

        self.evidence["accounts"][1]["include_in_net_worth"] = False
        notices = len(self.store.notifications)
        self.run_monitor(NOW + timedelta(hours=2))
        self.assertFalse(self.store.issues[key]["active"])
        self.assertEqual(len(self.store.notifications), notices)

        self.evidence["accounts"][0]["include_in_net_worth"] = True
        self.run_monitor(NOW + timedelta(hours=3))
        self.assertTrue(self.store.issues[key]["active"])
        self.assertEqual(self.store.issues[key]["occurrence"], 2)
        self.assertEqual(self.store.notifications[-1]["data"]["issue_code"],
                         "PLAID_REAUTH_REQUIRED")

    def test_disabled_item_replaced_with_verified_account_collection(self):
        self.evidence = sample_evidence(error="ITEM_LOGIN_REQUIRED")
        key = "plaid_connection:item-1:PLAID_REAUTH_REQUIRED"
        self.run_monitor()
        self.assertTrue(self.store.issues[key]["active"])

        self.evidence["items"][0]["enabled"] = False
        self.evidence["items"].append({
            "id": "item-2", "label": "Bank", "enabled": True,
            "last_success_at": NOW + timedelta(hours=1), "last_error": None,
        })
        self.evidence["snapshot"].update(
            id="snap-2", created_at=NOW + timedelta(hours=2))
        self.run_monitor(NOW + timedelta(hours=3), after_collection=True)

        self.assertFalse(self.store.issues[key]["active"])
        self.assertEqual(len(self.store.notifications), 2)
        self.assertIn("resolved", self.store.notifications[-1]["title"].lower())
        self.assertEqual(self.store.notifications[-1]["data"]["issue_code"],
                         "PLAID_REAUTH_REQUIRED")
        self.assertFalse(self.run_monitor(NOW + timedelta(hours=4)))
        self.assertEqual(len(self.store.notifications), 2)

    def test_replacement_connection_success_alone_is_not_account_recovery(self):
        self.evidence = sample_evidence(error="ITEM_LOGIN_REQUIRED")
        key = "plaid_connection:item-1:PLAID_REAUTH_REQUIRED"
        self.run_monitor()
        self.evidence["items"][0]["enabled"] = False
        self.evidence["items"].append({
            "id": "item-2", "label": "Bank", "enabled": True,
            "last_success_at": NOW + timedelta(hours=1), "last_error": None,
        })
        # The stored account snapshot predates the replacement's success.
        self.run_monitor(NOW + timedelta(hours=2))
        self.assertFalse(self.store.issues[key]["active"])
        self.assertEqual(len(self.store.notifications), 1)
        self.assertEqual(sum(notice["data"].get("issue_code") == "PLAID_REAUTH_REQUIRED"
                             for notice in self.store.notifications), 1)

    def test_unknown_provider_error_is_not_urgent(self):
        self.evidence["items"][0]["last_error"] = "temporary provider outage"
        self.run_monitor()
        self.assertFalse(self.store.notifications)

    def test_evidence_error_preserves_state(self):
        self.run_monitor()
        self.evidence["errors"] = [{"code": "EVIDENCE_UNAVAILABLE"}]
        with self.assertRaises(RuntimeError):
            self.run_monitor(NOW + timedelta(days=1))
        self.assertEqual(self.store.state["assessed_on"], date(2027, 2, 26))
        self.assertEqual(self.store.rollbacks, 1)

    def test_insert_failure_rolls_back_issue_and_event_then_retry_succeeds(self):
        self.evidence["items"][0]["last_error"] = "ITEM_LOGIN_REQUIRED"
        with patch.object(monitor, "insert_notification", side_effect=RuntimeError("insert failed")):
            with self.assertRaises(RuntimeError):
                self.run_monitor()
        self.assertFalse(self.store.issues)
        self.assertFalse(self.store.events)
        self.assertFalse(self.store.notifications)
        self.run_monitor()
        self.assertEqual(len(self.store.notifications), 1)

    def test_concurrent_same_day_execution_records_one_event(self):
        self.evidence["items"][0]["last_error"] = "ITEM_LOGIN_REQUIRED"
        errors = []

        def execute():
            try:
                self.run_monitor()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=execute) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(len(self.store.notifications), 1)

    def test_known_worker_account_failures_are_critical(self):
        prefix = "Bank (connection item-1): "
        for detail, expected in (
            ("missing included account plaid-1 (Checking)", "PLAID_ACCOUNT_MISSING"),
            ("included account plaid-1 (Checking): missing balances.current", "PLAID_BALANCE_MISSING"),
        ):
            with self.subTest(detail=detail):
                self.evidence = sample_evidence(error=prefix + detail, snapshot=False)
                self.run_monitor()
                self.assertEqual(self.store.notifications[-1]["data"]["issue_code"], expected)
                self.assertEqual(self.store.notifications[-1]["priority"], 1)

    def test_recent_item_success_does_not_make_account_fresh(self):
        self.evidence["snapshot"]["created_at"] = NOW - timedelta(days=9)
        self.evidence["items"][0]["last_success_at"] = NOW - timedelta(hours=1)
        self.run_monitor()
        self.assertEqual(self.store.notifications[0]["data"]["issue_code"], "PLAID_DATA_STALE")

    def test_unverified_item_staleness_does_not_create_account_alert(self):
        self.evidence = sample_evidence(snapshot=False)
        self.evidence["items"][0]["last_success_at"] = NOW - timedelta(days=9)
        self.run_monitor()
        self.assertFalse(any(row["data"].get("issue_code") == "PLAID_DATA_STALE"
                             for row in self.store.notifications))

    def test_reminders_before_next_month_and_no_repeat(self):
        self.evidence["manual"] = [{"id": "manual-1", "label": "Savings",
                                    "asset_category": "cash", "current_value": 10,
                                    "as_of_date": date(2026, 1, 1),
                                    "updated_at": NOW,
                                    "include_in_net_worth": True,
                                    "active": True, "valuation_key": None}]
        self.run_monitor()
        self.assertEqual(len(self.store.notifications), 2)  # warning and summary
        self.assertEqual(self.store.notifications[-1]["data"]["target_month"], "2027-03-01")
        self.assertFalse(self.run_monitor())
        self.run_monitor(datetime(2027, 2, 28, 15, tzinfo=timezone.utc))
        self.assertEqual(len(self.store.notifications), 3)
        self.assertEqual(self.store.notifications[-1]["data"]["days_before"], "1")
        for notice in self.store.notifications:
            self.assertNotIn("Savings", notice["body"])
            self.assertNotIn("10", notice["body"])

    def test_critical_alert_suppresses_equivalent_reminder(self):
        self.evidence["items"][0]["last_error"] = "ITEM_LOGIN_REQUIRED"
        self.run_monitor()
        self.assertEqual(len(self.store.notifications), 1)
        self.assertEqual(self.store.events["reminder:2027-03-01:3"]["kind"],
                         "suppressed")

    def test_month_lengths_and_new_york_boundary(self):
        self.assertEqual(monitor.reminder_interval(date(2027, 2, 26)), 3)
        self.assertEqual(monitor.reminder_interval(date(2027, 2, 28)), 1)
        self.assertEqual(monitor.reminder_interval(date(2028, 2, 27)), 3)
        self.assertEqual(monitor.reminder_interval(date(2028, 2, 29)), 1)
        self.assertEqual(monitor.reminder_interval(date(2027, 2, 27)), 3)
        self.assertIsNone(monitor.reminder_interval(date(2027, 3, 1)))
        self.assertEqual(datetime(2027, 3, 1, 0, tzinfo=timezone.utc)
                         .astimezone(monitor.NEW_YORK).date(), date(2027, 2, 28))

    def test_worker_checks_before_and_after_unchanged_snapshot_call(self):
        calls = []
        worker = health.worker

        def snapshot(**_kwargs):
            calls.append("snapshot")
            return {"success": False, "skipped": False,
                    "snapshot_month": "2027-03-01", "status": "blocked_incomplete"}

        with patch.object(monitor, "assess_and_notify",
                          side_effect=lambda **kwargs: calls.append(
                              "post" if kwargs.get("after_collection") else "pre")), \
             patch.object(worker, "create_monthly_finance_snapshot", side_effect=snapshot), \
             patch.object(worker.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                worker.run_finance_worker()
        self.assertEqual(calls, ["pre", "snapshot", "post"])

    def test_existing_snapshot_notice_targets_admin_without_amount(self):
        worker = health.worker
        with patch.object(worker, "get_db_conn", self.store.connection), \
             patch.object(worker, "put_db_conn", lambda _conn: None):
            worker.send_finance_snapshot_notification({
                "success": True, "skipped": False, "status": "success",
                "snapshot_month": "2027-03-01", "net_worth": 123456.78,
            })
        self.assertEqual(len(self.store.notifications), 1)
        notice = self.store.notifications[0]
        self.assertEqual(notice["data"]["user_id"], ADMIN)
        self.assertNotIn("123456", notice["body"])
        self.assertNotIn("net worth", notice["body"].lower())


if __name__ == "__main__":
    unittest.main()

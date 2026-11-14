import asyncio
import multiprocessing
import os
import signal
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException

from backend.core.agent_usage import normalize_quota, normalize_turn_usage
from backend.agent_usage_collector import RateLimits, collect_once, _receive_probe
from backend.core.auth import AuthenticatedPrincipal, require_admin_principal, require_current_principal
from backend.routers.agent import router, codex_quota, codex_summary
from backend.models.agent_models import CodexQuotaResponse, CodexSummaryResponse
from backend.services.agent_usage_service import (
    configuration, executions, period, quota as read_quota, summary,
)
from backend.services.agent_usage_writer import _write, ensure_attempt, record_turn, publish_stage_configuration
from backend.agent_worker import run_worker


def _large_probe_result(output):
    os.setsid()
    output.put(("available", "x" * 1_000_000))


class CollectorPipeTests(unittest.TestCase):
    def test_large_result_is_received_before_child_is_reaped(self):
        context = multiprocessing.get_context("spawn")
        output = context.Queue(maxsize=1)
        process = context.Process(target=_large_probe_result, args=(output,), daemon=True)
        process.start()
        try:
            status, value = _receive_probe(process, output, 5)
            self.assertEqual(status, "available")
            self.assertEqual(len(value), 1_000_000)
            self.assertFalse(process.is_alive())
        finally:
            output.close()


class NormalizationTests(unittest.TestCase):
    def test_last_turn_only_and_cached_tokens_are_subset(self):
        usage = normalize_turn_usage({"last": {"input_tokens": 100, "output_tokens": 50,
                                                 "cached_input_tokens": 30, "reasoning_output_tokens": 10,
                                                 "total_tokens": 150},
                                      "total": {"input_tokens": 1000, "output_tokens": 600,
                                                "total_tokens": 1600}})
        self.assertEqual(usage["total_tokens"], 150)
        self.assertEqual(usage["cached_input_tokens"], 30)
        self.assertEqual(usage["reasoning_tokens"], 10)

    def test_missing_or_invalid_usage_is_unavailable(self):
        self.assertEqual(normalize_turn_usage(None)["usage_status"], "unavailable")
        self.assertEqual(normalize_turn_usage({"total": {"total_tokens": 8}})["usage_status"], "unavailable")
        self.assertEqual(normalize_turn_usage({"last": {"input_tokens": 1, "output_tokens": 2,
                                                       "total_tokens": 9}})["usage_status"], "unavailable")

    def test_quota_keeps_independent_buckets_and_missing_fields(self):
        result = normalize_quota({"rateLimits": {}, "rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 30, "windowDurationMins": 300,
                                   "resetsAt": 1890000000}},
            "other": {"secondary": {"usedPercent": 150}}},
            "rateLimitResetCredits": {"availableCount": 2}})
        self.assertEqual(len(result["buckets"]), 2)
        self.assertEqual(result["buckets"][0]["windows"][0]["remaining_percent"], 70)
        self.assertEqual(result["buckets"][1]["windows"][0]["status"], "unavailable")
        self.assertEqual(result["available_reset_credits"], 2)
        self.assertIsNone(result["account_id"])

    def test_out_of_range_reset_preserves_valid_window_and_credits(self):
        result = normalize_quota({"rateLimits": {
            "primary": {"usedPercent": 42, "resetsAt": 10 ** 40},
            "credits": {"balance": "3", "hasCredits": True},
        }})
        bucket = result["buckets"][0]
        window = bucket["windows"][0]
        self.assertEqual(window["status"], "available")
        self.assertEqual(window["used_percent"], 42)
        self.assertEqual(window["remaining_percent"], 58)
        self.assertIsNone(window["reset_at"])
        self.assertEqual(bucket["credits"]["balance"], "3")

    def test_sparse_bucket_map_without_legacy_field(self):
        sparse = {"rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 25, "resetsAt": 1890000000}},
        }}
        result = normalize_quota(RateLimits.model_validate(sparse).model_dump(mode="json"))
        self.assertEqual(len(result["buckets"]), 1)
        self.assertEqual(result["buckets"][0]["windows"][0]["remaining_percent"], 75)
        self.assertIsNone(result["available_reset_credits"])
        with self.assertRaises(ValueError):
            normalize_quota({"unrelated": {}})
        with self.assertRaises(ValueError):
            normalize_quota({"rateLimitsByLimitId": {"codex": "invalid"}})

    def test_additional_and_invalid_windows_are_preserved(self):
        result = normalize_quota({"rateLimits": {
            "primary": {"usedPercent": 15},
            "tertiary": {"usedPercent": 40, "resetsAt": 1890000000},
            "futureWindow": {"resetsAt": 1890000000},
            "credits": {"hasCredits": True},
        }})
        windows = {window["name"]: window for window in result["buckets"][0]["windows"]}
        self.assertEqual(set(windows), {"primary", "tertiary", "futureWindow"})
        self.assertEqual(windows["tertiary"]["remaining_percent"], 60)
        self.assertEqual(windows["futureWindow"]["status"], "unavailable")

    def test_timeout_stops_probe_group_and_reaps_probe(self):
        process = MagicMock()
        process.pid = 12345
        process.is_alive.side_effect = [True, False, False]
        output = MagicMock()
        context = MagicMock()
        context.Queue.return_value = output
        context.Process.return_value = process
        signals = []

        def signal_group(pgid, sig):
            signals.append((pgid, sig))

        with patch("backend.agent_usage_collector.mp.get_context", return_value=context), \
             patch("backend.agent_usage_collector.Path.is_file", return_value=True), \
             patch("backend.agent_usage_collector.os.access", return_value=True), \
             patch("backend.agent_usage_collector.os.killpg", side_effect=signal_group):
            result = collect_once("/sandbox/codex", timeout_seconds=1)
        self.assertEqual(result, ("collection_failed", "Timeout"))
        self.assertEqual(signals, [(12345, signal.SIGTERM), (12345, signal.SIGKILL)])
        joins = [call.args[0] for call in process.join.call_args_list]
        self.assertGreater(joins[0], 0)
        self.assertLessEqual(joins[0], 1)
        self.assertEqual(joins[1:], [2, 2])
        process.kill.assert_not_called()
        output.close.assert_called_once()

    def test_period_requires_bounded_aware_timestamps(self):
        with self.assertRaises(ValueError):
            period(None, __import__("datetime").datetime(2026, 1, 1))

    def test_missing_codex_binary_is_a_collection_failure(self):
        status, code = collect_once("/nonexistent/codex")
        self.assertEqual((status, code), ("collection_failed", "CodexBinaryUnavailable"))


class ApiTests(unittest.TestCase):
    def test_usage_routes_have_admin_dependency(self):
        routes = [route for route in router.routes if route.path.startswith("/agent/usage/")]
        self.assertEqual(len(routes), 4)
        self.assertTrue(all(any(dep.dependency is require_admin_principal for dep in route.dependencies)
                            for route in routes))

    @patch("backend.routers.agent.agent_usage_service.quota")
    def test_quota_endpoint_never_needs_codex_credentials(self, quota):
        quota.return_value = {"status": "unavailable", "source": "codex_app_server_account_rateLimits_read",
                              "collected_at": None, "last_attempt_at": None, "freshness_seconds": None,
                              "error_code": None, "account_id": None, "buckets": [],
                              "ordinary_usage_allowed": None, "available_reset_credits": None,
                              "metric_statuses": {"absolute_remaining_allocation": "unsupported"}}
        response = codex_quota(_principal=None)
        CodexQuotaResponse.model_validate(response)
        self.assertIsNone(response["data"]["available_reset_credits"])

    def test_invalid_period_rejected(self):
        from datetime import datetime, timezone
        with self.assertRaises(HTTPException) as error:
            codex_summary(datetime(2026, 1, 1, tzinfo=timezone.utc),
                          datetime(2028, 1, 1, tzinfo=timezone.utc), _principal=None)
        self.assertEqual(error.exception.status_code, 400)

    def test_failed_quota_read_reports_failure_without_old_values(self):
        class Cursor:
            description = [(name,) for name in
                           ("source", "status", "payload", "collected_at", "last_attempt_at", "error_code")]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, *args):
                pass

            def fetchall(self):
                return [("codex_app_server_account_rateLimits_read", "collection_failed", None,
                         None, None, "Timeout")]

        class Connection:
            def cursor(self):
                return Cursor()

            def rollback(self):
                pass

        with patch("backend.services.agent_usage_service._connection", return_value=(Connection(), lambda _: None)):
            data = read_quota()
        self.assertEqual(data["metric_statuses"]["windows"], "collection_failed")
        self.assertIsNone(data["available_reset_credits"])
        self.assertEqual(data["buckets"], [])

    def test_quota_permission_missing_reports_unavailable(self):
        class PermissionDenied(Exception):
            pgcode = "42501"

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, *args):
                raise PermissionDenied()

        class Connection:
            rolled_back = False

            def cursor(self):
                return Cursor()

            def rollback(self):
                self.rolled_back = True

        conn = Connection()
        with patch("backend.services.agent_usage_service._connection", return_value=(conn, lambda _: None)):
            data = read_quota()
        self.assertTrue(conn.rolled_back)
        self.assertEqual(data["status"], "unavailable")
        self.assertEqual(data["error_code"], "DatabasePermissionUnavailable")

    def test_window_metric_requires_usable_utilization(self):
        class Cursor:
            description = [(name,) for name in
                           ("source", "status", "payload", "collected_at", "last_attempt_at", "error_code")]

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, *args):
                pass

            def fetchall(self):
                return [("codex_app_server_account_rateLimits_read", "available", self.payload,
                         datetime.now(timezone.utc), datetime.now(timezone.utc), None)]

        for windows, expected in (([], "unavailable"),
                                  ([{"name": "primary", "status": "unavailable",
                                     "used_percent": None}], "unavailable"),
                                  ([{"name": "primary", "status": "available",
                                     "used_percent": None}], "unavailable"),
                                  ([{"name": "primary", "status": "available",
                                     "used_percent": 150}], "unavailable"),
                                  ([{"name": "primary", "status": "unavailable",
                                     "used_percent": None},
                                    {"name": "tertiary", "status": "available",
                                     "used_percent": 40}], "available")):
            with self.subTest(expected=expected, windows=windows):
                conn = _Connection(Cursor({"buckets": [{"windows": windows}]}))
                with patch("backend.services.agent_usage_service._connection",
                           return_value=(conn, lambda _: None)):
                    data = read_quota()
                self.assertEqual(data["metric_statuses"]["windows"], expected)

    def test_empty_credits_remain_unavailable_through_normalization_and_quota(self):
        empty = normalize_quota({"rateLimits": {"credits": {}}})
        self.assertIsNone(empty["buckets"][0]["credits"])
        blank = normalize_quota({"rateLimits": {"credits": {"balance": "  "}}})
        self.assertIsNone(blank["buckets"][0]["credits"])
        false_balance = normalize_quota({"rateLimits": {"credits": {"hasCredits": False}}})
        self.assertEqual(false_balance["buckets"][0]["credits"]["has_credits"], False)

        class Cursor:
            description = [(name,) for name in
                           ("source", "status", "payload", "collected_at", "last_attempt_at", "error_code")]

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, *args):
                pass

            def fetchall(self):
                now = datetime.now(timezone.utc)
                return [("codex_app_server_account_rateLimits_read", "available",
                         self.payload, now, now, None)]

        for payload, expected in ((empty, "unavailable"),
                                  (blank, "unavailable"),
                                  (false_balance, "available"),
                                  ({"buckets": [{"credits": {"balance": None,
                                                   "has_credits": None, "unlimited": None}}]}, "unavailable")):
            with self.subTest(expected=expected, payload=payload):
                conn = _Connection(Cursor(payload))
                with patch("backend.services.agent_usage_service._connection",
                           return_value=(conn, lambda _: None)):
                    data = read_quota()
                self.assertEqual(data["metric_statuses"]["credits"], expected)


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Cursor:
    def __init__(self):
        self.calls = []
        self.description = []
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return self.rows


class PersistenceTests(unittest.TestCase):
    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_timed_out_telemetry_sql_rolls_back_and_releases_connection(self, get_conn, release):
        class TimedOutCursor(_Cursor):
            def execute(self, sql, params=None):
                super().execute(sql, params)
                if "INSERT INTO agent.codex_execution_usage" in sql:
                    raise TimeoutError("statement timeout")

        conn = _Connection(TimedOutCursor())
        get_conn.return_value = conn
        claim = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000001"), attempt_count=1)
        self.assertFalse(_write(claim, "turn-1", {"usage_status": "available", "usage_source": "test"},
                                "model", "model", None))
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 1)
        release.assert_called_once_with(conn)

    def test_overlapping_placeholder_and_turn_leave_one_counted_record(self):
        locked = threading.Lock()
        selected = threading.Event()
        release_placeholder = threading.Event()
        turn_committed = threading.Event()
        records = set()

        class Connection:
            def __init__(self, **_kwargs):
                self.held = False
                self.turn_id = None
                self.selected_existing = False

            def cursor(self):
                connection = self

                class Cursor:
                    def __enter__(self):
                        return self

                    def __exit__(self, *_args):
                        pass

                    def execute(self, sql, params=None):
                        if "pg_advisory_xact_lock" in sql:
                            locked.acquire()
                            connection.held = True
                        elif "SELECT 1 FROM agent.codex_execution_usage" in sql:
                            connection.selected_existing = bool(records)
                        elif "DELETE FROM agent.codex_execution_usage" in sql:
                            records.discard("")
                        elif "INSERT INTO agent.codex_execution_usage" in sql:
                            connection.turn_id = params[1]
                            records.add(params[1])

                    def fetchone(self):
                        selected.set()
                        release_placeholder.wait(2)
                        return (1,) if connection.selected_existing else None

                return Cursor()

            def commit(self):
                if self.held:
                    locked.release()
                    self.held = False
                if self.turn_id:
                    turn_committed.set()

            def rollback(self):
                if self.held:
                    locked.release()
                    self.held = False

        claim = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000001"),
                                attempt_count=1, phase=SimpleNamespace(value="review"))
        with patch.dict("os.environ", {"REMIHUB_AGENT_EXECUTOR": "codex-review"}), \
             patch("backend.services.agent_worker_service.get_db_conn", side_effect=Connection), \
             patch("backend.services.agent_worker_service.put_db_conn"):
            placeholder = threading.Thread(target=ensure_attempt, args=(claim,))
            placeholder.start()
            try:
                self.assertTrue(selected.wait(1))
                record_turn(claim, turn_id="turn-1", configured_model="model", usage={"last": {
                    "input_tokens": 7, "output_tokens": 3, "total_tokens": 10}})
                self.assertFalse(turn_committed.is_set())
            finally:
                release_placeholder.set()
                placeholder.join(2)
            self.assertTrue(turn_committed.wait(2))
        self.assertEqual(records, {"turn-1"})

    def test_stalled_turn_connection_does_not_hold_up_execution(self):
        acquired = threading.Event()
        release = threading.Event()
        returned = threading.Event()
        claim = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000001"),
                                attempt_count=1, phase=SimpleNamespace(value="review"))

        def blocked_acquisition(**_kwargs):
            acquired.set()
            release.wait(2)
            return _Connection(_Cursor())

        with patch.dict("os.environ", {"REMIHUB_AGENT_EXECUTOR": "codex-review"}), \
             patch("backend.services.agent_worker_service.get_db_conn", side_effect=blocked_acquisition), \
             patch("backend.services.agent_worker_service.put_db_conn", side_effect=lambda _conn: returned.set()):
            try:
                begin = time.monotonic()
                record_turn(claim, turn_id="turn-1", configured_model="model", usage={"last": {
                    "input_tokens": 7, "output_tokens": 3, "total_tokens": 10}})
                self.assertLess(time.monotonic() - begin, 0.5)
                self.assertTrue(acquired.is_set())
            finally:
                release.set()
            self.assertTrue(returned.wait(2))

    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_existing_attempt_ends_read_transaction_before_pool_release(self, get_conn, release):
        class ExistingCursor(_Cursor):
            def fetchone(self):
                return (1,)

        class Connection(_Connection):
            rollbacks = 0

            def rollback(self):
                self.rollbacks += 1

        conn = Connection(ExistingCursor())
        get_conn.return_value = conn
        claim = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000001"),
                                attempt_count=2, phase=SimpleNamespace(value="review"))
        with patch.dict("os.environ", {"REMIHUB_AGENT_EXECUTOR": "codex-review"}):
            ensure_attempt(claim, configured_model="gpt-6-sol")
        self.assertEqual(conn.rollbacks, 1)
        self.assertEqual(conn.commits, 0)
        self.assertEqual(len(conn._cursor.calls), 3)
        self.assertIn("statement_timeout", conn._cursor.calls[0][0])
        self.assertIn("pg_advisory_xact_lock", conn._cursor.calls[1][0])
        release.assert_called_once_with(conn)

    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_unavailable_attempt_is_replaced_and_retries_remain_distinct(self, get_conn, _release):
        cursor = _Cursor()
        conn = _Connection(cursor)
        get_conn.return_value = conn
        run_one = UUID("00000000-0000-0000-0000-000000000001")
        run_review = UUID("00000000-0000-0000-0000-000000000002")
        with patch.dict("os.environ", {"REMIHUB_AGENT_EXECUTOR": "codex-review"}):
            for run_id, attempt, turn in ((run_one, 1, "turn-one"),
                                          (run_one, 2, "turn-retry"),
                                          (run_review, 1, "turn-correction")):
                claim = SimpleNamespace(id=run_id, attempt_count=attempt,
                                        phase=SimpleNamespace(value="review"))
                ensure_attempt(claim, configured_model="gpt-6-sol")
                record_turn(claim, turn_id=turn, configured_model="gpt-6-sol",
                            actual_model="gpt-6-sol", reasoning_effort="high",
                            usage={"last": {"input_tokens": 10, "output_tokens": 2,
                                            "cached_input_tokens": 3, "total_tokens": 12},
                                   "total": {"total_tokens": 999}})
        inserts = [(sql, params) for sql, params in cursor.calls
                   if "INSERT INTO agent.codex_execution_usage" in sql]
        deletes = [(sql, params) for sql, params in cursor.calls
                   if "DELETE FROM agent.codex_execution_usage" in sql]
        self.assertEqual(len(inserts), 6)
        self.assertEqual(len(deletes), 3)
        self.assertEqual({(params[0], params[1], params[-1]) for _, params in inserts},
                         {(1, "", run_one), (1, "turn-one", run_one),
                          (2, "", run_one), (2, "turn-retry", run_one),
                          (1, "", run_review), (1, "turn-correction", run_review)})
        self.assertTrue(all("AND turn_id = ''" in sql for sql, _ in deletes))
        self.assertEqual([params for _, params in deletes],
                         [(run_one, 1), (run_one, 2), (run_review, 1)])
        self.assertTrue(all(params[-4] == 12 for _, params in inserts if params[1]))

    @patch("backend.services.agent_worker_service.put_db_conn")
    @patch("backend.services.agent_worker_service.get_db_conn")
    def test_worker_configuration_publication_uses_current_settings(self, get_conn, _release):
        cursor = _Cursor()
        conn = _Connection(cursor)
        get_conn.return_value = conn
        publish_stage_configuration(executor_name="codex-planning", worker_id="worker-1",
                                    model="model-new", reasoning_effort="high")
        self.assertEqual(cursor.calls[0][1], ("planning", "worker-1", "model-new", "high"))
        self.assertEqual(conn.commits, 1)
        publish_stage_configuration(executor_name="git-backend-deployment", worker_id="worker-1",
                                    model=None)
        self.assertEqual(len(cursor.calls), 1)


class WorkerConfigurationTests(unittest.TestCase):
    def test_run_once_reports_failed_telemetry_without_failing_completed_run(self):
        settings = SimpleNamespace(queue_environment="qa", deployment_environment=None,
                                   executor_name="codex-planning", worker_id="worker-1",
                                   codex_model="model", lease_seconds=30,
                                   heartbeat_seconds=10, max_attempts=3, run_once=True)
        queue = MagicMock()
        queue.verify_identity.return_value = ("qa", "worker")
        worker = MagicMock()
        claim = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000001"),
                                attempt_count=1, phase=SimpleNamespace(value="planning"))
        worker.process_once.side_effect = lambda: record_turn(
            claim, turn_id="turn-1", configured_model="model",
            usage={"last": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}}) or True
        worker.flush_usage_attempts.return_value = True
        with patch.dict("os.environ", {"REMIHUB_AGENT_EXECUTOR": "codex-planning"}), \
             patch("backend.agent_worker.DatabaseAgentQueue", return_value=queue), \
             patch("backend.agent_worker.build_executor", return_value=MagicMock()), \
             patch("backend.agent_worker.AgentWorker", return_value=worker), \
             patch("backend.services.agent_usage_writer.publish_stage_configuration"), \
             patch("backend.services.agent_usage_writer._write", return_value=False) as failed_write, \
             patch("backend.agent_worker.logger.warning") as warning:
            run_worker(settings)
        worker.process_once.assert_called_once_with()
        failed_write.assert_called_once()
        worker.flush_usage_attempts.assert_called_once_with()
        warning.assert_called_once()
        self.assertIn("telemetry incomplete", warning.call_args.args[0])
        self.assertEqual(warning.call_args.args[1:], (False, True))

    def test_consecutive_claims_queue_unreported_attempts_behind_slow_write(self):
        from backend.core.agent_worker import AgentWorker
        from backend.core.agent_state import RunPhase

        first_write_started = threading.Event()
        release_first = threading.Event()

        class SlowFirstConnection(_Connection):
            def commit(self):
                first_write_started.set()
                if not release_first.wait(2):
                    raise AssertionError("First write was not released")
                super().commit()

        first = SlowFirstConnection(_Cursor())
        second = _Connection(_Cursor())
        connections = iter((first, second))
        executor_type = type("CodexExecutor", (), {"__module__": "backend.core.codex_planning", "model": "model"})
        worker = AgentWorker(queue=MagicMock(), executor=executor_type(), worker_id="worker-1",
                             lease_seconds=30, heartbeat_seconds=10, max_attempts=3)
        claims = [SimpleNamespace(id=UUID(f"00000000-0000-0000-0000-{number:012d}"),
                                  attempt_count=1, phase=RunPhase.PLANNING) for number in (1, 2)]

        with patch.dict("os.environ", {"REMIHUB_AGENT_EXECUTOR": "codex-planning"}), \
             patch("backend.services.agent_worker_service.get_db_conn", side_effect=lambda **_kwargs: next(connections)), \
             patch("backend.services.agent_worker_service.put_db_conn"):
            try:
                worker._ensure_usage_attempt(claims[0])
                self.assertTrue(first_write_started.wait(1))
                began = time.monotonic()
                worker._ensure_usage_attempt(claims[1])
                self.assertLess(time.monotonic() - began, 0.2)
            finally:
                release_first.set()
            self.assertTrue(worker.flush_usage_attempts(timeout_seconds=2))

        self.assertEqual(first.commits, 1)
        self.assertEqual(second.commits, 1)
        for claim, connection in zip(claims, (first, second)):
            inserts = [(sql, params) for sql, params in connection._cursor.calls
                       if "INSERT INTO agent.codex_execution_usage" in sql]
            self.assertEqual(len(inserts), 1)
            self.assertEqual(inserts[0][1][-1], claim.id)
            self.assertEqual(inserts[0][1][1], "")

    def test_run_once_waits_for_unreported_attempt_placeholder_commit(self):
        from backend.core.agent_worker import AgentWorker
        from backend.core.agent_state import RunPhase

        durable = threading.Event()
        class SlowConnection(_Connection):
            def commit(self):
                time.sleep(0.25)
                super().commit()
                durable.set()

        connection = SlowConnection(_Cursor())
        claim = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000001"),
                                attempt_count=1, phase=RunPhase.PLANNING)
        executor_type = type("CodexExecutor", (), {"__module__": "backend.core.codex_planning", "model": "model"})
        worker = AgentWorker(queue=MagicMock(), executor=executor_type(), worker_id="worker-1",
                             lease_seconds=30, heartbeat_seconds=10, max_attempts=3)
        settings = SimpleNamespace(queue_environment="qa", deployment_environment=None,
                                   executor_name="codex-planning", worker_id="worker-1",
                                   codex_model="model", lease_seconds=30,
                                   heartbeat_seconds=10, max_attempts=3, run_once=True)
        queue = MagicMock()
        queue.verify_identity.return_value = ("qa", "worker")

        def process_once():
            worker._ensure_usage_attempt(claim)
            return True

        with patch.dict("os.environ", {"REMIHUB_AGENT_EXECUTOR": "codex-planning"}), \
             patch("backend.agent_worker.DatabaseAgentQueue", return_value=queue), \
             patch("backend.agent_worker.build_executor", return_value=executor_type()), \
             patch("backend.agent_worker.AgentWorker", return_value=worker), \
             patch.object(worker, "process_once", side_effect=process_once), \
             patch("backend.services.agent_usage_writer.publish_stage_configuration"), \
             patch("backend.services.agent_worker_service.get_db_conn", return_value=connection), \
             patch("backend.services.agent_worker_service.put_db_conn"):
            began = time.monotonic()
            run_worker(settings)
            elapsed = time.monotonic() - began

        self.assertTrue(durable.is_set())
        self.assertEqual(connection.commits, 1)
        self.assertGreaterEqual(elapsed, 0.2)
        inserts = [(sql, params) for sql, params in connection._cursor.calls
                   if "INSERT INTO agent.codex_execution_usage" in sql]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][1][1], "")

    def test_run_once_waits_for_turn_commit_beyond_initial_write_wait(self):
        durable = threading.Event()
        released = threading.Event()
        class SlowConnection(_Connection):
            def commit(self):
                time.sleep(0.25)
                super().commit()
                durable.set()

        connection = SlowConnection(_Cursor())
        claim = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000001"),
                                attempt_count=1, phase=SimpleNamespace(value="planning"))
        settings = SimpleNamespace(queue_environment="qa", deployment_environment=None,
                                   executor_name="codex-planning", worker_id="worker-1",
                                   codex_model="model", lease_seconds=30,
                                   heartbeat_seconds=10, max_attempts=3, run_once=True)
        queue = MagicMock()
        queue.verify_identity.return_value = ("qa", "worker")
        worker = MagicMock()
        worker.process_once.side_effect = lambda: record_turn(
            claim, turn_id="turn-1", configured_model="model",
            usage={"last": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}}) or True

        with patch.dict("os.environ", {"REMIHUB_AGENT_EXECUTOR": "codex-planning"}), \
             patch("backend.agent_worker.DatabaseAgentQueue", return_value=queue), \
             patch("backend.agent_worker.build_executor", return_value=MagicMock()), \
             patch("backend.agent_worker.AgentWorker", return_value=worker), \
             patch("backend.services.agent_usage_writer.publish_stage_configuration"), \
             patch("backend.services.agent_worker_service.get_db_conn", return_value=connection), \
             patch("backend.services.agent_worker_service.put_db_conn", side_effect=lambda _conn: released.set()):
            began = time.monotonic()
            run_worker(settings)
            elapsed = time.monotonic() - began
        self.assertTrue(durable.is_set())
        self.assertTrue(released.is_set())
        self.assertEqual(connection.commits, 1)
        self.assertGreaterEqual(elapsed, 0.2)

    def test_stalled_configuration_write_does_not_delay_worker_startup(self):
        write_started = threading.Event()
        release_write = threading.Event()
        processed = threading.Event()
        errors = []
        settings = SimpleNamespace(queue_environment="qa", deployment_environment=None,
                                   executor_name="codex-planning", worker_id="worker-1",
                                   codex_model="model-new", lease_seconds=30,
                                   heartbeat_seconds=10, max_attempts=3, run_once=True)
        queue = MagicMock()
        queue.verify_identity.return_value = ("qa", "worker")
        worker = MagicMock()
        worker.process_once.side_effect = lambda: processed.set() or True

        def blocked_publish(**_kwargs):
            write_started.set()
            release_write.wait(2)

        def run():
            try:
                run_worker(settings)
            except Exception as exc:
                errors.append(exc)

        with patch("backend.agent_worker.DatabaseAgentQueue", return_value=queue), \
             patch("backend.agent_worker.build_executor", return_value=MagicMock()), \
             patch("backend.agent_worker.AgentWorker", return_value=worker), \
             patch("backend.services.agent_usage_writer.publish_stage_configuration", side_effect=blocked_publish):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(processed.wait(0.5))
                self.assertTrue(write_started.wait(0.5))
            finally:
                release_write.set()
                thread.join(2)
        self.assertFalse(errors)
        self.assertFalse(thread.is_alive())

    def test_stalled_attempt_write_does_not_delay_execution(self):
        from backend.core.agent_worker import AgentWorker
        from backend.core.agent_state import RunPhase
        executor_type = type("CodexExecutor", (), {"__module__": "backend.core.codex_planning", "model": "model"})
        worker = AgentWorker(queue=MagicMock(), executor=executor_type(), worker_id="worker-1",
                             lease_seconds=30, heartbeat_seconds=10, max_attempts=3)
        release_write = threading.Event()
        started = threading.Event()

        def blocked_write(*_args, **_kwargs):
            started.set()
            release_write.wait(2)
            return True

        with patch("backend.services.agent_usage_writer.ensure_attempt", side_effect=blocked_write):
            try:
                begin = time.monotonic()
                worker._ensure_usage_attempt(SimpleNamespace(id="run", phase=RunPhase.PLANNING))
                self.assertLess(time.monotonic() - begin, 0.5)
                self.assertTrue(started.wait(1))
                self.assertFalse(worker.flush_usage_attempts(timeout_seconds=0.02))
            finally:
                release_write.set()
            self.assertTrue(worker.flush_usage_attempts(timeout_seconds=1))

    def test_long_execution_keeps_reporting_configuration(self):
        executing = threading.Event()
        finish_execution = threading.Event()
        reported_during_execution = threading.Event()
        reports = []
        errors = []

        def process_once():
            executing.set()
            if not finish_execution.wait(2):
                raise AssertionError("Test execution was not released")
            return True

        def publish(**settings):
            reports.append(settings)
            if executing.is_set() and not finish_execution.is_set():
                reported_during_execution.set()

        settings = SimpleNamespace(queue_environment="qa", deployment_environment=None,
                                   executor_name="codex-planning", worker_id="worker-1",
                                   codex_model="model-new", lease_seconds=30,
                                   heartbeat_seconds=10, max_attempts=3, run_once=True)
        queue = MagicMock()
        queue.verify_identity.return_value = ("qa", "worker")
        worker = MagicMock()
        worker.process_once.side_effect = process_once

        def run():
            try:
                run_worker(settings)
            except Exception as exc:
                errors.append(exc)

        with patch("backend.agent_worker.DatabaseAgentQueue", return_value=queue), \
             patch("backend.agent_worker.build_executor", return_value=MagicMock()), \
             patch("backend.agent_worker.AgentWorker", return_value=worker), \
             patch("backend.agent_worker.CONFIGURATION_REPORT_SECONDS", 0.01), \
             patch("backend.services.agent_usage_writer.publish_stage_configuration", side_effect=publish):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(executing.wait(1))
                self.assertTrue(reported_during_execution.wait(1))
            finally:
                finish_execution.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertGreaterEqual(len(reports), 2)
        self.assertTrue(all(report["model"] == "model-new" for report in reports))


class QueryTests(unittest.TestCase):
    def test_configuration_uses_fresh_worker_when_latest_report_is_future_dated(self):
        now = datetime.now(timezone.utc)
        class Cursor(_Cursor):
            def execute(self, sql, params=None):
                super().execute(sql, params)
                if "codex_stage_configuration" in sql:
                    self.description = [(name,) for name in
                                        ("stage", "configured_model", "configured_reasoning_effort", "reported_at")]
                    self.rows = [("planning", "active-model", "high", now - timedelta(seconds=30))] if "WHERE reported_at" in sql else [
                        ("planning", "future-model", "low", now + timedelta(minutes=10))]
                else:
                    self.rows = []

        with patch("backend.services.agent_usage_service._connection",
                   return_value=(_Connection(Cursor()), lambda _: None)):
            stages = {entry["stage"]: entry for entry in configuration()}
        self.assertEqual(stages["planning"]["configured_model"], "active-model")
        self.assertEqual(stages["planning"]["last_reported_configured_model"], "future-model")
        self.assertEqual(stages["planning"]["status"], "available")

    def test_all_successful_read_paths_end_transactions_before_pool_release(self):
        class Cursor(_Cursor):
            def fetchone(self):
                return (0,)

        connections = []

        def acquire():
            conn = _Connection(Cursor())
            connections.append(conn)

            def release(released):
                self.assertIs(released, conn)
                self.assertEqual(conn.rollbacks, 1)

            return conn, release

        start, end = period(None, None)
        with patch("backend.services.agent_usage_service._connection", side_effect=acquire):
            read_quota()
            summary(start, end)
            executions(start, end)
            configuration()
        self.assertEqual(len(connections), 4)
        self.assertTrue(all(conn.rollbacks == 1 for conn in connections))

    def test_configuration_keeps_verified_model_when_newer_attempt_is_unavailable(self):
        latest_attempt_at = datetime.now(timezone.utc)
        actual_model_at = latest_attempt_at - timedelta(days=1)

        class Cursor(_Cursor):
            def execute(self, sql, params=None):
                super().execute(sql, params)
                if "WHERE actual_model IS NOT NULL" in sql:
                    self.description = [(name,) for name in
                                        ("stage", "actual_model", "reasoning_effort", "recorded_at")]
                    self.rows = [("planning", "actual-old", "low", actual_model_at)]
                elif "codex_execution_usage" in sql:
                    self.description = [(name,) for name in ("stage", "recorded_at")]
                    self.rows = [("planning", latest_attempt_at)]
                else:
                    self.description = [(name,) for name in
                                        ("stage", "configured_model", "configured_reasoning_effort", "reported_at")]
                    self.rows = [("planning", "new-model", "high", datetime.now(timezone.utc)),
                                 ("implementation", "configured-no-run", None, datetime.now(timezone.utc))]
                    if "WHERE reported_at" not in sql:
                        self.rows.append(("review", "stale-model", "medium", datetime.now(timezone.utc) - timedelta(hours=1)))

        with patch("backend.services.agent_usage_service._connection",
                   return_value=(_Connection(Cursor()), lambda _: None)):
            stages = {entry["stage"]: entry for entry in configuration()}
        self.assertEqual(stages["planning"]["configured_model"], "new-model")
        self.assertEqual(stages["planning"]["actual_model_last_execution"], "actual-old")
        self.assertEqual(stages["planning"]["observed_at"], latest_attempt_at)
        self.assertEqual(stages["planning"]["actual_model_observed_at"], actual_model_at)
        self.assertEqual(stages["implementation"]["configured_model"], "configured-no-run")
        self.assertIsNone(stages["implementation"]["actual_model_last_execution"])
        self.assertEqual(stages["review"]["status"], "stale")
        self.assertIsNone(stages["review"]["configured_model"])
        self.assertEqual(stages["review"]["last_reported_configured_model"], "stale-model")
        self.assertEqual(stages["deployment"]["status"], "unsupported")

    def test_summary_groups_use_available_turns_once_and_typed_contract(self):
        class Cursor(_Cursor):
            def execute(self, sql, params=None):
                super().execute(sql, params)
                fields = ["execution_count", "available_count", "unavailable_count",
                          "input_tokens", "output_tokens", "cached_input_tokens",
                          "reasoning_tokens", "total_tokens"]
                group = []
                if "GROUP BY stage" in sql:
                    group = ["stage"]
                elif "GROUP BY card_id" in sql:
                    group = ["card_id"]
                elif "GROUP BY actual_model, reasoning_effort" in sql:
                    group = ["actual_model", "reasoning_effort"]
                self.description = [(name,) for name in group + fields]
                prefix = {"stage": ["review"],
                          "card_id": [UUID("00000000-0000-0000-0000-000000000001")],
                          "actual_model": ["model", "high"]}
                self.rows = [tuple((prefix.get(group[0], []) if group else []) +
                                   [2, 1, 1, 10, 2, 3, 1, 12])]

        cursor = Cursor()
        start, end = period(None, None)
        with patch("backend.services.agent_usage_service._connection",
                   return_value=(_Connection(cursor), lambda _: None)):
            data = summary(start, end)
        validated = CodexSummaryResponse.model_validate({"success": True, "data": data})
        self.assertEqual(validated.data.token_accounting.overall.total_tokens, 12)
        self.assertEqual(validated.data.token_accounting.by_stage[0].available_count, 1)
        self.assertEqual(len(cursor.calls), 4)
        self.assertTrue(all("FILTER (WHERE usage_status = 'available')" in sql
                            and params == (start, end) for sql, params in cursor.calls))

    def test_executions_filter_period_card_and_paginate(self):
        class Cursor(_Cursor):
            def fetchone(self):
                return (3,)

        cursor = Cursor()
        start, end = period(None, None)
        card = UUID("00000000-0000-0000-0000-000000000001")
        with patch("backend.services.agent_usage_service._connection",
                   return_value=(_Connection(cursor), lambda _: None)):
            data = executions(start, end, card_id=card, limit=1, offset=2)
        self.assertEqual((data["total"], data["limit"], data["offset"]), (3, 1, 2))
        self.assertEqual(cursor.calls[0][1], [start, end, card])
        self.assertEqual(cursor.calls[1][1], [start, end, card, 1, 2])
        self.assertIn("ORDER BY recorded_at DESC", cursor.calls[1][0])


class HttpUsageTests(unittest.TestCase):
    def test_actual_http_rejects_missing_and_non_admin_identity(self):
        async def inline_call(func, *args, **kwargs):
            # This sandbox cannot start AnyIO's worker thread pool. Execute
            # synchronous dependencies inline while retaining ASGI routing.
            return func(*args, **kwargs)

        async def check(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                missing = await client.get("/agent/usage/quota")
                self.assertEqual(missing.status_code, 401)
                app.dependency_overrides[require_current_principal] = lambda: AuthenticatedPrincipal(
                    id="u", firebase_uid="u", email="u@example.com", display_name=None, role="user")
                try:
                    denied = await client.get("/agent/usage/quota")
                    self.assertEqual(denied.status_code, 403)
                finally:
                    app.dependency_overrides.clear()

        app = FastAPI()
        app.include_router(router)
        with patch("fastapi.dependencies.utils.run_in_threadpool", inline_call), \
             patch("fastapi.routing.run_in_threadpool", inline_call):
            asyncio.run(check(app))

    def test_openapi_summary_nested_aggregates_are_typed(self):
        app = FastAPI()
        app.include_router(router)
        schemas = app.openapi()["components"]["schemas"]
        self.assertEqual(schemas["CodexSummaryData"]["properties"]["token_accounting"]["$ref"],
                         "#/components/schemas/CodexTokenAccounting")
        for field in ("overall", "by_stage", "by_card", "by_model"):
            self.assertIn(field, schemas["CodexTokenAccounting"]["properties"])

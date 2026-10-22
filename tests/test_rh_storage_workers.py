"""RH-Storage worker API tests without a live PostgreSQL connection."""

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

from backend.core.auth import (
    AuthenticatedPrincipal, require_admin_principal, require_current_principal,
)
from backend.models.rh_storage_worker_models import DefaultWorkersUpdate, WorkerBoostRequest
from backend.routers import rh_storage


NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
ADMIN = AuthenticatedPrincipal("1", "admin", "admin@example.test", None, "admin")
MEMBER = AuthenticatedPrincipal("2", "member", "member@example.test", None, "member")


class FakeConnection:
    def __init__(self):
        self.default_workers = 2
        self.boost_workers = None
        self.boost_until = None
        self.updated_at = NOW - timedelta(days=1)
        self.heartbeats = []
        self.queries = []
        self.fail = False
        self.closed = False

    def config(self):
        return {
            "default_workers": self.default_workers,
            "boost_workers": self.boost_workers,
            "boost_until": self.boost_until,
            "updated_at": self.updated_at,
            "effective_workers": self.boost_workers if self.boost_until and self.boost_until > NOW else self.default_workers,
            "server_now": NOW,
        }

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if self.fail:
            raise RuntimeError("private database details")
        if "UPDATE public.worker_scaling" in query:
            if "SET default_workers" in query:
                self.default_workers = args[0]
            elif "SET boost_workers = $1" in query:
                self.boost_workers = args[0]
                self.boost_until = NOW + timedelta(minutes=args[1])
            else:
                self.boost_workers = self.boost_until = None
            self.updated_at = NOW
        return self.config()

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        if self.fail:
            raise RuntimeError("private database details")
        if "public.worker_heartbeats" in query:
            return [row for row in self.heartbeats if row["last_seen_at"] >= NOW - timedelta(seconds=45)]
        return []

    async def close(self):
        self.closed = True


def heartbeat(worker_id, *, pid=200, hostname="host-a", age=10, state="idle", **changes):
    row = {
        "worker_id": worker_id, "hostname": hostname, "pid": pid,
        "state": state, "pool_id": "pool-1", "job_id": None,
        "last_seen_at": NOW - timedelta(seconds=age), "last_error": None,
        "retiring": False, "bytes_copied": None, "total_bytes": None,
        "job_started_at": None, "last_progress_at": None,
        "job_type": None, "rel_path": None, "pool_name": None,
        "pool_mountpoint": None, "source_branch_id": None,
        "source_branch_path": None, "target_branch_id": None,
        "target_branch_path": None, "source_branch_registered_path": None,
        "target_branch_registered_path": None,
    }
    row.update(changes)
    return row


class WorkerApiTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConnection()
        self.connect = patch.object(rh_storage.asyncpg, "connect", new=AsyncMock(return_value=self.conn))
        self.connect.start()
        self.environment = patch.dict(os.environ, {
            "REMIHUB_AUTH_MODE": "required",
            "RH_STORAGE_DATABASE_URL": "postgresql://test.invalid/rh_storage",
        })
        self.environment.start()
        self.principal = patch("backend.core.auth._principal_from_credentials", return_value=ADMIN)
        self.principal.start()
        self.addCleanup(self.connect.stop)
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.principal.stop)

    def request(self, method, path, **kwargs):
        return self._send(method, path, headers={"Authorization": "Bearer local-test"}, **kwargs)

    def _send(self, method, path, **kwargs):
        class Result:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self.body = body

            def json(self):
                return self.body

        try:
            auth = kwargs.get("headers", {}).get("Authorization")
            credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=auth[7:]) if auth else None
            principal = require_current_principal(credentials)
            if method not in {"GET"} and path != "/rh-storage/status":
                require_admin_principal(principal)
            endpoints = {
                ("GET", "/rh-storage/workers/config"): rh_storage.get_worker_config,
                ("PATCH", "/rh-storage/workers/config"): rh_storage.patch_worker_config,
                ("POST", "/rh-storage/workers/boost"): rh_storage.activate_worker_boost,
                ("DELETE", "/rh-storage/workers/boost"): rh_storage.clear_worker_boost,
                ("GET", "/rh-storage/workers"): rh_storage.get_workers,
                ("GET", "/rh-storage/status"): rh_storage.get_rh_storage_status,
            }
            endpoint = endpoints[(method, path)]
            body = kwargs.get("json")
            if method == "PATCH":
                result = asyncio.run(endpoint(DefaultWorkersUpdate.model_validate(body)))
            elif method == "POST":
                result = asyncio.run(endpoint(WorkerBoostRequest.model_validate(body)))
            else:
                result = asyncio.run(endpoint())
            return Result(200, result)
        except HTTPException as exc:
            return Result(exc.status_code, {"detail": exc.detail})
        except ValidationError as exc:
            return Result(422, {"detail": exc.errors()})

    def test_config_read_and_expired_boost(self):
        response = self.request("GET", "/rh-storage/workers/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["effective_workers"], 2)
        self.assertEqual(response.json()["data"]["server_now"], NOW.isoformat())
        self.conn.boost_workers = 5
        self.conn.boost_until = NOW - timedelta(seconds=1)
        expired = self.request("GET", "/rh-storage/workers/config").json()["data"]
        self.assertEqual(expired["effective_workers"], 2)
        self.assertFalse(expired["boost_active"])

    def test_admin_update_boost_and_clear(self):
        updated = self.request("PATCH", "/rh-storage/workers/config", json={"default_workers": 3})
        self.assertEqual(updated.json()["data"]["default_workers"], 3)
        boosted = self.request("POST", "/rh-storage/workers/boost", json={"workers": 5, "duration_minutes": 120})
        self.assertEqual(boosted.status_code, 200)
        self.assertEqual(boosted.json()["data"]["effective_workers"], 5)
        self.assertEqual(boosted.json()["data"]["boost_until"], (NOW + timedelta(hours=2)).isoformat())
        self.assertEqual(boosted.json()["data"]["updated_at"], NOW.isoformat())
        cleared = self.request("DELETE", "/rh-storage/workers/boost")
        self.assertEqual(cleared.json()["data"]["effective_workers"], 3)
        self.assertIsNone(cleared.json()["data"]["boost_workers"])
        self.assertIsNone(cleared.json()["data"]["boost_until"])
        writes = [(query, args) for query, args in self.conn.queries if "UPDATE public.worker_scaling" in query]
        self.assertEqual([args for _, args in writes], [(3,), (5, 120), ()])
        self.assertTrue(all("updated_at = NOW()" in query for query, _ in writes))

    def test_validation(self):
        for count in (0, 17, "5", True):
            with self.subTest(count=count), self.assertRaises(ValidationError):
                DefaultWorkersUpdate(default_workers=count)
        for duration in (0, 30, 241, "120"):
            with self.subTest(duration=duration), self.assertRaises(ValidationError):
                WorkerBoostRequest(workers=5, duration_minutes=duration)
        for duration in (15, 60, 120, 240):
            self.assertEqual(WorkerBoostRequest(workers=5, duration_minutes=duration).duration_minutes, duration)
        invalid = self.request("POST", "/rh-storage/workers/boost", json={"workers": 17, "duration_minutes": 120})
        self.assertEqual(invalid.status_code, 422)

    def test_authentication_and_admin_authorization(self):
        guards = {
            (route.path, method): {dependency.call for dependency in route.dependant.dependencies}
            for route in rh_storage.router.routes
            for method in route.methods
        }
        for method, path in (("GET", "/rh-storage/workers"), ("GET", "/rh-storage/workers/config")):
            self.assertIn(require_current_principal, guards[(path, method)])
        for method, path in (("PATCH", "/rh-storage/workers/config"),
                             ("POST", "/rh-storage/workers/boost"),
                             ("DELETE", "/rh-storage/workers/boost")):
            self.assertIn(require_admin_principal, guards[(path, method)])
        for mode in ("transition", "required"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"REMIHUB_AUTH_MODE": mode}):
                self.assertEqual(self._send("GET", "/rh-storage/workers").status_code, 401)
                self.assertEqual(self._send("PATCH", "/rh-storage/workers/config", json={"default_workers": 3}).status_code, 401)
                with patch("backend.core.auth._principal_from_credentials", return_value=MEMBER):
                    self.assertEqual(self.request("GET", "/rh-storage/workers/config").status_code, 200)
                    self.assertEqual(self.request("PATCH", "/rh-storage/workers/config", json={"default_workers": 3}).status_code, 403)
                    self.assertEqual(self.request("POST", "/rh-storage/workers/boost", json={"workers": 5, "duration_minutes": 15}).status_code, 403)
                    self.assertEqual(self.request("DELETE", "/rh-storage/workers/boost").status_code, 403)

    def test_restart_overlap_retirement_progress_and_payload(self):
        self.conn.heartbeats = [
            heartbeat("old", pid=100, age=20, state="copying"),
            heartbeat("copy", age=5, state="copying", retiring=True,
                      job_id="job-1", job_type="replicate", rel_path="docs/file.bin",
                      pool_name="secure", pool_mountpoint="/mnt/secure-pool",
                      source_branch_id="7", source_branch_path="/disk/source",
                      target_branch_id="8", target_branch_path="/disk/target",
                      source_branch_registered_path="/disk/source",
                      target_branch_registered_path="/disk/target",
                      bytes_copied=50, total_bytes=200,
                      job_started_at=NOW - timedelta(seconds=60),
                      last_progress_at=NOW - timedelta(seconds=3)),
            heartbeat("idle", age=10),
            heartbeat("stale", age=46, state="copying"),
        ]
        response = self.request("GET", "/rh-storage/workers")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["observed_daemon_pid"], 200)
        self.assertEqual(data["counts"], {"active": 2, "busy": 1, "idle": 1,
                                          "retiring": 1, "healthy": 2, "unhealthy": 0,
                                          "observed_gap": 0})
        worker = next(row for row in data["workers"] if row["worker_id"] == "copy")
        self.assertEqual(worker["source_branch_path"], "/disk/source")
        self.assertEqual(worker["target_branch_path"], "/disk/target")
        self.assertEqual(worker["rel_path"], "docs/file.bin")
        self.assertEqual(worker["progress_percent"], 25.0)
        self.assertEqual(worker["job_elapsed_seconds"], 60.0)
        self.assertEqual(worker["last_progress_age_seconds"], 3.0)
        self.assertIsNone(data["workers"][1]["progress_percent"])
        query = next(query for query, _ in self.conn.queries if "public.worker_heartbeats" in query)
        for expression in ("j.payload ->> 'source_branch_path'", "j.payload ->> 'target_branch_path'",
                           "j.target_branch_id", "source_branch.branch_id", "target_branch.branch_id"):
            self.assertIn(expression, query)

    def test_no_workers_ambiguous_and_scale_down(self):
        empty = self.request("GET", "/rh-storage/workers").json()["data"]
        self.assertEqual(empty["monitoring_status"], "no_recent_heartbeats")
        self.assertEqual(empty["counts"]["active"], 0)
        self.conn.heartbeats = [heartbeat("one", age=5, pid=100), heartbeat("two", age=5, pid=200)]
        ambiguous = self.request("GET", "/rh-storage/workers").json()["data"]
        self.assertEqual(ambiguous["monitoring_status"], "ambiguous")
        self.assertIsNone(ambiguous["counts"])
        self.conn.heartbeats = [heartbeat(str(i), age=5+i) for i in range(4)]
        scaled = self.request("GET", "/rh-storage/workers").json()["data"]
        self.assertEqual(scaled["counts"]["active"], 4)
        self.assertEqual(scaled["counts"]["unhealthy"], 0)

    def test_freshness_boundary_and_missing_job_fields(self):
        self.conn.heartbeats = [
            heartbeat("fresh", age=45, state="recovering", job_id="orphan",
                      last_error="retrying"),
            heartbeat("stale", age=45.1, state="copying"),
        ]
        data = self.request("GET", "/rh-storage/workers").json()["data"]
        self.assertEqual(data["counts"]["active"], 1)
        self.assertEqual(data["counts"]["unhealthy"], 1)
        worker = data["workers"][0]
        self.assertEqual(worker["job_id"], "orphan")
        self.assertIsNone(worker["job_type"])
        self.assertIsNone(worker["source_branch_path"])
        self.assertIsNone(worker["target_branch_path"])
        self.assertIsNone(worker["progress_percent"])

    def test_database_errors_and_legacy_status_contract(self):
        self.conn.fail = True
        failed = self.request("GET", "/rh-storage/workers")
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn("private", str(failed.json()))
        with patch.object(rh_storage.asyncpg, "connect", new=AsyncMock(side_effect=RuntimeError("private connection details"))):
            disconnected = self.request("GET", "/rh-storage/workers/config")
        self.assertEqual(disconnected.status_code, 503)
        self.assertNotIn("private", str(disconnected.json()))
        self.conn.fail = False
        with patch.object(rh_storage, "systemd_status_for_rh_storage_compat", return_value={"active": True}):
            legacy = self.request("GET", "/rh-storage/status")
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(set(legacy.json()), {"success", "updated_at", "service", "pools", "recent_jobs"})


if __name__ == "__main__":
    unittest.main()

"""Offline contract, migration, and transactional safety checks for Autographs V2."""
from __future__ import annotations

import io
import asyncio
import unittest
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import HTTPException, UploadFile
from PIL import Image
from psycopg2 import pool
from psycopg2 import IntegrityError
from pydantic import ValidationError


class OfflinePool:
    def __init__(self, *_args, **_kwargs): pass
    def getconn(self): raise RuntimeError("No database access in offline autograph tests")
    def putconn(self, _conn): pass


pool.ThreadedConnectionPool = OfflinePool

from backend.models import autographs as m
from backend.routers import autographs as router
from backend.services import autograph_service as service
from backend.scripts.autographs_v2_backfill import BACKFILL_SQL, run as run_backfill
from backend.scripts.verify_autographs_v2 import audit
from backend.core.agent_deployment import GitBackendDeploymentManager

HELMET = UUID("11111111-1111-4111-8111-111111111111")
DRIVER = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 5, 15, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def execute(self, sql, args=()):
        self.calls.append((sql, args))

    def fetchone(self):
        return next(self.responses)

    def fetchall(self):
        return next(self.responses)


class AutographV2Tests(unittest.TestCase):
    def test_migration_is_additive_and_backfill_is_operator_guarded(self):
        root = Path("backend/database/migrations")
        up = root / "0024_autographs_v2.up.sql"
        down = root / "0024_autographs_v2.down.sql"
        GitBackendDeploymentManager._validate_migration_sql(up, direction="up")
        GitBackendDeploymentManager._validate_migration_sql(down, direction="down")
        GitBackendDeploymentManager._validate_migration_pair(up, down)
        sql = up.read_text()
        self.assertIn("ADD COLUMN helmet_id uuid", sql)
        self.assertIn("ADD COLUMN driver_id uuid", sql)
        self.assertNotIn("DROP TABLE public.autograph_entries", sql)
        backfill = BACKFILL_SQL
        self.assertIn("total <> 31", backfill)
        self.assertIn("normalized_count <> 30", backfill)
        self.assertIn("ON CONFLICT (year) DO NOTHING", backfill)
        self.assertIn("ON CONFLICT (normalized_name) DO NOTHING", backfill)
        self.assertIn("CREATE TRIGGER autograph_link_legacy_insert", backfill)
        self.assertIn("IF NEW.helmet_id IS NOT NULL AND NEW.driver_id IS NOT NULL THEN RETURN NEW", backfill)
        self.assertIn("CHECK (helmet_id IS NOT NULL AND driver_id IS NOT NULL) NOT VALID", backfill)
        self.assertIn("VALIDATE CONSTRAINT autograph_entries_associations_required", backfill)
        self.assertLess(backfill.index("CREATE TRIGGER autograph_link_legacy_insert"),
                        backfill.index("VALIDATE CONSTRAINT autograph_entries_associations_required"))
        self.assertNotIn("SET date_signed", backfill)

    def test_operator_backfill_commits_or_rolls_back_as_one_unit(self):
        class Conn:
            def __init__(self, fail=False):
                self.fail = fail
                self.commits = 0
                self.rollbacks = 0
                self.sql = None
            def cursor(self):
                @contextmanager
                def cursor():
                    class Cur:
                        def execute(_self, sql):
                            self.sql = sql
                            if self.fail:
                                raise RuntimeError("backfill rejected")
                    yield Cur()
                return cursor()
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1
        success = Conn()
        run_backfill(success)
        self.assertEqual((success.commits, success.rollbacks), (1, 0))
        self.assertIn("CREATE TRIGGER autograph_link_legacy_insert", success.sql)
        failure = Conn(fail=True)
        with self.assertRaisesRegex(RuntimeError, "backfill rejected"):
            run_backfill(failure)
        self.assertEqual((failure.commits, failure.rollbacks), (0, 1))

    def test_models_reject_invalid_driver_choice_view_and_coordinates(self):
        common = dict(helmet_view="front", x_percent=0.5, y_percent=1)
        self.assertIsNone(m.ScopedAutographCreate(driver_name="Alex Palou", **common).date_signed)
        for values in ({}, {"driver_id": DRIVER, "driver_name": "Alex Palou"}, {"driver_name": "   "}):
            with self.assertRaises(ValidationError):
                m.ScopedAutographCreate(**common, **values)
        with self.assertRaises(ValidationError):
            m.ScopedAutographCreate(driver_id=DRIVER, helmet_view="top", x_percent=0.5, y_percent=0.5)
        with self.assertRaises(ValidationError):
            m.ScopedAutographCreate(driver_id=DRIVER, helmet_view="front", x_percent=float("nan"), y_percent=0.5)
        with self.assertRaises(ValidationError):
            m.HelmetCreate(year=2027, display_name="  ")

    def test_driver_lookup_reuses_case_and_whitespace_equivalent_identity(self):
        cur = Cursor([None, {"id": DRIVER, "display_name": "Alex Palou"}])
        row = service._driver(cur, None, "  ALEX   PALOU  ")
        self.assertEqual(row["id"], DRIVER)
        self.assertEqual(cur.calls[0][1], ("ALEX PALOU", "alex palou"))
        self.assertIn("ON CONFLICT", cur.calls[0][0])
        self.assertEqual(cur.calls[1][1], ("alex palou",))

    def test_autograph_creation_uses_one_transaction_and_preserves_multiple_signatures(self):
        expected = dict(id=42, driver_name="Alex Palou", image_path="", helmet_view="front",
                        x_percent=0.2, y_percent=0.7, region=None, notes=None,
                        created_at=NOW, helmet_id=HELMET, driver_id=DRIVER, date_signed=None)
        cur = Cursor([{"id": HELMET}, {"id": 42}, expected])
        @contextmanager
        def media(_path): yield cur, object()
        request = m.ScopedAutographCreate(driver_id=DRIVER, helmet_view="front", x_percent=0.2, y_percent=0.7)
        with patch.object(service, "transaction", side_effect=AssertionError("second pooled connection")), \
             patch.object(service, "_media_lock", media), \
             patch.object(service, "_driver", return_value={"id": DRIVER, "display_name": "Alex Palou"}):
            result = service.create_autograph(HELMET, request)
        self.assertEqual(result["id"], 42)
        insert = next((sql, args) for sql, args in cur.calls if "INSERT INTO public.autograph_entries" in sql)
        self.assertEqual(insert[1][0:2], ("Alex Palou", ""))
        self.assertEqual(insert[1][-1], None)
        self.assertIn("FOR UPDATE", cur.calls[0][0])
        self.assertNotIn("ON CONFLICT", insert[0])

    def test_helmet_creation_is_empty_and_duplicate_year_conflicts(self):
        cur = Cursor([{"id": HELMET, "year": 2027, "display_name": "2027 Helmet",
                       "description": None, "uses_legacy_images": False, "created_at": NOW}])
        @contextmanager
        def tx(): yield cur
        with patch.object(service, "transaction", tx):
            helmet = service.create_helmet(m.HelmetCreate(year=2027, display_name="2027 Helmet"))
        self.assertEqual(helmet["year"], 2027)
        self.assertEqual(helmet["uses_legacy_images"], False)
        self.assertFalse(any("autograph_entries" in sql for sql, _ in cur.calls))
        @contextmanager
        def conflicting_tx(): raise IntegrityError("duplicate year"); yield
        with patch.object(service, "transaction", conflicting_tx):
            with self.assertRaises(HTTPException) as error:
                service.create_helmet(m.HelmetCreate(year=2027, display_name="Duplicate"))
        self.assertEqual(error.exception.status_code, 409)

    def test_optional_signing_date_is_distinct_from_creation_time(self):
        signing = date(2027, 5, 20)
        request = m.ScopedAutographCreate(driver_id=DRIVER, helmet_view="left", x_percent=0.1,
                                           y_percent=0.2, date_signed=signing)
        self.assertEqual(request.date_signed, signing)
        self.assertNotEqual(request.date_signed, NOW.date())

    def test_transaction_rolls_back_failed_driver_and_autograph_insert(self):
        class Conn:
            committed = False
            rolled_back = False
            returned = False
            def cursor(self, **_kwargs):
                @contextmanager
                def context():
                    yield object()
                return context()
            def commit(self): self.committed = True
            def rollback(self): self.rolled_back = True
        conn = Conn()
        with patch.object(service, "get_db_conn", return_value=conn), patch.object(service, "put_db_conn") as put:
            with self.assertRaisesRegex(RuntimeError, "insert failed"):
                with service.transaction():
                    raise RuntimeError("insert failed")
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)
        put.assert_called_once_with(conn)

    def test_media_lock_uses_one_connection_through_commit_and_cleanup(self):
        calls = []
        class Cur:
            def execute(self, sql, _args=()): calls.append(sql)
        class Conn:
            def cursor(self, **_kwargs):
                @contextmanager
                def context(): yield Cur()
                return context()
            def commit(self): calls.append("commit")
            def rollback(self): calls.append("rollback")
        conn = Conn()
        path = "/autographs/image/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jpg"
        with patch.object(service, "get_db_conn", return_value=conn) as get, \
             patch.object(service, "put_db_conn") as put:
            with service._media_lock(path) as (cur, held):
                self.assertIs(held, conn)
                cur.execute("DELETE FROM public.autograph_entries")
                held.commit()
                cur.execute("SELECT references")
        get.assert_called_once_with()
        put.assert_called_once_with(conn)
        self.assertLess(calls.index("DELETE FROM public.autograph_entries"), calls.index("SELECT references"))
        self.assertLess(calls.index("SELECT references"), next(i for i, value in enumerate(calls) if "pg_advisory_unlock" in value))

    def test_three_simultaneous_media_locks_fit_five_connection_pool(self):
        class Cur:
            def execute(self, _sql, _args=()): pass
        class Conn:
            def cursor(self, **_kwargs):
                @contextmanager
                def context(): yield Cur()
                return context()
            def commit(self): pass
            def rollback(self): pass
        available = [Conn() for _ in range(5)]
        checked_out = []
        def take():
            conn = available.pop()
            checked_out.append(conn)
            return conn
        def give(conn):
            checked_out.remove(conn)
            available.append(conn)
        with patch.object(service, "get_db_conn", side_effect=take) as get, \
             patch.object(service, "put_db_conn", side_effect=give):
            with ExitStack() as stack:
                for index in range(3):
                    stack.enter_context(service._media_lock(f"image-{index}"))
                self.assertEqual(len(checked_out), 3)
                self.assertEqual(len(available), 2)
            self.assertEqual(get.call_count, 3)
            self.assertEqual(len(checked_out), 0)

    def test_deletion_cleans_only_after_commit_and_checks_references(self):
        stages = []
        class Cur:
            def __init__(self): self.calls = []
            def execute(self, sql, args=()): self.calls.append(sql)
            def fetchone(self): return {"image_path": "/autographs/image/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jpg"}
        @contextmanager
        def tx():
            stages.append("begin")
            yield Cur()
            stages.append("commit")
        @contextmanager
        def media(_path):
            stages.append("lock")
            class Conn:
                def commit(self): stages.append("commit_delete")
            yield Cur(), Conn()
            stages.append("unlock")
        with patch.object(service, "transaction", tx), patch.object(service, "_media_lock", media), \
             patch.object(service, "_remove_if_unreferenced", side_effect=lambda _cur, _path: stages.append("cleanup")):
            self.assertEqual(service.delete_autograph(7), {"success": True, "deleted_id": 7})
        self.assertEqual(stages, ["begin", "commit", "lock", "commit_delete", "cleanup", "unlock"])

    def test_rename_conflict_and_merge_preserve_autograph_rows(self):
        cur = Cursor([[{"id": DRIVER, "display_name": "Alex Palou"},
                       {"id": HELMET, "display_name": "Alex"}]])
        @contextmanager
        def tx(): yield cur
        with patch.object(service, "transaction", tx):
            merged = service.merge_drivers(DRIVER, HELMET)
        self.assertEqual(merged["id"], DRIVER)
        self.assertIn("UPDATE public.autograph_entries SET driver_id", cur.calls[1][0])
        self.assertIn("DELETE FROM public.autograph_drivers", cur.calls[2][0])
        self.assertNotIn("DELETE FROM public.autograph_entries", " ".join(sql for sql, _ in cur.calls))
        with self.assertRaises(HTTPException) as error:
            service.merge_drivers(DRIVER, DRIVER)
        self.assertEqual(error.exception.status_code, 422)

    def test_helmet_image_replacement_rejects_existing_coordinates(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            image_name = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jpg"
            (path / image_name).write_bytes(b"image")
            image_url = "/autographs/image/" + image_name
            cur = Cursor([{"id": HELMET}, {"id": HELMET}, {"n": 1}])
            @contextmanager
            def tx(): yield cur
            @contextmanager
            def media(_path): yield cur, object()
            with patch.object(service, "transaction", tx), patch.object(service, "_media_lock", media), \
                 patch.object(service, "_save_image", new=AsyncMock(return_value=(image_url, 5))), \
                 patch.object(service, "_remove_if_unreferenced") as cleanup:
                with self.assertRaises(HTTPException) as error:
                    asyncio.run(service.upload_helmet_image(HELMET, "front", UploadFile(file=io.BytesIO(), filename="view.jpg")))
            self.assertEqual(error.exception.status_code, 409)
            cleanup.assert_called_once_with(cur, image_url)
            self.assertIn("FOR UPDATE", cur.calls[1][0])

    def test_history_and_stats_query_distinguish_signatures_and_people(self):
        history = Cursor([{"id": DRIVER}, []])
        stats = Cursor([{"total_signatures": 3, "unique_signers": 1}])
        @contextmanager
        def history_tx(): yield history
        @contextmanager
        def stats_tx(): yield stats
        with patch.object(service, "transaction", history_tx):
            self.assertEqual(service.driver_history(DRIVER), [])
        self.assertIn("ORDER BY h.year DESC,a.created_at DESC NULLS LAST,a.id DESC", history.calls[1][0])
        with patch.object(service, "transaction", stats_tx):
            self.assertEqual(service.collection_stats()["unique_signers"], 1)
        self.assertIn("count(DISTINCT driver_id)", stats.calls[0][0])

    def test_installed_android_json_contract_and_original_helmet_filter(self):
        row = dict(id=7, driver_name="Alex Palou", image_path="", helmet_view="front",
                   x_percent=0.3, y_percent=0.4, region=None, notes=None, created_at=NOW)
        with patch.object(service, "add_autograph", return_value=m.AutographEntry(**row)), \
             patch.object(service, "get_all_autographs", return_value=[m.AutographEntry(**row)]):
            request = m.AutographCreate(**{key: row[key] for key in
                ("driver_name", "image_path", "helmet_view", "x_percent", "y_percent", "region", "notes")})
            created = router.add_autograph(request).model_dump(mode="json")
            listed = [item.model_dump(mode="json") for item in router.get_all_autographs()]
        self.assertEqual(created["image_path"], "")
        self.assertIsInstance(created["driver_name"], str)
        self.assertEqual(created, listed[0])
        cur = Cursor([[]])
        @contextmanager
        def tx(): yield cur
        with patch.object(service, "transaction", tx):
            service.get_all_autographs()
        self.assertIn("WHERE h.year=2026", cur.calls[0][0])

    def test_image_reference_validation_and_content_type(self):
        with self.assertRaises(HTTPException):
            service._image_path("/etc/passwd")
        with self.assertRaises(HTTPException):
            service._file_for_name("../file.jpg")
        self.assertEqual(service._image_path(None), "")

    def test_upload_preserves_legacy_image_response_and_checks_content(self):
        class File:
            def __init__(self, contents, content_type):
                self.contents = contents
                self.content_type = content_type
            async def read(self, _limit): return self.contents
        image = Image.new("RGB", (4, 4), "blue")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG")
        payload = buffer.getvalue()
        with TemporaryDirectory() as directory, patch.object(service, "AUTOGRAPH_IMAGE_DIR", Path(directory)):
            uploaded = asyncio.run(service.upload_autograph_image(File(payload, "image/jpeg")))
            self.assertTrue(uploaded["success"])
            self.assertEqual(uploaded["image_path"], "/autographs/image/" + uploaded["filename"])
            self.assertEqual((Path(directory) / uploaded["filename"]).read_bytes(), payload)
            with self.assertRaises(HTTPException):
                asyncio.run(service.upload_autograph_image(File(payload, "image/png")))

    def test_operator_audit_checks_exact_files_and_legacy_digest(self):
        class AuditCur:
            def __init__(self, rows): self.rows = rows; self.query = ""
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, sql): self.query = sql
            def fetchall(self): return self.rows
            def fetchone(self): return (30,)
        class Conn:
            def __init__(self, rows): self.rows = rows
            def cursor(self): return AuditCur(self.rows)
        with TemporaryDirectory() as directory:
            image_path = "/autographs/image/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jpg"
            row = (1, "Alex Palou", image_path, "front", 0.5, 0.5, None, None, NOW)
            result = audit(Conn([row] * 31), Path(directory), "before")
            self.assertIn(image_path, result["missing_or_invalid_photo_references"])
            self.assertIn("one or more photo references", " ".join(result["failures"]))
            (Path(directory) / image_path.rsplit("/", 1)[1]).write_bytes(b"photo")
            result2 = audit(Conn([row] * 31), Path(directory), "before", result["legacy_columns_sha256"])
            self.assertFalse(result2["missing_or_invalid_photo_references"])
            self.assertEqual(result2["legacy_columns_sha256"], result["legacy_columns_sha256"])

    def test_operator_post_audit_requires_validated_association_constraint(self):
        class Cur:
            def __init__(self, validated):
                self.responses = iter([(30,), (31,), (0,), (30,), (1,), (validated,)])
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, _sql): pass
            def fetchall(self):
                return [(i, "Driver", "", "front", 0.5, 0.5, None, None, NOW) for i in range(31)]
            def fetchone(self): return next(self.responses)
        class Conn:
            def __init__(self, validated): self.validated = validated
            def cursor(self): return Cur(self.validated)
        with TemporaryDirectory() as directory:
            valid = audit(Conn(True), Path(directory), "after")
            self.assertTrue(valid["association_constraint_validated"])
            self.assertEqual(valid["failures"], ["nonblank_photo_references: expected 29, found 0"])
            invalid = audit(Conn(False), Path(directory), "after")
            self.assertFalse(invalid["association_constraint_validated"])
            self.assertIn("autograph association constraint is missing, invalid, or unvalidated", invalid["failures"])


if __name__ == "__main__":
    unittest.main()

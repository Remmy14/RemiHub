from __future__ import annotations

import io
import os
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID

from fastapi import FastAPI
import httpx
from PIL import Image
from psycopg2 import pool


class OfflinePool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise RuntimeError("No database access in offline Adventures tests")

    def putconn(self, _conn):
        pass


pool.ThreadedConnectionPool = OfflinePool

from backend.core.auth import AuthenticatedPrincipal, require_current_principal
from backend.models import adventures_models as models
from backend.routers import adventures
from backend.services import adventures_media as media, adventures_service as service


USER = AuthenticatedPrincipal(
    id="11111111-1111-4111-8111-111111111111",
    firebase_uid="test", email="test@example.invalid", display_name="Test", role="member",
)
TRIP = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


def trip_row(**overrides):
    base = models.TripInput(title="Fri to Mon", arrival_date=date(2026, 11, 20),
                            departure_date=date(2026, 11, 23), intent=models.Intent.BOOKED).model_dump(mode="python")
    base.update(id=TRIP, user_id=USER.id, cover_photo_id=None, campground_name=None,
                created_at=NOW, updated_at=NOW)
    base.update(overrides)
    return base


class AdventureCalendarTests(unittest.TestCase):
    def test_friday_to_monday_and_lifecycle(self):
        row = trip_row()
        upcoming = service.present_trip(row, date(2026, 11, 16))
        self.assertEqual((upcoming["nights"], upcoming["calendar_days"], upcoming["days_until_arrival"]), (3, 4, 4))
        self.assertEqual((upcoming["intent"], upcoming["state"], upcoming["classification"]),
                         (models.Intent.BOOKED, "Booked", "upcoming"))
        active = service.present_trip(row, date(2026, 11, 21))
        self.assertEqual((active["state"], active["trip_day"], active["intent"]), ("In Progress", 2, models.Intent.BOOKED))
        past = service.present_trip(row, date(2026, 11, 24))
        self.assertEqual((past["state"], past["intent"]), ("Completed", models.Intent.BOOKED))

    def test_undated_cancelled_same_day_and_overlap(self):
        self.assertEqual(service.present_trip(trip_row(arrival_date=None, departure_date=None), date(2026, 11, 16))["classification"], "undated")
        self.assertEqual(service.present_trip(trip_row(intent="Cancelled"), date(2026, 11, 21))["state"], "Cancelled")
        same = service.present_trip(trip_row(arrival_date=date(2026, 11, 16), departure_date=date(2026, 11, 16)), date(2026, 11, 16))
        self.assertEqual((same["nights"], same["calendar_days"], same["trip_day"]), (0, 1, 1))

    def test_calendar_intersection_query_and_bound(self):
        class CalendarCursor:
            def __init__(self): self.query = None; self.args = None
            def execute(self, query, args): self.query = query; self.args = args
            def fetchall(self): return [trip_row(arrival_date=date(2026, 11, 30), departure_date=date(2026, 12, 2))]
        cur = CalendarCursor()
        @contextmanager
        def tx(): yield cur
        with patch.object(service, "transaction", tx), patch.object(service, "today", return_value=date(2026, 11, 16)):
            items = service.calendar(USER.id, date(2026, 12, 1), date(2026, 12, 31))
        self.assertEqual(items[0]["arrival_date"], date(2026, 11, 30))
        self.assertIn("t.arrival_date<=%s AND t.departure_date>=%s", cur.query)
        self.assertEqual(cur.args, (USER.id, date(2026, 12, 31), date(2026, 12, 1)))
        with self.assertRaises(ValueError):
            service.calendar(USER.id, date(2026, 1, 1), date(2027, 12, 31))

    def test_dates_validation_and_update_wire_model(self):
        for start, end in [(None, "2026-11-23"), ("2026-11-23", None), ("2026-11-23", "2026-11-20")]:
            with self.assertRaises(ValueError):
                service.dates_valid(start, end)
        self.assertNotIn("departure_date", models.TripPatch(arrival_date=date(2026, 12, 1)).model_dump(exclude_unset=True))
        with self.assertRaises(ValueError):
            models.RatingInput(overall=0)
        with self.assertRaises(ValueError):
            models.RatingInput(family_friendliness=6)
        self.assertIsNone(models.RatingInput().overall)
        with self.assertRaises(ValueError):
            models.TripInput(title="Bad", arrival_date="2026-11-20T00:00:00Z")

    def test_timezone_is_explicit_and_checked_before_write(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(service.ConfigurationError):
                service.create_trip(USER.id, {"title": "test"})
        with patch.dict(os.environ, {"REMIHUB_ADVENTURES_TIMEZONE": "America/New_York"}):
            self.assertIsInstance(service.today(), date)

    def test_dashboard_year_denominators_and_empty_state(self):
        class Cursor:
            def __init__(self): self.queries = []; self.fetchall_count = 0
            def execute(self, query, args): self.queries.append((query, args))
            def fetchall(self): self.fetchall_count += 1; return []
            def fetchone(self):
                return {"total_planned": 0, "upcoming": 0, "active": 0, "completed": 0,
                        "planned_nights": 0, "completed_nights": 0, "distinct_visited_campgrounds": 0}
        cur = Cursor()
        @contextmanager
        def tx(): yield cur
        with patch.object(service, "transaction", tx), patch.object(service, "today", return_value=date(2026, 11, 16)):
            result = service.dashboard(USER.id, 2026, 20)
        self.assertIsNone(result["next_trip"])
        self.assertEqual(result["summary"]["completed_nights"], 0)
        self.assertIn("count(DISTINCT campground_id) FILTER (WHERE departure_date<%s)", cur.queries[2][0])
        self.assertIn("intent<>'Cancelled'", cur.queries[2][0])
        self.assertEqual(cur.queries[2][1][-2:], (date(2026, 1, 1), date(2027, 1, 1)))

    def test_checklist_instantiation_is_single_copy(self):
        class Cursor:
            def __init__(self, inserted): self.inserted=inserted; self.queries=[]
            def execute(self, query, args): self.queries.append(query)
            def fetchone(self): return {"trip_id": TRIP} if self.inserted else None
        @contextmanager
        def tx_for(cur): yield cur
        for inserted in (True, False):
            cur = Cursor(inserted)
            with patch.object(service, "transaction", lambda: tx_for(cur)), \
                 patch.object(service, "assert_trip", return_value=trip_row()), \
                 patch.object(service, "get_checklist", return_value={"instantiated": True, "items": [], "completed_count": 0, "total_count": 0, "percent": 0}):
                result = service.instantiate_checklist(USER.id, str(TRIP))
            copies = [q for q in cur.queries if "INSERT INTO public.adventures_trip_items" in q]
            self.assertEqual(len(copies), int(inserted))
            self.assertEqual(result["percent"], 0)

    def test_checklist_completion_counts_and_explicit_reset(self):
        class Cursor:
            def __init__(self): self.queries=[]; self.reset=False
            def execute(self, query, args):
                self.queries.append(query)
                if "SET completed=false" in query: self.reset=True
            def fetchone(self): return {"exists": 1}
            def fetchall(self):
                return [{"completed": not self.reset}, {"completed": False}]
        cur = Cursor()
        @contextmanager
        def tx(): yield cur
        with patch.object(service, "transaction", tx), patch.object(service, "assert_trip", return_value=trip_row()):
            checklist = service.get_checklist(USER.id, str(TRIP))
            reset = service.reset_checklist(USER.id, str(TRIP))
        self.assertEqual((checklist["completed_count"], checklist["total_count"], checklist["percent"]), (1, 2, 50))
        self.assertEqual(reset["percent"], 0)
        self.assertTrue(any("SET completed=false" in q for q in cur.queries))

    def test_campground_delete_rejects_referenced_trip(self):
        class Cursor:
            def execute(self, query, args): pass
            def fetchone(self): return {"exists": 1}
        @contextmanager
        def tx(): yield Cursor()
        with patch.object(service, "transaction", tx), patch.object(service, "assert_campground", return_value={}):
            with self.assertRaises(service.Conflict):
                service.delete_campground(USER.id, str(TRIP))

    def test_cross_trip_memory_photo_is_rejected(self):
        class Cursor:
            def execute(self, query, args): pass
            def fetchall(self): return []
        with self.assertRaises(service.NotFound):
            service._memory_photos(Cursor(), str(TRIP), str(TRIP), [str(UUID(int=3))])


class AdventureMediaTests(unittest.TestCase):
    def image_bytes(self, fmt="JPEG"):
        output = io.BytesIO()
        Image.new("RGB", (640, 480), (20, 90, 140)).save(output, format=fmt)
        return output.getvalue()

    def test_normalization_and_thumbnail(self):
        for fmt, mime in [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")]:
            with self.subTest(fmt=fmt):
                actual, original, thumb = media._encode(self.image_bytes(fmt), mime, 5_000_000)
                self.assertEqual(actual, mime)
                self.assertEqual(Image.open(io.BytesIO(original)).size, (640, 480))
                self.assertLessEqual(max(Image.open(io.BytesIO(thumb)).size), 480)
                self.assertIsNone(Image.open(io.BytesIO(original)).getexif().get(34853))

    def test_rejects_mismatch_garbage_and_oversize(self):
        with self.assertRaises(ValueError):
            media._encode(self.image_bytes(), "image/png", 5_000_000)
        with self.assertRaises(ValueError):
            media._encode(b"#!/bin/sh\n", "image/jpeg", 5_000_000)
        with self.assertRaises(ValueError):
            media._encode(self.image_bytes(), "image/jpeg", 10)

    def test_private_root_rejects_temp_and_missing_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(service.ConfigurationError):
                media.storage_root()
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"REMIHUB_ADVENTURES_MEDIA_ROOT": tmp}):
            with self.assertRaises(service.ConfigurationError):
                media.storage_root()

    def test_unwritable_media_child_is_configuration_failure(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / "originals"
            with patch.object(media.os, "access", side_effect=lambda path, mode: path != child):
                with self.assertRaises(service.ConfigurationError):
                    media._ensure_subdirectory(root, "originals")

    def test_atomic_storage_and_no_path_in_metadata(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "image.jpg"
            media._atomic_file(path, b"test")
            self.assertEqual(path.read_bytes(), b"test")
            row = {"id": TRIP, "trip_id": TRIP, "storage_key": TRIP, "cover_photo_id": TRIP,
                   "original_filename": "same.jpg", "content_type": "image/jpeg", "byte_size": 4,
                   "caption": None, "favorite": False, "display_order": 0, "uploaded_at": NOW, "updated_at": NOW}
            shown = media._present(row)
            self.assertNotIn("storage_key", shown)
            self.assertTrue(shown["is_cover"])
            self.assertTrue(shown["thumbnail_url"].startswith("/adventures/trips/"))

    def test_directory_publication_syncs_directory_fd(self):
        with TemporaryDirectory() as tmp, patch.object(media.os, "fsync") as sync:
            media._fsync_directory(Path(tmp))
            sync.assert_called_once()

    def test_cleanup_queue_removes_files_and_record(self):
        class Cursor:
            def __init__(self): self.deleted = False
            def execute(self, query, args=None):
                self.query = query
                if query.startswith("DELETE FROM public.adventures_photo_cleanup"):
                    self.deleted = True
            def fetchall(self): return [{"storage_key": TRIP}]
            def fetchone(self):
                return {"acquired": True} if "pg_try_advisory" in self.query else None
        cur = Cursor()
        @contextmanager
        def tx(): yield cur
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "originals").mkdir(); (root / "thumbnails").mkdir()
            original, thumb = media._paths(root, TRIP, "image/jpeg")
            original.write_bytes(b"original"); thumb.write_bytes(b"thumb")
            with patch.object(media, "storage_root", return_value=root), patch.object(media.db, "transaction", tx):
                media.reconcile_cleanup()
            self.assertFalse(original.exists()); self.assertFalse(thumb.exists())
            self.assertTrue(cur.deleted)


class AdventureMediaAsyncTests(unittest.IsolatedAsyncioTestCase):
    class Connection:
        def __init__(self): self.queries = []; self.commits = 0; self.rollbacks = 0
        def cursor(self, **_kwargs):
            conn = self
            class Cursor:
                def __enter__(self): return self
                def __exit__(self, *_args): pass
                def execute(self, query, args=None): conn.queries.append((query, args))
                def fetchone(self): return {"next": 0}
            return Cursor()
        def commit(self): self.commits += 1
        def rollback(self): self.rollbacks += 1

    async def test_duplicate_filenames_use_distinct_private_storage(self):
        connections = []
        synced = []
        def connection():
            result = self.Connection(); connections.append(result); return result
        @contextmanager
        def tx(): yield None
        image = io.BytesIO()
        Image.new("RGB", (12, 12)).save(image, format="JPEG")
        class Upload:
            filename = "same.jpg"
            content_type = "image/jpeg"
            async def read(self, size): return image.getvalue()[:size]
        def insert(_cur, _table, values):
            self.assertEqual(synced[-2:], ["originals", "thumbnails"])
            return {"id": UUID(values["storage_key"]), "trip_id": TRIP,
                    "original_filename": values["original_filename"], "storage_key": values["storage_key"],
                    "content_type": values["content_type"], "byte_size": values["byte_size"],
                    "caption": None, "favorite": False, "display_order": 0,
                    "uploaded_at": NOW, "updated_at": NOW}
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "originals").mkdir(); (root / "thumbnails").mkdir()
            with patch.object(media, "storage_root", return_value=root), \
                 patch.object(media.db, "transaction", tx), \
                 patch.object(media.db, "get_db_conn", side_effect=connection), \
                 patch.object(media.db, "put_db_conn"), \
                 patch.object(media.db, "assert_trip", return_value=None), \
                 patch.object(media, "_fsync_directory", side_effect=lambda path: synced.append(path.name)), \
                 patch.object(media.db, "_insert", side_effect=insert):
                first = await media.upload(USER.id, str(TRIP), Upload())
                second = await media.upload(USER.id, str(TRIP), Upload())
            self.assertEqual(first["original_filename"], second["original_filename"])
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(len(list((root / "originals").iterdir())), 2)
            self.assertEqual(len(list((root / "thumbnails").iterdir())), 2)
            for conn in connections:
                intent = next(i for i, (query, _) in enumerate(conn.queries) if "INSERT INTO public.adventures_photo_cleanup" in query)
                self.assertGreaterEqual(conn.commits, 2)
                self.assertGreaterEqual(intent, 0)

    async def test_broken_upload_retains_cleanup_intent_for_published_original(self):
        @contextmanager
        def tx(): yield None
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "originals").mkdir(); (root / "thumbnails").mkdir()
            image = io.BytesIO()
            Image.new("RGB", (12, 12)).save(image, format="JPEG")
            class Upload:
                filename = "../../same.jpg"
                content_type = "image/jpeg"
                async def read(self, size): return image.getvalue()[:size]
            upload = Upload()
            conn = self.Connection()
            original_atomic = media._atomic_file
            calls = 0
            def broken(path, content):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated thumbnail disk failure")
                original_atomic(path, content)
            with patch.object(media, "storage_root", return_value=root), \
                 patch.object(media.db, "transaction", tx), \
                 patch.object(media.db, "get_db_conn", return_value=conn), \
                 patch.object(media.db, "put_db_conn"), \
                 patch.object(media.db, "assert_trip", return_value=None), \
                 patch.object(media, "_atomic_file", side_effect=broken):
                with self.assertRaises(service.ConfigurationError):
                    await media.upload(USER.id, str(TRIP), upload)
            self.assertEqual(len(list((root / "originals").iterdir())), 1)
            self.assertEqual(list((root / "thumbnails").iterdir()), [])
            self.assertTrue(any("INSERT INTO public.adventures_photo_cleanup" in q for q, _ in conn.queries))
            self.assertGreaterEqual(conn.commits, 1)

    async def test_crash_after_publication_leaves_durable_cleanup_intent(self):
        @contextmanager
        def tx(): yield None
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "originals").mkdir(); (root / "thumbnails").mkdir()
            image = io.BytesIO()
            Image.new("RGB", (12, 12)).save(image, format="JPEG")
            class Upload:
                filename = "crash.jpg"
                content_type = "image/jpeg"
                async def read(self, size): return image.getvalue()[:size]
            conn = self.Connection()
            atomic = media._atomic_file
            calls = 0
            def crash(path, content):
                nonlocal calls
                calls += 1
                self.assertGreaterEqual(conn.commits, 1)
                self.assertTrue(any("INSERT INTO public.adventures_photo_cleanup" in q for q, _ in conn.queries))
                if calls == 2:
                    raise SystemExit("simulated process exit")
                atomic(path, content)
            with patch.object(media, "storage_root", return_value=root), \
                 patch.object(media.db, "transaction", tx), \
                 patch.object(media.db, "get_db_conn", return_value=conn), \
                 patch.object(media.db, "put_db_conn"), \
                 patch.object(media.db, "assert_trip", return_value=None), \
                 patch.object(media, "_atomic_file", side_effect=crash):
                with self.assertRaises(SystemExit):
                    await media.upload(USER.id, str(TRIP), Upload())
            self.assertEqual(len(list((root / "originals").iterdir())), 1)
            self.assertEqual(len(list((root / "thumbnails").iterdir())), 0)

    async def test_directory_sync_failure_keeps_intent_and_does_not_commit_photo(self):
        @contextmanager
        def tx(): yield None
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "originals").mkdir(); (root / "thumbnails").mkdir()
            image = io.BytesIO()
            Image.new("RGB", (12, 12)).save(image, format="JPEG")
            class Upload:
                filename = "sync.jpg"
                content_type = "image/jpeg"
                async def read(self, size): return image.getvalue()[:size]
            conn = self.Connection()
            with patch.object(media, "storage_root", return_value=root), \
                 patch.object(media.db, "transaction", tx), \
                 patch.object(media.db, "get_db_conn", return_value=conn), \
                 patch.object(media.db, "put_db_conn"), \
                 patch.object(media.db, "assert_trip", return_value=None), \
                 patch.object(media.db, "_insert") as insert, \
                 patch.object(media, "_fsync_directory", side_effect=OSError("disk sync failure")):
                with self.assertRaises(service.ConfigurationError):
                    await media.upload(USER.id, str(TRIP), Upload())
            insert.assert_not_called()
            self.assertEqual(len(list((root / "originals").iterdir())), 1)
            self.assertEqual(len(list((root / "thumbnails").iterdir())), 1)
            self.assertTrue(any("INSERT INTO public.adventures_photo_cleanup" in q for q, _ in conn.queries))
            self.assertFalse(any("DELETE FROM public.adventures_photo_cleanup" in q for q, _ in conn.queries))


class AdventureContractTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(adventures.router)

    def test_every_route_requires_strict_principal(self):
        for route in adventures.router.routes:
            calls = {d.call for d in route.dependant.dependencies}
            self.assertIn(require_current_principal, calls, route.path)

    def test_generated_openapi_matches_registered_routes(self):
        schema = self.app.openapi()
        actual = {(method.lower(), route.path) for route in adventures.router.routes for method in route.methods}
        documented = {(method, path) for path, item in schema["paths"].items() for method in item}
        self.assertEqual(actual, documented)
        upload = schema["paths"]["/adventures/trips/{trip_id}/photos"]["post"]
        self.assertIn("multipart/form-data", schema["components"]["requestBodies"][upload["requestBody"]["$ref"].split("/")[-1]]["content"] if "$ref" in upload["requestBody"] else upload["requestBody"]["content"])
        self.assertIn("201", upload["responses"])
        self.assertIn("413", upload["responses"])
        self.assertNotIn("413", schema["paths"]["/adventures/trips"]["post"]["responses"])

    def test_response_model_filters_internal_columns(self):
        response = models.Envelope[models.TripOut](data=service.present_trip(trip_row(), date(2026, 11, 16)))
        self.assertNotIn("user_id", response.model_dump()["data"])

    def test_auth_denies_anonymous_and_owner_passed_from_principal(self):
        with self.assertRaises(Exception) as caught:
            require_current_principal(None)
        self.assertEqual(caught.exception.status_code, 401)
        with patch.object(service, "get_trip", return_value=service.present_trip(trip_row(), date(2026, 11, 16))) as get:
            self.assertTrue(adventures.get_trip(TRIP, principal=USER)["success"])
            get.assert_called_once_with(USER.id, str(TRIP))


class AdventureRequestValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.include_router(adventures.router)
        async def principal(): return USER
        self.app.dependency_overrides[require_current_principal] = principal
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_numeric_epoch_dates_rejected_by_api_before_database_access(self):
        requests = [
            ("/adventures/trips", {"title": "Epoch", "arrival_date": 1795132800, "departure_date": "2026-11-23"}),
            (f"/adventures/trips/{TRIP}/activities", {"title": "Epoch", "activity_date": 1795132800}),
            (f"/adventures/trips/{TRIP}/memories", {"title": "Epoch", "memory_date": 1795132800}),
        ]
        for path, body in requests:
            with self.subTest(path=path):
                result = await self.client.post(path, json=body)
                self.assertEqual(result.status_code, 422, result.text)

    async def test_multipart_content_length_rejected_before_spooling(self):
        with patch.dict(os.environ, {"REMIHUB_ADVENTURES_MAX_PHOTO_BYTES": "1024"}):
            result = await self.client.post(f"/adventures/trips/{TRIP}/photos",
                files={"file": ("huge.jpg", b"X" * 200_000, "image/jpeg")})
        self.assertEqual(result.status_code, 413, result.text)

    async def test_bounded_multipart_replays_valid_body_to_upload(self):
        data = b"valid multipart payload"
        async def upload(user_id, trip_id, file, caption):
            self.assertEqual((user_id, trip_id, caption), (USER.id, str(TRIP), "sample"))
            self.assertEqual(await file.read(), data)
            return media._present({"id": TRIP, "trip_id": TRIP, "storage_key": TRIP,
                "cover_photo_id": None, "original_filename": "photo.jpg", "content_type": "image/jpeg",
                "byte_size": len(data), "caption": caption, "favorite": False, "display_order": 0,
                "uploaded_at": NOW, "updated_at": NOW})
        with patch.dict(os.environ, {"REMIHUB_ADVENTURES_MAX_PHOTO_BYTES": "1024"}), \
             patch.object(media, "upload", side_effect=upload):
            result = await self.client.post(f"/adventures/trips/{TRIP}/photos",
                data={"caption": "sample"}, files={"file": ("photo.jpg", data, "image/jpeg")})
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["data"]["caption"], "sample")

    async def test_chunked_multipart_excess_rejected_during_ingestion(self):
        async def chunks():
            yield b"--test\r\nContent-Disposition: form-data; name=\"file\"; filename=\"large.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"
            for _ in range(100):
                yield b"X" * 1024
            yield b"\r\n--test--\r\n"
        with patch.dict(os.environ, {"REMIHUB_ADVENTURES_MAX_PHOTO_BYTES": "1024"}):
            result = await self.client.post(f"/adventures/trips/{TRIP}/photos",
                content=chunks(), headers={"content-type": "multipart/form-data; boundary=test", "content-length": "1"})
        self.assertEqual(result.status_code, 413, result.text)


if __name__ == "__main__":
    unittest.main()

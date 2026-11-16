"""Opt-in HTTP/SQL acceptance checks against an operator-provisioned QA schema.

These tests never apply migrations. Set both REMIHUB_ADVENTURES_QA_TESTS=1 and
REMIHUB_ADVENTURES_QA_DATABASE_URL to a disposable QA database with migration
0023 already applied. No production database or implicit fallback is used.
"""
from __future__ import annotations

import io
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4


@unittest.skipUnless(
    os.environ.get("REMIHUB_ADVENTURES_QA_TESTS") == "1"
    and os.environ.get("REMIHUB_ADVENTURES_QA_DATABASE_URL"),
    "Operator-provisioned Adventures QA database is not configured",
)
class AdventuresDatabaseHttpTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        from psycopg2 import pool

        # Importing the service must not connect through the application pool.
        class OfflinePool:
            def __init__(self, *_args, **_kwargs): pass
            def getconn(self): raise RuntimeError("QA test must use its explicit DSN")
            def putconn(self, _conn): pass

        cls.pool_patch = patch.object(pool, "ThreadedConnectionPool", OfflinePool)
        cls.pool_patch.start()
        from fastapi import FastAPI
        from backend.core.auth import AuthenticatedPrincipal, require_current_principal
        from backend.routers import adventures
        from backend.services import adventures_media, adventures_service

        cls.psycopg2 = psycopg2
        cls.media = adventures_media
        cls.service = adventures_service
        cls.dsn = os.environ["REMIHUB_ADVENTURES_QA_DATABASE_URL"]
        cls.users = []
        with psycopg2.connect(cls.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.adventures_photo_cleanup')")
                if cur.fetchone()[0] is None:
                    raise RuntimeError("QA database must have Adventures migration 0023 applied")
                for _ in range(2):
                    identity = uuid4().hex
                    cur.execute("""INSERT INTO public.remihub_users(firebase_uid,email,display_name)
                        VALUES (%s,%s,'Adventures QA') RETURNING id""",
                        (f"adventures-qa-{identity}", f"adventures-qa-{identity}@example.invalid"))
                    cls.users.append(cur.fetchone()[0])

        cls.owner = AuthenticatedPrincipal(id=str(cls.users[0]), firebase_uid="qa-owner",
            email="qa-owner@example.invalid", display_name="QA Owner", role="member")
        cls.other = AuthenticatedPrincipal(id=str(cls.users[1]), firebase_uid="qa-other",
            email="qa-other@example.invalid", display_name="QA Other", role="member")
        cls.current = cls.owner
        cls.app = FastAPI()
        cls.app.include_router(adventures.router)
        cls.app.dependency_overrides[require_current_principal] = lambda: cls.current
        cls.db_patches = [
            patch.object(adventures_service, "get_db_conn", side_effect=lambda: psycopg2.connect(cls.dsn)),
            patch.object(adventures_service, "put_db_conn", side_effect=lambda conn: conn.close()),
            patch.object(adventures_service, "today", return_value=__import__("datetime").date(2026, 11, 16)),
        ]
        for item in cls.db_patches: item.start()
        cls.temp = TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        (cls.root / "originals").mkdir()
        (cls.root / "thumbnails").mkdir()
        cls.media_patch = patch.object(adventures_media, "storage_root", return_value=cls.root)
        cls.media_patch.start()

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "users"):
            cls.pool_patch.stop()
            return
        try:
            with cls.psycopg2.connect(cls.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM public.remihub_users WHERE id = ANY(%s)", (cls.users,))
        finally:
            cls.media_patch.stop()
            cls.temp.cleanup()
            for item in reversed(cls.db_patches): item.stop()
            cls.pool_patch.stop()

    async def asyncSetUp(self):
        import httpx
        type(self).current = self.owner
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://qa")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def request(self, method, path, **kwargs):
        return await self.client.request(method, path, **kwargs)

    async def test_persisted_trip_campground_owner_and_constraints(self):
        campground = await self.request("POST", "/adventures/campgrounds", json={"name": "Shared Site"})
        self.assertEqual(campground.status_code, 201, campground.text)
        campground_id = campground.json()["data"]["id"]
        payload = {"title": "Fri to Mon", "campground_id": campground_id,
            "arrival_date": "2026-11-20", "departure_date": "2026-11-23",
            "intent": "Booked", "campsite_number": "A4", "electricity": "30A"}
        trip = await self.request("POST", "/adventures/trips", json=payload)
        self.assertEqual(trip.status_code, 201, trip.text)
        trip_id = trip.json()["data"]["id"]
        self.assertEqual(trip.json()["data"]["nights"], 3)
        self.assertEqual(trip.json()["data"]["calendar_days"], 4)
        got = await self.request("GET", f"/adventures/trips/{trip_id}")
        self.assertEqual(got.json()["data"]["campsite_number"], "A4")
        self.assertEqual((await self.request("DELETE", f"/adventures/campgrounds/{campground_id}")).status_code, 409)
        type(self).current = self.other
        self.assertEqual((await self.request("GET", f"/adventures/trips/{trip_id}")).status_code, 404)
        self.assertEqual((await self.request("PATCH", f"/adventures/trips/{trip_id}", json={"title": "stolen"})).status_code, 404)
        self.assertEqual((await self.request("GET", f"/adventures/campgrounds/{campground_id}")).status_code, 404)
        type(self).current = self.owner
        with self.psycopg2.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                with self.assertRaises(self.psycopg2.IntegrityError):
                    cur.execute("""INSERT INTO public.adventures_trips(user_id,title,arrival_date,departure_date)
                        VALUES (%s,'Bad dates','2026-11-23','2026-11-20')""", (self.users[0],))
                conn.rollback()
                with self.assertRaises(self.psycopg2.IntegrityError):
                    cur.execute("""INSERT INTO public.adventures_trips(user_id,title,campground_id)
                        VALUES (%s,'Foreign campground',%s)""", (self.users[1], campground_id))
                conn.rollback()
        self.assertEqual((await self.request("DELETE", f"/adventures/trips/{trip_id}")).status_code, 204)
        self.assertEqual((await self.request("DELETE", f"/adventures/campgrounds/{campground_id}")).status_code, 204)

    async def test_authenticated_multipart_gallery_and_failure_intent(self):
        from PIL import Image
        trip = await self.request("POST", "/adventures/trips", json={"title": "Album"})
        self.assertEqual(trip.status_code, 201, trip.text)
        trip_id = trip.json()["data"]["id"]
        content = io.BytesIO()
        Image.new("RGB", (24, 24), (5, 20, 40)).save(content, format="JPEG")
        photo = await self.request("POST", f"/adventures/trips/{trip_id}/photos",
            files={"file": ("../../photo.jpg", content.getvalue(), "image/jpeg")})
        self.assertEqual(photo.status_code, 201, photo.text)
        photo_id = photo.json()["data"]["id"]
        self.assertEqual(photo.json()["data"]["original_filename"], "photo.jpg")
        for suffix in ("thumbnail", "original", "download"):
            response = await self.request("GET", f"/adventures/trips/{trip_id}/photos/{photo_id}/{suffix}")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.content)
            self.assertEqual(response.headers["cache-control"], "private, no-store")
        type(self).current = self.other
        self.assertEqual((await self.request("GET", f"/adventures/trips/{trip_id}/photos/{photo_id}/thumbnail")).status_code, 404)
        type(self).current = self.owner
        mismatch = await self.request("POST", f"/adventures/trips/{trip_id}/photos",
            files={"file": ("bad.png", content.getvalue(), "image/png")})
        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual((await self.request("PUT", f"/adventures/trips/{trip_id}/cover/{photo_id}")).status_code, 200)
        self.assertEqual((await self.request("DELETE", f"/adventures/trips/{trip_id}/photos/{photo_id}")).status_code, 204)
        self.assertIsNone((await self.request("GET", f"/adventures/trips/{trip_id}")).json()["data"]["cover_photo_id"])

        original_atomic = self.media._atomic_file
        calls = 0
        def fail_thumbnail(path, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated media failure")
            return original_atomic(path, data)
        with patch.object(self.media, "_atomic_file", side_effect=fail_thumbnail):
            broken = await self.request("POST", f"/adventures/trips/{trip_id}/photos",
                files={"file": ("broken.jpg", content.getvalue(), "image/jpeg")})
        self.assertEqual(broken.status_code, 503)
        with self.psycopg2.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.adventures_photo_cleanup")
                self.assertGreaterEqual(cur.fetchone()[0], 1)
        self.media.reconcile_cleanup()
        with self.psycopg2.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.adventures_photo_cleanup")
                self.assertEqual(cur.fetchone()[0], 0)
        self.assertEqual(list((self.root / "originals").iterdir()), [])

    async def test_persisted_itinerary_checklist_journal_and_rating(self):
        trip = await self.request("POST", "/adventures/trips", json={"title": "History",
            "arrival_date": "2024-05-10", "departure_date": "2024-05-13"})
        self.assertEqual(trip.status_code, 201, trip.text)
        trip_id = trip.json()["data"]["id"]
        activity_path = f"/adventures/trips/{trip_id}/activities"
        activity = await self.request("POST", activity_path, json={"title": "Campfire", "display_order": 2})
        self.assertEqual(activity.status_code, 201, activity.text)
        activity_id = activity.json()["data"]["id"]
        move = await self.request("PATCH", f"{activity_path}/{activity_id}",
            json={"activity_date": "2024-05-11", "display_order": 1, "completed": True})
        self.assertEqual(move.status_code, 200, move.text)
        self.assertEqual((await self.request("GET", activity_path)).json()["data"]["items"][0]["activity_date"], "2024-05-11")
        item = await self.request("POST", "/adventures/checklist-template/items",
            json={"text": "Tent", "category": "Before Departure"})
        self.assertEqual(item.status_code, 201, item.text)
        checklist_path = f"/adventures/trips/{trip_id}/checklist"
        first = await self.request("POST", checklist_path + "/instantiate")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["data"]["total_count"], 1)
        trip_item_id = first.json()["data"]["items"][0]["id"]
        checked = await self.request("PATCH", checklist_path + f"/items/{trip_item_id}", json={"completed": True})
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual((await self.request("POST", checklist_path + "/instantiate")).json()["data"]["percent"], 100)
        await self.request("PATCH", f"/adventures/checklist-template/items/{item.json()['data']['id']}",
            json={"text": "New tent"})
        self.assertEqual((await self.request("GET", checklist_path)).json()["data"]["items"][0]["text"], "Tent")
        self.assertEqual((await self.request("POST", checklist_path + "/reset")).json()["data"]["percent"], 0)
        journal = await self.request("PUT", f"/adventures/trips/{trip_id}/journal",
            json={"overall_summary": "Great", "favorite_part": "Fire"})
        self.assertEqual(journal.status_code, 200, journal.text)
        memory = await self.request("POST", f"/adventures/trips/{trip_id}/memories", json={"title": "Sunset"})
        self.assertEqual(memory.status_code, 201, memory.text)
        rating = await self.request("PUT", f"/adventures/trips/{trip_id}/rating",
            json={"overall": 5, "would_return": "yes"})
        self.assertEqual(rating.status_code, 200, rating.text)
        self.assertIsNone(rating.json()["data"]["facilities"])
        self.assertEqual((await self.request("GET", f"/adventures/trips/{trip_id}/rating")).json()["data"]["overall"], 5)
        with self.psycopg2.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                with self.assertRaises(self.psycopg2.IntegrityError):
                    cur.execute("UPDATE public.adventures_ratings SET overall=6 WHERE trip_id=%s", (trip_id,))
                conn.rollback()

    async def test_auth_contract_without_override(self):
        from fastapi import FastAPI
        from backend.routers import adventures
        import httpx
        app = FastAPI()
        app.include_router(adventures.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qa") as client:
            response = await client.get("/adventures/trips")
        self.assertEqual(response.status_code, 401)

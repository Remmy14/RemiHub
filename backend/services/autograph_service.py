"""Autograph collection persistence and private media operations."""
from __future__ import annotations

import io
import os
import re
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from psycopg2 import IntegrityError
from psycopg2.extras import RealDictCursor

from backend.database.database import get_db_conn, put_db_conn
from backend.models import autographs as m

AUTOGRAPH_IMAGE_DIR = Path("/opt/remihub/data/autographs")
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg": ("JPEG", ".jpg"), "image/png": ("PNG", ".png"), "image/webp": ("WEBP", ".webp")}
FILENAME = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(?:jpg|png|webp)\Z")
IMAGE_URL = re.compile(r"/autographs/image/([0-9a-f-]+\.(?:jpg|png|webp))\Z")
VIEWS = ("front", "right", "back", "left")


@contextmanager
def transaction():
    conn = get_db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _one(cur, sql, args=(), missing="Resource not found"):
    cur.execute(sql, args)
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, missing)
    return dict(row)


def _normalize(name: str) -> tuple[str, str]:
    display = " ".join(name.split())
    if not display or len(display) > 200:
        raise HTTPException(422, "Driver name must contain 1 to 200 characters")
    # PostgreSQL lower() and regexp_replace use database collation; this key
    # covers case and whitespace normalization without fuzzy identity merges.
    return display, display.lower()


def _image_file(image_path: str) -> Path:
    match = IMAGE_URL.fullmatch(image_path)
    if not match or not FILENAME.fullmatch(match.group(1)):
        raise HTTPException(422, "Invalid autograph image reference")
    path = AUTOGRAPH_IMAGE_DIR / match.group(1)
    if AUTOGRAPH_IMAGE_DIR.is_symlink() or path.is_symlink() or not path.is_file():
        raise HTTPException(422, "Autograph image does not exist")
    return path


def _image_path(value: str | None) -> str:
    value = value or ""
    if value:
        _image_file(value)
    return value


@contextmanager
def _media_lock(path: str):
    """Hold one pooled connection and a session lock through commit and cleanup."""
    conn = get_db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if path:
                cur.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (path,))
            conn.commit()
            try:
                yield cur, conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                if path:
                    # Any failed statement must be cleared before unlocking.
                    conn.rollback()
                    cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (path,))
                    conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _file_for_name(filename: str) -> Path:
    if not FILENAME.fullmatch(filename):
        raise HTTPException(404, "Image not found")
    path = AUTOGRAPH_IMAGE_DIR / filename
    if AUTOGRAPH_IMAGE_DIR.is_symlink() or path.is_symlink() or not path.is_file():
        raise HTTPException(404, "Image not found")
    return path


async def _save_image(file: UploadFile) -> tuple[str, int]:
    expected = ALLOWED_IMAGE_TYPES.get(file.content_type)
    if expected is None:
        raise HTTPException(400, "Only JPEG, PNG, and WEBP images are allowed.")
    raw = await file.read(MAX_IMAGE_SIZE_BYTES + 1)
    if not raw or len(raw) > MAX_IMAGE_SIZE_BYTES:
        raise HTTPException(400, "Image is empty or exceeds the 5 MB limit")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != expected[0] or image.width * image.height > 40_000_000 or getattr(image, "n_frames", 1) != 1:
                raise HTTPException(400, "Invalid image content")
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(400, "Invalid image content") from exc
    if AUTOGRAPH_IMAGE_DIR.is_symlink() or not AUTOGRAPH_IMAGE_DIR.is_dir():
        raise HTTPException(503, "Autograph media directory is unavailable")
    name = f"{uuid4()}{expected[1]}"
    path = AUTOGRAPH_IMAGE_DIR / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        raise HTTPException(503, "Autograph media directory is not writable") from exc
    return f"/autographs/image/{name}", len(raw)


async def upload_autograph_image(file: UploadFile):
    path, _ = await _save_image(file)
    return {"success": True, "image_path": path, "filename": path.rsplit("/", 1)[1]}


def get_autograph_image(filename: str):
    return FileResponse(_file_for_name(filename), headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


def _driver(cur, driver_id: UUID | None, driver_name: str | None):
    if driver_id is not None:
        return _one(cur, "SELECT * FROM public.autograph_drivers WHERE id=%s FOR KEY SHARE", (str(driver_id),), "Driver not found")
    display, key = _normalize(driver_name or "")
    cur.execute("""INSERT INTO public.autograph_drivers(display_name,normalized_name) VALUES (%s,%s)
        ON CONFLICT (normalized_name) DO NOTHING RETURNING *""", (display, key))
    row = cur.fetchone()
    return dict(row) if row else _one(cur, "SELECT * FROM public.autograph_drivers WHERE normalized_name=%s FOR KEY SHARE", (key,))


ENTRY_SELECT = """SELECT a.id,d.display_name AS driver_name,a.image_path,a.helmet_view,
 a.x_percent,a.y_percent,a.region,a.notes,a.created_at,a.helmet_id,a.driver_id,a.date_signed
 FROM public.autograph_entries a JOIN public.autograph_drivers d ON d.id=a.driver_id"""


def _entry(cur, autograph_id):
    return _one(cur, ENTRY_SELECT + " WHERE a.id=%s", (autograph_id,), "Autograph not found")


def create_autograph(helmet_id: UUID, entry: m.ScopedAutographCreate):
    image_path = _image_path(entry.image_path)
    with _media_lock(image_path) as (cur, _conn):
        if image_path:
            _image_file(image_path)
        # Serializes coordinate assignment with helmet-view replacement.
        _one(cur, "SELECT id FROM public.autograph_helmets WHERE id=%s FOR UPDATE", (str(helmet_id),), "Helmet not found")
        driver = _driver(cur, entry.driver_id, entry.driver_name)
        cur.execute("""INSERT INTO public.autograph_entries
            (driver_name,image_path,helmet_view,x_percent,y_percent,region,notes,helmet_id,driver_id,date_signed)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (driver["display_name"], image_path, entry.helmet_view, entry.x_percent, entry.y_percent,
             entry.region, entry.notes, str(helmet_id), str(driver["id"]), entry.date_signed))
        autograph_id = cur.fetchone()["id"]
        result = _entry(cur, autograph_id)
    return result


def add_autograph(entry: m.AutographCreate):
    with transaction() as cur:
        helmet = _one(cur, "SELECT id FROM public.autograph_helmets WHERE year=2026", missing="Original helmet not ready")
    result = create_autograph(helmet["id"], m.ScopedAutographCreate(**entry.model_dump(exclude={"driver_name"}), driver_name=entry.driver_name))
    return m.AutographEntry(**result)


def get_all_autographs():
    with transaction() as cur:
        cur.execute(ENTRY_SELECT + " JOIN public.autograph_helmets h ON h.id=a.helmet_id WHERE h.year=2026 ORDER BY a.created_at DESC NULLS LAST,a.id DESC")
        return [m.AutographEntry(**row) for row in cur.fetchall()]


def list_helmets():
    with transaction() as cur:
        cur.execute("SELECT * FROM public.autograph_helmets ORDER BY year DESC,id")
        return cur.fetchall()


def create_helmet(value: m.HelmetCreate):
    try:
        with transaction() as cur:
            return _one(cur, """INSERT INTO public.autograph_helmets(year,display_name,description)
                VALUES (%s,%s,%s) RETURNING *""", (value.year, value.display_name.strip(), value.description))
    except IntegrityError as exc:
        raise HTTPException(409, "Helmet year already exists") from exc


def get_helmet(helmet_id: UUID):
    with transaction() as cur:
        return _one(cur, "SELECT * FROM public.autograph_helmets WHERE id=%s", (str(helmet_id),), "Helmet not found")


def list_helmet_autographs(helmet_id: UUID):
    with transaction() as cur:
        _one(cur, "SELECT id FROM public.autograph_helmets WHERE id=%s", (str(helmet_id),), "Helmet not found")
        cur.execute(ENTRY_SELECT + " WHERE a.helmet_id=%s ORDER BY a.created_at DESC NULLS LAST,a.id DESC", (str(helmet_id),))
        return cur.fetchall()


def helmet_stats(helmet_id: UUID):
    with transaction() as cur:
        _one(cur, "SELECT id FROM public.autograph_helmets WHERE id=%s", (str(helmet_id),), "Helmet not found")
        return _one(cur, """SELECT count(*)::integer AS total_signatures,
            count(DISTINCT driver_id)::integer AS unique_signers FROM public.autograph_entries WHERE helmet_id=%s""", (str(helmet_id),))


def collection_stats():
    with transaction() as cur:
        return _one(cur, """SELECT (SELECT count(*)::integer FROM public.autograph_helmets) AS total_helmets,
            count(*)::integer AS total_signatures,count(DISTINCT driver_id)::integer AS unique_signers
            FROM public.autograph_entries""")


def list_drivers(search: str | None = None):
    with transaction() as cur:
        if search:
            cur.execute("SELECT * FROM public.autograph_drivers WHERE position(%s in normalized_name)>0 ORDER BY lower(display_name),id", (_normalize(search)[1],))
        else:
            cur.execute("SELECT * FROM public.autograph_drivers ORDER BY lower(display_name),id")
        return cur.fetchall()


def get_driver(driver_id: UUID):
    with transaction() as cur:
        return _one(cur, "SELECT * FROM public.autograph_drivers WHERE id=%s", (str(driver_id),), "Driver not found")


def rename_driver(driver_id: UUID, value: m.DriverCreate):
    display, key = _normalize(value.display_name)
    try:
        with transaction() as cur:
            return _one(cur, """UPDATE public.autograph_drivers SET display_name=%s,normalized_name=%s
                WHERE id=%s RETURNING *""", (display, key, str(driver_id)), "Driver not found")
    except IntegrityError as exc:
        raise HTTPException(409, "Equivalent driver already exists; use merge") from exc


def merge_drivers(target_id: UUID, source_id: UUID):
    if target_id == source_id:
        raise HTTPException(422, "Drivers must be different")
    with transaction() as cur:
        cur.execute("SELECT * FROM public.autograph_drivers WHERE id IN (%s,%s) ORDER BY id FOR UPDATE", (str(target_id), str(source_id)))
        rows = {str(row["id"]): dict(row) for row in cur.fetchall()}
        if str(target_id) not in rows or str(source_id) not in rows:
            raise HTTPException(404, "Driver not found")
        cur.execute("UPDATE public.autograph_entries SET driver_id=%s WHERE driver_id=%s", (str(target_id), str(source_id)))
        cur.execute("DELETE FROM public.autograph_drivers WHERE id=%s", (str(source_id),))
        return rows[str(target_id)]


def driver_history(driver_id: UUID):
    with transaction() as cur:
        _one(cur, "SELECT id FROM public.autograph_drivers WHERE id=%s", (str(driver_id),), "Driver not found")
        cur.execute("""SELECT a.id,d.display_name AS driver_name,a.image_path,a.helmet_view,a.x_percent,a.y_percent,
            a.region,a.notes,a.created_at,a.helmet_id,a.driver_id,a.date_signed,
            h.year AS helmet_year,h.display_name AS helmet_name,
            NULLIF(a.image_path,'') AS image_url
            FROM public.autograph_entries a JOIN public.autograph_drivers d ON d.id=a.driver_id
            JOIN public.autograph_helmets h ON h.id=a.helmet_id
            WHERE a.driver_id=%s ORDER BY h.year DESC,a.created_at DESC NULLS LAST,a.id DESC""", (str(driver_id),))
        return cur.fetchall()


def _unreferenced(cur, path: str) -> bool:
    cur.execute("""SELECT (SELECT count(*) FROM public.autograph_entries WHERE image_path=%s)
        + (SELECT count(*) FROM public.autograph_helmet_images WHERE image_path=%s) AS references""", (path, path))
    return cur.fetchone()["references"] == 0


def _remove_if_unreferenced(cur, path: str):
    if not path or not IMAGE_URL.fullmatch(path) or not _unreferenced(cur, path):
        return
    file_path = AUTOGRAPH_IMAGE_DIR / path.rsplit("/", 1)[1]
    if not file_path.is_symlink() and file_path.is_file():
        file_path.unlink(missing_ok=True)


def delete_autograph(autograph_id: int):
    with transaction() as cur:
        image_path = _one(cur, "SELECT image_path FROM public.autograph_entries WHERE id=%s", (autograph_id,), "Autograph not found")["image_path"]
    with _media_lock(image_path) as (cur, conn):
        row = _one(cur, "SELECT image_path FROM public.autograph_entries WHERE id=%s FOR UPDATE", (autograph_id,), "Autograph not found")
        if row["image_path"] != image_path:
            raise HTTPException(409, "Autograph image changed concurrently")
        cur.execute("DELETE FROM public.autograph_entries WHERE id=%s", (autograph_id,))
        conn.commit()  # Never unlink before the deletion has committed.
        _remove_if_unreferenced(cur, image_path)
    return {"success": True, "deleted_id": autograph_id}


def list_helmet_images(helmet_id: UUID):
    with transaction() as cur:
        _one(cur, "SELECT id FROM public.autograph_helmets WHERE id=%s", (str(helmet_id),), "Helmet not found")
        cur.execute("SELECT * FROM public.autograph_helmet_images WHERE helmet_id=%s ORDER BY helmet_view", (str(helmet_id),))
        return [{**row, "image_url": row["image_path"]} for row in cur.fetchall()]


def get_helmet_image(helmet_id: UUID, view: str):
    if view not in VIEWS:
        raise HTTPException(404, "Helmet view not found")
    with transaction() as cur:
        row = _one(cur, "SELECT * FROM public.autograph_helmet_images WHERE helmet_id=%s AND helmet_view=%s", (str(helmet_id), view), "Helmet image not found")
        return {**row, "image_url": row["image_path"]}


async def upload_helmet_image(helmet_id: UUID, view: str, file: UploadFile):
    if view not in VIEWS:
        raise HTTPException(404, "Helmet view not found")
    with transaction() as cur:
        _one(cur, "SELECT id FROM public.autograph_helmets WHERE id=%s", (str(helmet_id),), "Helmet not found")
    path, size = await _save_image(file)
    old_path = None
    try:
        with transaction() as cur:
            _one(cur, "SELECT id FROM public.autograph_helmets WHERE id=%s FOR UPDATE", (str(helmet_id),), "Helmet not found")
            cur.execute("SELECT count(*) AS n FROM public.autograph_entries WHERE helmet_id=%s AND helmet_view=%s", (str(helmet_id), view))
            if cur.fetchone()["n"]:
                raise HTTPException(409, "View image cannot be replaced after signatures are placed")
            cur.execute("SELECT image_path FROM public.autograph_helmet_images WHERE helmet_id=%s AND helmet_view=%s", (str(helmet_id), view))
            old = cur.fetchone()
            old_path = old["image_path"] if old else None
            cur.execute("""INSERT INTO public.autograph_helmet_images(helmet_id,helmet_view,image_path,content_type,byte_size)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (helmet_id,helmet_view) DO UPDATE SET
                image_path=EXCLUDED.image_path,content_type=EXCLUDED.content_type,byte_size=EXCLUDED.byte_size,updated_at=now()
                RETURNING *""", (str(helmet_id), view, path, file.content_type, size))
            result = dict(cur.fetchone())
    except Exception:
        with _media_lock(path) as (cur, _conn):
            _remove_if_unreferenced(cur, path)
        raise
    if old_path:
        with _media_lock(old_path) as (cur, _conn):
            _remove_if_unreferenced(cur, old_path)
    return {**result, "image_url": result["image_path"]}

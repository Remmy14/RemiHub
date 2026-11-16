"""Private durable media for Adventures. Never mount these files as static assets."""
from __future__ import annotations

import io
import asyncio
import logging
import os
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

from backend.services import adventures_service as db

logger = logging.getLogger("remihub.adventures.media")
FORMATS = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}
MAX_PIXELS = 40_000_000


def max_bytes() -> int:
    value = os.environ.get("REMIHUB_ADVENTURES_MAX_PHOTO_BYTES", "15728640")
    try:
        size = int(value)
    except ValueError as exc:
        raise db.ConfigurationError("REMIHUB_ADVENTURES_MAX_PHOTO_BYTES must be an integer") from exc
    if not 1024 <= size <= 100 * 1024 * 1024:
        raise db.ConfigurationError("REMIHUB_ADVENTURES_MAX_PHOTO_BYTES is outside safe bounds")
    return size


def _ensure_subdirectory(root: Path, folder: str) -> None:
    target = root / folder
    try:
        target.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise db.ConfigurationError("Adventures media subdirectory is not writable") from exc
    if target.is_symlink() or not target.is_dir():
        raise db.ConfigurationError("Adventures media subdirectory is invalid")
    if not os.access(target, os.W_OK | os.X_OK):
        raise db.ConfigurationError("Adventures media subdirectory is not writable")


def storage_root() -> Path:
    value = os.environ.get("REMIHUB_ADVENTURES_MEDIA_ROOT")
    if not value:
        raise db.ConfigurationError("REMIHUB_ADVENTURES_MEDIA_ROOT is not configured")
    root = Path(value)
    try:
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise db.ConfigurationError("Adventures media root must be an existing absolute directory, not a symlink")
        root = root.resolve()
    except OSError as exc:
        raise db.ConfigurationError("Adventures media root is unavailable") from exc
    project = Path(__file__).resolve().parents[2]
    if any(root == temporary or temporary in root.parents for temporary in (Path("/tmp"), Path("/var/tmp"), Path("/run"))) or root == project or project in root.parents:
        raise db.ConfigurationError("Adventures media root must be outside temporary and release directories")
    if not os.access(root, os.W_OK | os.X_OK):
        raise db.ConfigurationError("Adventures media root is not writable")
    for folder in ("originals", "thumbnails"):
        _ensure_subdirectory(root, folder)
    return root


def _paths(root: Path, key, content_type: str):
    extension = next(ext for mime, ext in FORMATS.values() if mime == content_type)
    name = str(key) + extension
    return root / "originals" / name, root / "thumbnails" / (str(key) + ".jpg")


def _encode(data: bytes, claimed_type: str, limit: int):
    if not data or len(data) > limit:
        raise ValueError("Photo is empty or exceeds configured byte limit")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            if fmt not in FORMATS or claimed_type != FORMATS[fmt][0]:
                raise ValueError("Photo format and declared content type must match JPEG, PNG, or WebP")
            if getattr(probe, "n_frames", 1) != 1 or probe.width * probe.height > MAX_PIXELS:
                raise ValueError("Animated or oversized-dimension images are unsupported")
            probe.verify()
        with Image.open(io.BytesIO(data)) as decoded:
            image = ImageOps.exif_transpose(decoded)
            image.load()
            image = image.convert("RGB" if fmt == "JPEG" else "RGBA" if "A" in image.getbands() else "RGB")
            original = io.BytesIO()
            if fmt == "PNG":
                image.save(original, format=fmt)
            else:
                image.save(original, format=fmt, quality=90)
            original_bytes = original.getvalue()
            if len(original_bytes) > limit:
                raise ValueError("Normalized photo exceeds configured byte limit")
            image.thumbnail((480, 480), Image.Resampling.LANCZOS)
            thumb = io.BytesIO()
            image.convert("RGB").save(thumb, format="JPEG", quality=78, optimize=True)
            return FORMATS[fmt][0], original_bytes, thumb.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid or unsafe image content") from exc


def _atomic_file(path: Path, content: bytes):
    temporary = path.with_name("." + path.name + "." + str(uuid4()) + ".pending")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.link(temporary, path, follow_symlinks=False)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    """Persist a published or removed filename before its DB intent is cleared."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _present(row):
    row = dict(row)
    row.pop("storage_key", None)
    base = f"/adventures/trips/{row['trip_id']}/photos/{row['id']}"
    row["thumbnail_url"] = base + "/thumbnail"
    row["original_url"] = base + "/original"
    row["is_cover"] = row.pop("cover_photo_id", None) == row["id"]
    return row


def _photo(cur, user_id, trip_id, photo_id, lock=False):
    db.assert_trip(cur, user_id, trip_id)
    suffix = " FOR UPDATE OF p" if lock else ""
    return db._one(cur, """SELECT p.*,t.cover_photo_id FROM public.adventures_photos p
        JOIN public.adventures_trips t ON t.id=p.trip_id
        WHERE t.user_id=%s AND p.trip_id=%s AND p.id=%s""" + suffix,
                   (user_id, trip_id, photo_id), "Photo not found")


async def upload(user_id, trip_id, file: UploadFile, caption: str | None = None):
    with db.transaction() as cur:
        db.assert_trip(cur, user_id, trip_id)
    root = storage_root()
    limit = max_bytes()
    if file.content_type not in [x[0] for x in FORMATS.values()]:
        raise ValueError("Only JPEG, PNG, and WebP are accepted")
    raw = await file.read(limit + 1)
    mime, original, thumbnail = _encode(raw, file.content_type, limit)
    supplied_name = (file.filename or "photo").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in supplied_name if ch.isprintable() and ch not in "\r\n\x00")[:255] or "photo"
    key = uuid4()
    original_path, thumbnail_path = _paths(root, key, mime)
    # One connection holds a session advisory lock across the committed intent,
    # file publication, and final metadata transaction. A crashed process releases
    # the lock; the durable intent then lets reconciliation remove every file.
    conn = db.get_db_conn()
    locked = False
    try:
        with conn.cursor(cursor_factory=db.RealDictCursor) as cur:
            cur.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (str(key),))
            locked = True
            db.assert_trip(cur, user_id, trip_id)
            cur.execute("INSERT INTO public.adventures_photo_cleanup(storage_key) VALUES (%s)", (str(key),))
        conn.commit()  # Intent must survive before any file becomes visible.
        try:
            _atomic_file(original_path, original)
            _atomic_file(thumbnail_path, thumbnail)
            _fsync_directory(original_path.parent)
            _fsync_directory(thumbnail_path.parent)
        except OSError as exc:
            raise db.ConfigurationError("Adventures media storage is unavailable or unwritable") from exc
        with conn.cursor(cursor_factory=db.RealDictCursor) as cur:
            db.assert_trip(cur, user_id, trip_id, lock=True)
            cur.execute("SELECT coalesce(max(display_order),-1)+1 AS next FROM public.adventures_photos WHERE trip_id=%s", (trip_id,))
            order = cur.fetchone()["next"]
            row = db._insert(cur, "adventures_photos", {"trip_id": trip_id, "original_filename": name,
                "storage_key": str(key), "content_type": mime, "byte_size": len(original),
                "caption": caption, "display_order": order})
            cur.execute("DELETE FROM public.adventures_photo_cleanup WHERE storage_key=%s", (str(key),))
        conn.commit()  # Metadata and removal of the intent are atomic.
        return _present({**row, "cover_photo_id": None})
    except Exception:
        # Never remove published files here: a failed commit may actually have
        # succeeded on the server. Reconciliation checks the photo row first.
        conn.rollback()
        raise
    finally:
        if locked:
            try:
                conn.rollback()
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (str(key),))
                conn.commit()
            except Exception:
                logger.exception("Failed to release Adventures upload lock")
                conn.rollback()
        db.put_db_conn(conn)


def get(user_id, trip_id, photo_id):
    with db.transaction() as cur:
        return _present(_photo(cur, user_id, trip_id, photo_id))


def list_photos(user_id, trip_id, limit, offset):
    with db.transaction() as cur:
        db.assert_trip(cur, user_id, trip_id)
        page = db._page(cur, "FROM public.adventures_photos p JOIN public.adventures_trips t ON t.id=p.trip_id",
            "t.user_id=%s AND p.trip_id=%s", [user_id, trip_id], "p.display_order,p.uploaded_at,p.id", limit, offset,
            "p.*,t.cover_photo_id")
        page["items"] = [_present(row) for row in page["items"]]
        return page


def update(user_id, trip_id, photo_id, values):
    with db.transaction() as cur:
        _photo(cur, user_id, trip_id, photo_id, lock=True)
        db._update(cur, "adventures_photos", values, "trip_id=%s AND id=%s", (trip_id, photo_id))
    return get(user_id, trip_id, photo_id)


def set_cover(user_id, trip_id, photo_id):
    db.today()
    with db.transaction() as cur:
        db.assert_trip(cur, user_id, trip_id, lock=True)
        if photo_id is not None:
            _photo(cur, user_id, trip_id, photo_id)
        cur.execute("UPDATE public.adventures_trips SET cover_photo_id=%s,updated_at=now() WHERE id=%s", (photo_id, trip_id))
    return db.get_trip(user_id, trip_id)


def delete(user_id, trip_id, photo_id):
    with db.transaction() as cur:
        db.assert_trip(cur, user_id, trip_id, lock=True)
        photo = _photo(cur, user_id, trip_id, photo_id, lock=True)
        cur.execute("UPDATE public.adventures_trips SET cover_photo_id=NULL,updated_at=now() WHERE id=%s AND cover_photo_id=%s", (trip_id, photo_id))
        cur.execute("INSERT INTO public.adventures_photo_cleanup(storage_key) VALUES (%s) ON CONFLICT DO NOTHING", (photo["storage_key"],))
        db._delete(cur, "adventures_photos", "trip_id=%s AND id=%s", (trip_id, photo_id))
    try:
        reconcile_cleanup()
    except db.ConfigurationError:
        logger.warning("Adventures photo deletion queued; durable media root unavailable")


def reconcile_cleanup():
    """Idempotent bounded cleanup; queued keys remain for retry on I/O failure."""
    root = storage_root()
    with db.transaction() as cur:
        cur.execute("SELECT storage_key FROM public.adventures_photo_cleanup ORDER BY queued_at LIMIT 100 FOR UPDATE SKIP LOCKED")
        for row in cur.fetchall():
            key = row["storage_key"]
            try:
                cur.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired", (str(key),))
                if not cur.fetchone()["acquired"]:
                    continue
                cur.execute("SELECT 1 FROM public.adventures_photos WHERE storage_key=%s", (key,))
                if cur.fetchone():
                    # An ambiguous commit succeeded; preserve referenced media.
                    cur.execute("DELETE FROM public.adventures_photo_cleanup WHERE storage_key=%s", (key,))
                    continue
                for mime, _ in FORMATS.values():
                    original, thumb = _paths(root, key, mime)
                    original.unlink(missing_ok=True)
                    thumb.unlink(missing_ok=True)
                for folder in ("originals", "thumbnails"):
                    for pending in (root / folder).glob(f".{key}.*.pending"):
                        pending.unlink(missing_ok=True)
                _fsync_directory(root / "originals")
                _fsync_directory(root / "thumbnails")
                cur.execute("DELETE FROM public.adventures_photo_cleanup WHERE storage_key=%s", (key,))
            except OSError:
                logger.exception("Adventures photo cleanup will retry")


async def cleanup_loop():
    """Retry queued media work at startup and periodically after process restarts."""
    while True:
        try:
            await asyncio.to_thread(reconcile_cleanup)
        except Exception:
            logger.exception("Adventures photo cleanup will retry after an error")
        await asyncio.sleep(300)


def file_for(user_id, trip_id, photo_id, variant):
    with db.transaction() as cur:
        row = _photo(cur, user_id, trip_id, photo_id)
    root = storage_root()
    original, thumb = _paths(root, row["storage_key"], row["content_type"])
    path = original if variant == "original" else thumb
    if not path.is_file() or path.is_symlink():
        raise db.NotFound("Photo file unavailable")
    return path, row["content_type"] if variant == "original" else "image/jpeg", row["original_filename"]

# Python Imports
from pathlib import Path
from uuid import uuid4

# 3rd Party Imports
from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse

# Local Imports
from backend.database.database import get_db_conn, put_db_conn
from backend.models.autographs import AutographCreate, AutographEntry


AUTOGRAPH_IMAGE_DIR = Path("/opt/remihub/data/autographs")
AUTOGRAPH_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


async def upload_autograph_image(file: UploadFile):
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Only JPEG, PNG, and WEBP images are allowed.",
        )

    extension = ALLOWED_IMAGE_TYPES[file.content_type]
    filename = f"{uuid4()}{extension}"
    file_path = AUTOGRAPH_IMAGE_DIR / filename

    try:
        contents = await file.read()

        if not contents:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")

        if len(contents) > MAX_IMAGE_SIZE_BYTES:
            raise HTTPException(
                status_code=400,
                detail="Image is too large. Maximum size is 5 MB.",
            )
        with open(file_path, "wb") as output_file:
            output_file.write(contents)

        return {
            "success": True,
            "image_path": f"/autographs/image/{filename}",
            "filename": filename,
        }

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def get_autograph_image(filename: str):
    file_path = AUTOGRAPH_IMAGE_DIR / filename

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found.")

    return FileResponse(file_path)


def add_autograph(entry: AutographCreate):
    conn = get_db_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO autograph_entries (
                driver_name,
                image_path,
                helmet_view,
                x_percent,
                y_percent,
                region,
                notes
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, driver_name, image_path, helmet_view,
                      x_percent, y_percent, region, notes, created_at
            """,
            (
                entry.driver_name,
                entry.image_path,
                entry.helmet_view,
                entry.x_percent,
                entry.y_percent,
                entry.region,
                entry.notes,
            ),
        )

        row = cur.fetchone()
        conn.commit()

        return AutographEntry(
            id=row[0],
            driver_name=row[1],
            image_path=row[2],
            helmet_view=row[3],
            x_percent=row[4],
            y_percent=row[5],
            region=row[6],
            notes=row[7],
            created_at=row[8],
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        cur.close()
        put_db_conn(conn)


def get_all_autographs():
    conn = get_db_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT id, driver_name, image_path, helmet_view,
                   x_percent, y_percent, region, notes, created_at
            FROM autograph_entries
            ORDER BY created_at DESC
            """
        )

        rows = cur.fetchall()

        return [
            AutographEntry(
                id=row[0],
                driver_name=row[1],
                image_path=row[2],
                helmet_view=row[3],
                x_percent=row[4],
                y_percent=row[5],
                region=row[6],
                notes=row[7],
                created_at=row[8],
            )
            for row in rows
        ]

    finally:
        cur.close()
        put_db_conn(conn)

def delete_autograph(autograph_id: int):
    conn = get_db_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT image_path
            FROM autograph_entries
            WHERE id = %s
            """,
            (autograph_id,),
        )

        row = cur.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Autograph not found.")

        image_path = row[0]

        cur.execute(
            """
            DELETE FROM autograph_entries
            WHERE id = %s
            """,
            (autograph_id,),
        )

        if image_path:
            filename = Path(image_path).name
            file_path = AUTOGRAPH_IMAGE_DIR / filename

            if file_path.exists() and file_path.is_file():
                file_path.unlink()

        conn.commit()

        return {
            "success": True,
            "deleted_id": autograph_id,
        }

    except HTTPException:
        conn.rollback()
        raise

    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc))

    finally:
        cur.close()
        put_db_conn(conn)

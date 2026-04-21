# Python Imports
from datetime import datetime
import os
from pathlib import Path
import uuid

# 3rd Party Imports
from pydantic import BaseModel

# Local Imports
from backend.database.database import get_db_conn, put_db_conn


CRAWLJOB_DIR = Path(os.getenv("REMHUB_CRAWLJOB_DIR", "/srv/remihub/Temp/JDownloaderWatch/"))
CATEGORY_PATHS = {
    "Movies": os.getenv("REMHUB_MOVIES_DIR", "/srv/remihub/Temp/Movies"),
    "TV": os.getenv("REMHUB_TV_DIR", "/srv/remihub/Temp/TV"),
}

class DownloadRequest(BaseModel):
    url: str
    category: str
    name: str


def create_crawljob_file(req: DownloadRequest):
    job_id = str(uuid.uuid4())
    filename = CRAWLJOB_DIR / f"remihub_{job_id}.crawljob"
    download_path = CATEGORY_PATHS[req.category]

    job_content = f"""
        text={req.url}
        enabled=true
        autoStart=true
        packageName=RemiHub Automated Download - {req.category}
        downloadFolder={download_path}
        """.strip()

    # Drop the file to the download directory
    filename.write_text(job_content)

    # Log the request in the database
    log_download_request(req, job_id)

    return {"success": True, "message": "Download job added."}


def log_download_request(req: DownloadRequest, job_id: str):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO plex_download_requests (id, url, name, category)
                VALUES (%s, %s, %s, %s)
                """,
                (job_id, req.url, req.name, req.category)
            )
        conn.commit()
    finally:
        put_db_conn(conn)

def get_recent_download_requests():
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, url, name, category, requested_at
                FROM plex_download_requests
                ORDER BY requested_at DESC
                LIMIT 10
            """)
            rows = cur.fetchall()
            return {"success": True, "data": [dict(zip([desc[0] for desc in cur.description], row)) for row in rows]}
    finally:
        put_db_conn(conn)


if __name__ == '__main__':
    temp = DownloadRequest(
        url="mega.nz/thisisatestfile",
        category="Movies",
        name="Some Test",
    )

    create_crawljob_file(temp)


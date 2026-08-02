import os
from pathlib import Path
import uuid

# 3rd Party Imports
from pydantic import BaseModel

CRAWLJOB_DIR = Path(os.getenv("REMHUB_CRAWLJOB_DIR", "/srv/remihub/Temp/JDownloaderWatch/"))
CATEGORY_PATHS = {
    "Movies": os.getenv("REMHUB_MOVIES_DIR", "/srv/remihub/Temp/Movies"),
    "TV": os.getenv("REMHUB_TV_DIR", "/srv/remihub/Temp/TV"),
}
MAX_FILENAME_ATTEMPTS = 100


class EmptyDownloadUrlError(ValueError):
    pass


class DownloadRequest(BaseModel):
    url: str
    category: str
    name: str


def parse_download_urls(raw_url: str) -> list[str]:
    return [
        url
        for url in (line.strip() for line in raw_url.splitlines())
        if url
    ]


def create_crawljob_files(req: DownloadRequest):
    urls = parse_download_urls(req.url)
    if not urls:
        raise EmptyDownloadUrlError("At least one download URL is required")

    job_ids = []
    for url in urls:
        job_req = DownloadRequest(url=url, name=req.name, category=req.category)
        job_id = _write_crawljob_file(job_req)
        log_download_request(job_req, job_id)
        job_ids.append(job_id)

    return {
        "success": True,
        "message": "Download job added.",
        "count": len(job_ids),
        "job_ids": job_ids,
    }


def create_crawljob_file(req: DownloadRequest):
    create_crawljob_files(req)
    return {"success": True, "message": "Download job added."}


def _write_crawljob_file(req: DownloadRequest) -> str:
    download_path = CATEGORY_PATHS[req.category]

    job_content = f"""
        text={req.url}
        enabled=true
        autoStart=true
        packageName=RemiHub Automated Download - {req.category}
        downloadFolder={download_path}
        """.strip()

    for _ in range(MAX_FILENAME_ATTEMPTS):
        job_id = str(uuid.uuid4())
        filename = CRAWLJOB_DIR / f"remihub_{job_id}.crawljob"
        temp_filename = CRAWLJOB_DIR / f".remihub_{job_id}.crawljob.tmp"
        try:
            _write_temp_file(temp_filename, job_content)
            os.link(temp_filename, filename)
            _sync_directory(CRAWLJOB_DIR)
            return job_id
        except FileExistsError:
            continue
        finally:
            temp_filename.unlink(missing_ok=True)

    raise RuntimeError("Unable to create a unique crawljob filename")


def _write_temp_file(filename: Path, content: str):
    file_descriptor = os.open(
        filename,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(directory: Path):
    directory_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def log_download_request(req: DownloadRequest, job_id: str):
    from backend.database.database import get_db_conn, put_db_conn

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
    from backend.database.database import get_db_conn, put_db_conn

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

import os
from pathlib import Path
import uuid
from backend.services import jdownloader_api_service as jd2

# 3rd Party Imports
from pydantic import BaseModel

CRAWLJOB_DIR = Path(os.getenv("REMHUB_CRAWLJOB_DIR", "/mnt/plex-pool/Temp/JDownloaderWatch/"))
CATEGORY_PATHS = {
    "Movies": os.getenv("REMHUB_MOVIES_DIR", "/mnt/plex-pool/Temp/Movies"),
    "TV": os.getenv("REMHUB_TV_DIR", "/mnt/plex-pool/Temp/TV"),
}
MAX_FILENAME_ATTEMPTS = 100
MAX_DOWNLOAD_PAGE = 25
MAX_JD2_PAGES = 5


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


def _execute_write(sql: str, params: tuple):
    from backend.database.database import get_db_conn, put_db_conn

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def submit_downloads(req: DownloadRequest) -> dict:
    urls = parse_download_urls(req.url)
    if not urls:
        raise EmptyDownloadUrlError("At least one download URL is required")

    results = []
    for url in urls:
        request_id = str(uuid.uuid4())
        try:
            _execute_write(
                """INSERT INTO public.plex_download_requests
                   (id, url, name, category, submission_outcome)
                   VALUES (%s, %s, %s, %s, 'pending')""",
                (request_id, url, req.name, req.category),
            )
        except Exception:
            results.append({"request_id": request_id, "jd2_job_id": None,
                            "submission_outcome": "unknown", "message": "Request persistence failed; submission was not attempted"})
            break

        job_id = None
        try:
            job_id = jd2.add_links(url, CATEGORY_PATHS[req.category], req.category)
            outcome, message = "succeeded", "JDownloader confirmed submission"
        except jd2.JDownloaderError as exc:
            outcome = "unknown" if exc.ambiguous else "failed"
            message = str(exc)

        try:
            _execute_write(
                """UPDATE public.plex_download_requests
                   SET jd2_job_id = %s, submission_outcome = %s WHERE id = %s""",
                (job_id, outcome, request_id),
            )
        except Exception:
            # JD2 may already have accepted the URL; never retry automatically.
            results.append({"request_id": request_id, "jd2_job_id": job_id,
                            "submission_outcome": "unknown", "message": "Tracking update failed; manual reconciliation required"})
            break
        results.append({"request_id": request_id, "jd2_job_id": job_id,
                        "submission_outcome": outcome, "message": message})

    success = len(results) == len(urls) and all(
        result["submission_outcome"] == "succeeded" for result in results
    )
    return {
        "success": success,
        "message": "Download job added." if success else "One or more downloads were not confirmed; review request results.",
        "count": len(results),
        "requests": results,
    }


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


def _read_requests(limit: int, offset: int = 0, request_id: str | None = None) -> list[dict]:
    from backend.database.database import get_db_conn, put_db_conn

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            if request_id is None:
                cur.execute(
                    """SELECT id, name, category, requested_at, jd2_job_id, submission_outcome
                       FROM public.plex_download_requests
                       ORDER BY requested_at DESC, id DESC LIMIT %s OFFSET %s""",
                    (limit, offset),
                )
            else:
                cur.execute(
                    """SELECT id, name, category, requested_at, jd2_job_id, submission_outcome
                       FROM public.plex_download_requests WHERE id = %s""",
                    (request_id,),
                )
            names = [column[0] for column in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]
    finally:
        put_db_conn(conn)


def _nonnegative(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _file(link: dict, source: str) -> dict:
    total = _nonnegative(link.get("bytesTotal"))
    loaded = _nonnegative(link.get("bytesLoaded"))
    finished = bool(link.get("finished"))
    status = link.get("status") or ("finished" if finished else "queued")
    return {
        "id": link.get("uuid"), "name": link.get("name"), "source": source,
        "status": status, "finished": finished,
        "bytes_downloaded": loaded, "total_bytes": total,
        "download_speed": _nonnegative(link.get("speed")),
        "eta": _nonnegative(link.get("eta")),
        "extraction_status": link.get("extractionStatus"),
    }


def _summary(row: dict, files: list[dict], *, unavailable: bool = False,
             truncated: bool = False) -> dict:
    outcome = row["submission_outcome"]
    if outcome is None:
        state = "legacy"
    elif unavailable:
        state = "unavailable"
    elif outcome in {"failed", "unknown", "pending"}:
        state = outcome
    elif truncated:
        state = "partial_data"
    elif not files:
        state = "discovering"
    elif all(item["finished"] for item in files):
        state = "download_complete"
    elif any(item["source"] == "downloads" for item in files):
        state = "downloading" if any((item["download_speed"] or 0) > 0 for item in files) else "queued"
    else:
        state = "linkgrabber"

    known_totals = [item["total_bytes"] for item in files]
    total = sum(known_totals) if files and all(value is not None for value in known_totals) else None
    loaded = (sum(item["bytes_downloaded"] or 0 for item in files)
              if not files or any(item["bytes_downloaded"] is not None for item in files) else None)
    speed = (sum(item["download_speed"] or 0 for item in files)
             if any(item["download_speed"] is not None for item in files) else None)
    etas = [item["eta"] for item in files if not item["finished"]]
    eta = max(etas) if etas and all(value is not None for value in etas) else None
    percentage = round(min(100, loaded * 100 / total), 2) if total and loaded is not None else None
    return {
        "request_id": str(row["id"]), "jd2_job_id": row["jd2_job_id"],
        "name": row["name"], "category": row["category"],
        "requested_at": row["requested_at"].isoformat() if row["requested_at"] else None,
        "submission_outcome": outcome, "state": state,
        "discovered_files": len(files),
        "finished_files": sum(item["finished"] for item in files),
        "bytes_downloaded": loaded, "total_bytes": total,
        "percentage_complete": percentage, "download_speed": speed,
        "eta": eta, "truncated": truncated,
        "extraction_status": [item["extraction_status"] for item in files if item["extraction_status"] is not None],
        "files": files,
    }


def _query_job_files(job_ids: list[int], *, include_packages: bool = False) -> tuple[dict[int, list[dict]], bool]:
    by_job = {job_id: {} for job_id in job_ids}
    package_by_file = {}
    truncated = False
    for source, list_name in (("linkgrabber", "linkgrabberv2"), ("downloads", "downloadsV2")):
        for page in range(MAX_JD2_PAGES):
            links = jd2.query_links(list_name, job_ids, page * jd2.MAX_QUERY_RESULTS)
            for link in links:
                if not isinstance(link, dict):
                    continue
                job_id = link.get("jobUUID")
                if isinstance(job_id, str) and job_id.isdecimal():
                    job_id = int(job_id)
                if job_id not in by_job:
                    continue
                file_id = link.get("uuid")
                if file_id is None:
                    continue
                key = str(file_id)
                # Prefer the download-list version after automatic confirmation.
                if source == "downloads" or key not in by_job[job_id]:
                    by_job[job_id][key] = _file(link, source)
                    if source == "downloads" and link.get("packageUUID") is not None:
                        package_by_file[(job_id, key)] = link["packageUUID"]
            if len(links) < jd2.MAX_QUERY_RESULTS:
                break
        else:
            truncated = True
    if include_packages:
        package_ids = list(dict.fromkeys(package_by_file.values()))[:50]
        if package_ids:
            try:
                packages = jd2.query_packages(package_ids)
            except jd2.JDownloaderError:
                packages = []  # Link status remains usable when optional package metadata fails.
            extraction = {
                package.get("uuid"): package.get("extractionStatus")
                for package in packages if isinstance(package, dict)
            }
            for (job_id, file_id), package_id in package_by_file.items():
                if by_job[job_id][file_id]["extraction_status"] is None:
                    by_job[job_id][file_id]["extraction_status"] = extraction.get(package_id)
    return {job_id: list(files.values()) for job_id, files in by_job.items()}, truncated


def get_downloads(limit: int = 10, offset: int = 0, request_id: str | None = None) -> dict:
    if not 1 <= limit <= MAX_DOWNLOAD_PAGE or not 0 <= offset <= 10000:
        raise ValueError("Invalid pagination")
    rows = _read_requests(limit, offset, request_id)
    job_ids = list(dict.fromkeys(row["jd2_job_id"] for row in rows if row["jd2_job_id"] is not None))
    files_by_job = {}
    unavailable = truncated = False
    if job_ids:
        try:
            files_by_job, truncated = _query_job_files(job_ids, include_packages=request_id is not None)
        except jd2.JDownloaderError:
            unavailable = True
    return {
        "success": True,
        "data": [_summary(row, files_by_job.get(row["jd2_job_id"], []),
                          unavailable=unavailable and row["jd2_job_id"] is not None,
                          truncated=truncated) for row in rows],
        "limit": limit, "offset": offset, "jd2_available": not unavailable,
    }


if __name__ == '__main__':
    temp = DownloadRequest(
        url="mega.nz/thisisatestfile",
        category="Movies",
        name="Some Test",
    )

    create_crawljob_file(temp)

import os
import math
import re
from pathlib import Path
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
MAX_DISCOVERY_JOBS = 10
DISCOVERY_AGE = timedelta(days=7)
# JD2's queried link lists do not expose per-job crawl completion or duplicates.
# A successful addLinks response only means that JD2 accepted a job ID. After
# this grace period, require two complete empty observations before reporting
# unresolved discovery. These are application policy, not claims about JD2.
EMPTY_DISCOVERY_DETAIL = "JD2 link inventory was empty; awaiting confirmation"
UNRESOLVED_DISCOVERY_DETAIL = (
    "No links were found after repeated JD2 checks; discovery outcome could not be verified. "
    "Check JDownloader before submitting again."
)


def _discovery_duration(name: str, default: float, minimum: float) -> timedelta:
    try:
        hours = float(os.getenv(name, str(default)))
    except ValueError:
        hours = default
    if not math.isfinite(hours):
        hours = default
    return timedelta(hours=min(max(hours, minimum), 24 * 30))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


MEDIA_SUFFIXES = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv"}
ARCHIVE_SUFFIXES = {".rar", ".zip", ".7z"}


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
    return (value if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0 else None)


_RAR_PART = re.compile(r"^(?P<stem>.+?)\.part0*(?P<part>[1-9]\d*)\.rar$", re.I)
_RAR_VOLUME = re.compile(r"^(?P<stem>.+?)\.r(?P<part>\d{2,3})$", re.I)
_EPISODE = re.compile(r"^(?P<show>.+?)[ ._-]+(?P<episode>s\d{1,2}e\d{1,2}(?:e\d{1,2})?)\b", re.I)
_COMPANION_SUFFIXES = MEDIA_SUFFIXES | {".srt", ".ass", ".ssa", ".sub", ".nfo"}


def _logical_group(file: dict, request_id: str) -> tuple[str, str]:
    """A package is evidence, but may contain many unrelated archive sets."""
    name = file.get("name")
    package_id = file.get("package_id")
    if not isinstance(name, str) or not name or Path(name).name != name:
        return f"request:{request_id}:link:{file.get('id')}", "Unnamed file"
    scope = (f"package:{package_id}" if isinstance(package_id, int)
             and not isinstance(package_id, bool) and package_id > 0 else f"request:{request_id}")
    part = _RAR_PART.fullmatch(name)
    volume = _RAR_VOLUME.fullmatch(name)
    if part or volume:
        stem = (part or volume).group("stem").strip(" ._-")
        if stem:
            return f"{scope}:archive:{stem.casefold()}", stem
    if name.lower().endswith(".rar"):
        stem = name[:-4].strip(" ._-")
        if stem:
            return f"{scope}:archive:{stem.casefold()}", stem
    episode = _EPISODE.match(name)
    if episode:
        title = f"{episode.group('show').strip(' ._-')} {episode.group('episode').upper()}"
        return f"{scope}:episode:{title.casefold()}", title
    if scope.startswith("package:") and Path(name).suffix.lower() in _COMPANION_SUFFIXES:
        stem = Path(name).stem.strip(" ._-")
        if stem:
            return f"{scope}:content:{stem.casefold()}", stem
    return f"request:{request_id}:link:{file.get('id')}", name


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
        "package_id": link.get("packageUUID"),
        "job_id": link.get("jobUUID"),
        "enabled": link.get("enabled") if isinstance(link.get("enabled"), bool) else None,
        "package_name": None, "package_child_count": None,
    }


def _persisted_file(link: dict) -> dict:
    """Show last observed link identity when JD2 no longer returns its job UUID."""
    return {
        "id": link["link_id"], "name": link["name"], "source": "persisted",
        "status": "Last observed complete" if link["finished"] else "Live status unknown",
        "finished": bool(link["finished"]), "bytes_downloaded": None,
        "total_bytes": None, "download_speed": None, "eta": None,
        "extraction_status": link.get("extraction_status"),
        "package_id": link.get("package_id"), "job_id": link.get("jd2_job_id"),
        "enabled": None, "package_name": None, "package_child_count": None,
    }


def _summary(row: dict, files: list[dict], *, unavailable: bool = False,
             truncated: bool = False, inventory_incomplete: bool = False,
             tracking: dict | None = None) -> dict:
    tracking = tracking or {"links": [], "processed": [], "event": None}
    outcome = row["submission_outcome"]
    if outcome is None:
        state = "submitted"
    elif unavailable:
        state = "unavailable"
    elif outcome in {"failed", "unknown"}:
        state = "failed" if outcome == "failed" else "unavailable"
    elif outcome == "pending":
        state = "submitted"
    elif truncated:
        state = "unavailable"
    elif not files:
        state = "discovering"
    elif all(item["finished"] for item in files):
        state = "download_complete"
    elif any(item["source"] == "downloads" for item in files):
        state = "downloading" if any((item["download_speed"] or 0) > 0 for item in files) else "queued"
    else:
        state = "discovering"

    links = tracking["links"]
    processed = tracking["processed"]
    event = tracking["event"]
    if event and event["state"] == "failed" and event.get("detail") == UNRESOLVED_DISCOVERY_DETAIL and not files and not links:
        state = "failed"
    if not files and links and not unavailable:
        state = "download_complete" if all(link["finished"] for link in links) else "queued"
    if any(link["kind"] == "archive" for link in links):
        statuses = [str(link.get("extraction_status") or "").lower() for link in links]
        if any("extracting" in value or "running" in value for value in statuses):
            state = "extracting"
        elif state == "download_complete":
            state = "waiting_for_extraction"
    elif state == "download_complete" and any(item["state"] == "queued_for_plex" for item in processed):
        state = "queued_for_plex"
    if any(item["state"] == "failed" for item in processed):
        state = "failed"
    elif any(item["state"] == "processing" for item in processed):
        state = "processing"
    elif event and event["state"] == "completed":
        state = "completed"
    if unavailable and state not in {"completed", "failed"}:
        state = "unavailable"
    if inventory_incomplete and state not in {"completed", "failed"}:
        state = "unavailable"

    known_totals = [item["total_bytes"] for item in files]
    valid_totals = bool(files) and all(
        _nonnegative(item["total_bytes"]) is not None and item["total_bytes"] > 0
        and _nonnegative(item["bytes_downloaded"]) is not None
        and item["bytes_downloaded"] <= item["total_bytes"] for item in files)
    total = sum(known_totals) if valid_totals and not inventory_incomplete and not truncated else None
    loaded = (sum(item["bytes_downloaded"] or 0 for item in files)
              if not files or any(item["bytes_downloaded"] is not None for item in files) else None)
    if inventory_incomplete or truncated:
        loaded = None
    speed = (sum(item["download_speed"] or 0 for item in files)
             if any(item["download_speed"] is not None for item in files) else None)
    if inventory_incomplete or truncated:
        speed = None
    etas = [item["eta"] for item in files if not item["finished"]]
    eta = max(etas) if etas and all(value is not None for value in etas) else None
    percentage = round(loaded * 100 / total, 2) if total and loaded is not None and not truncated else None
    return {
        "request_id": str(row["id"]), "jd2_job_id": row["jd2_job_id"],
        "name": row["name"], "category": row["category"],
        "requested_at": row["requested_at"].isoformat() if row["requested_at"] else None,
        "submission_outcome": outcome, "state": state,
        "discovered_files": len(files),
        "finished_files": sum(item["finished"] for item in files
                              if not inventory_incomplete or item["source"] != "persisted"),
        "bytes_downloaded": loaded, "total_bytes": total,
        "percentage_complete": percentage, "download_speed": speed,
        "eta": None if inventory_incomplete else eta, "truncated": truncated,
        "inventory_incomplete": inventory_incomplete,
        "jd2_unavailable": unavailable,
        "extraction_status": [item["extraction_status"] for item in files if item["extraction_status"] is not None],
        "files": files,
        "lifecycle_state": state,
        "file_count": (sum(link["kind"] == "media" for link in links) or None)
                      if not any(link["kind"] == "archive" for link in links) else None,
        "completed_file_count": len({item["link_id"] for item in processed
                                     if item["state"] == "completed" and item["link_id"] is not None}),
        "processing_status": "failed" if any(item["state"] == "failed" for item in processed) else (
            "completed" if state == "completed" else "processing" if processed else None),
        "final_plex_destination": next((item["destination_path"] for item in processed
                                        if item["state"] == "completed"), None),
        "completed_at": (event["occurred_at"].isoformat() if event and event["state"] == "completed"
                         and event.get("occurred_at") else None),
        "failure": next((item["error"] for item in processed if item["state"] == "failed"), None)
                   or (event.get("detail") if event and state == "failed" else None),
        "cleanup_eligible": False,
        "cleanup_reason": "JD2 link-only removal is disabled pending semantic verification",
        "jd2_entries_removed": bool(event and event["state"] == "jd2_entries_removed"),
        "processing_files": [{**item, "completed_at": item["completed_at"].isoformat()
                              if item["completed_at"] else None} for item in processed],
        "legacy": outcome is None,
    }


def _query_job_files(job_ids: list[int], *, include_packages: bool = False,
                     known_link_jobs: dict[str, int | None] | None = None,
                     discover_job_ids: list[int] | None = None,
                     require_complete_ownership: bool = False) -> tuple[dict[int, list[dict]], bool]:
    by_job = {job_id: {} for job_id in job_ids}
    package_by_file = {}
    unclaimed_downloads: dict[str, dict] = {}
    live_link_jobs: dict[str, int | None] = {}
    truncated = False
    for source, list_name in (("linkgrabber", "linkgrabberv2"), ("downloads", "downloadsV2")):
        for page in range(MAX_JD2_PAGES):
            links = jd2.query_links(list_name, job_ids, page * jd2.MAX_QUERY_RESULTS)
            for link in links:
                if not isinstance(link, dict):
                    continue
                file_id = link.get("uuid")
                key = str(file_id) if file_id is not None else None
                job_id = link.get("jobUUID")
                if isinstance(job_id, str) and job_id.isdecimal():
                    job_id = int(job_id)
                if key is not None and job_id in by_job:
                    if key not in live_link_jobs:
                        live_link_jobs[key] = job_id
                    elif live_link_jobs[key] != job_id:
                        live_link_jobs[key] = None
                if job_id is None and key is not None:
                    persisted_owner = (known_link_jobs or {}).get(key)
                    live_owner = live_link_jobs.get(key)
                    if known_link_jobs is not None and key in known_link_jobs:
                        job_id = (persisted_owner if key not in live_link_jobs or
                                  live_owner == persisted_owner else None)
                    else:
                        job_id = live_owner
                if job_id not in by_job:
                    if source == "downloads" and link.get("jobUUID") is None and key is not None:
                        unclaimed_downloads[key] = link
                    continue
                if file_id is None:
                    continue
                # Prefer the download-list version after automatic confirmation.
                if source == "downloads" or key not in by_job[job_id]:
                    by_job[job_id][key] = _file(link, source)
                    if by_job[job_id][key]["job_id"] is None:
                        by_job[job_id][key]["job_id"] = job_id
                    if source == "downloads" and link.get("packageUUID") is not None:
                        package_by_file[(job_id, key)] = link["packageUUID"]
            if len(links) < jd2.MAX_QUERY_RESULTS:
                break
        else:
            truncated = True
    # A filtered response is not proof that JD2 excluded other real jobs' links.
    # Discover only links whose own jobUUID provides positive ownership evidence.
    if discover_job_ids and unclaimed_downloads:
        try:
            candidates = {}
            for job_id in discover_job_ids:
                for page in range(MAX_JD2_PAGES):
                    links = jd2.query_links("downloadsV2", [job_id], page * jd2.MAX_QUERY_RESULTS)
                    for link in links:
                        if not isinstance(link, dict) or link.get("uuid") is None:
                            continue
                        key = str(link["uuid"])
                        if key in unclaimed_downloads and str(link.get("jobUUID")) == str(job_id):
                            candidates.setdefault(key, []).append((job_id, link))
                    if len(links) < jd2.MAX_QUERY_RESULTS:
                        break
                else:
                    truncated = True
            for key, matches in candidates.items():
                if len({job_id for job_id, _ in matches}) != 1:
                    continue
                job_id, link = matches[0]
                owner = (known_link_jobs or {}).get(key)
                if known_link_jobs and key in known_link_jobs and owner != job_id:
                    continue
                by_job[job_id][key] = _file(link, "downloads")
                if link.get("packageUUID") is not None:
                    package_by_file[(job_id, key)] = link["packageUUID"]
        except jd2.JDownloaderError:
            if require_complete_ownership:
                raise  # A failed ownership probe cannot certify an empty inventory.
            pass  # Keep existing live status for links whose ownership is known.
    if include_packages:
        package_ids = list(dict.fromkeys(package_by_file.values()))
        if package_ids:
            try:
                packages = []
                for start in range(0, len(package_ids), 50):
                    packages.extend(jd2.query_packages(package_ids[start:start + 50]))
            except jd2.JDownloaderError:
                packages = []  # Link status remains usable when optional package metadata fails.
            extraction = {
                str(package.get("uuid")): package
                for package in packages if isinstance(package, dict)
            }
            for (job_id, file_id), package_id in package_by_file.items():
                package = extraction.get(str(package_id), {})
                if by_job[job_id][file_id]["extraction_status"] is None:
                    by_job[job_id][file_id]["extraction_status"] = package.get("extractionStatus")
                by_job[job_id][file_id]["save_to"] = package.get("saveTo")
                by_job[job_id][file_id]["package_name"] = package.get("name")
                by_job[job_id][file_id]["package_child_count"] = package.get("childCount")
    return {job_id: list(files.values()) for job_id, files in by_job.items()}, truncated


def _discovery_jobs(rows: list[dict], tracking: dict[str, dict] | None = None,
                    now: datetime | None = None) -> list[int]:
    now = now or datetime.now(timezone.utc)
    candidates = []
    for row in rows:
        requested = row.get("requested_at")
        if not isinstance(requested, datetime):
            continue
        if requested.tzinfo is None:
            requested = requested.replace(tzinfo=timezone.utc)
        links = tracking.get(str(row["id"]), {}).get("links", []) if tracking is not None else None
        unresolved = not links if links is not None else row.get("needs_discovery", False)
        active = (any(link.get("finished") is False for link in links) if links is not None
                  else row.get("has_unfinished_links", False))
        recent = timedelta(0) <= now - requested <= DISCOVERY_AGE
        if (row.get("jd2_job_id") is not None and row.get("submission_outcome") == "succeeded"
                and (recent or unresolved or active)):
            # Older unresolved or unfinished jobs can still produce later parts.
            candidates.append(row["jd2_job_id"])
    if len(candidates) > MAX_DISCOVERY_JOBS:
        # Rotate the bounded probe window so jobs still crawling do not starve
        # later submissions on the same dashboard or monitor page.
        start = int(now.timestamp() // 30) % len(candidates)
        candidates = candidates[start:] + candidates[:start]
    return candidates[:MAX_DISCOVERY_JOBS]


def _merge_observed_files(live: list[dict], links: list[dict]) -> tuple[list[dict], bool]:
    live_ids = {str(file["id"]) for file in live if file.get("id") is not None}
    missing = [_persisted_file(link) for link in links
               if link.get("link_id") is not None and isinstance(link.get("name"), str)
               and str(link["link_id"]) not in live_ids]
    return [*live, *missing], bool(missing)


def _known_link_jobs(tracking: dict[str, dict]) -> dict[str, int | None]:
    ownership: dict[str, int | None] = {}
    for record in tracking.values():
        for link in record["links"]:
            if link.get("link_id") is None or link.get("jd2_job_id") is None:
                continue
            key = str(link["link_id"])
            job = link["jd2_job_id"]
            if key not in ownership:
                ownership[key] = job
            elif ownership[key] != job:
                # None is an absorbing conflict: later tracking rows cannot claim it.
                ownership[key] = None
    return ownership


def get_downloads(limit: int = 10, offset: int = 0, request_id: str | None = None) -> dict:
    if not 1 <= limit <= MAX_DOWNLOAD_PAGE or not 0 <= offset <= 10000:
        raise ValueError("Invalid pagination")
    rows = _read_requests(limit, offset, request_id)
    job_ids = list(dict.fromkeys(row["jd2_job_id"] for row in rows if row["jd2_job_id"] is not None))
    files_by_job = {}
    unavailable = truncated = False
    tracking = {str(row["id"]): read_tracking(str(row["id"])) for row in rows}
    known_link_jobs = _known_link_jobs(tracking)
    if job_ids:
        try:
            files_by_job, truncated = _query_job_files(
                job_ids, include_packages=True, known_link_jobs=known_link_jobs,
                discover_job_ids=_discovery_jobs(rows, tracking))
        except jd2.JDownloaderError:
            unavailable = True
    for row in rows:
        key = str(row["id"])
        if row["jd2_job_id"] is not None and not unavailable and not truncated:
            # Inventory is durable even when JD2 later removes a link.
            sync_link_inventory(row, files_by_job.get(row["jd2_job_id"], []))
            tracking[key] = read_tracking(key)
    summaries = []
    for row in rows:
        record = tracking[str(row["id"])]
        files, incomplete = _merge_observed_files(
            files_by_job.get(row["jd2_job_id"], []), record["links"])
        summaries.append(_summary(row, files,
                                  unavailable=unavailable and row["jd2_job_id"] is not None,
                                  truncated=truncated, inventory_incomplete=incomplete,
                                  tracking=record))
    for summary in summaries:
        for file in summary["files"]:
            file["logical_group_key"], file["logical_group_name"] = _logical_group(
                file, summary["request_id"])
    for row, summary in zip(rows, summaries):
        if row["submission_outcome"] is not None:
            record_request_lifecycle(str(row["id"]), summary["state"])
    return {
        "success": True,
        "data": summaries,
        "limit": limit, "offset": offset, "jd2_available": not unavailable,
    }


@contextmanager
def _transaction():
    from backend.database.database import get_db_conn, put_db_conn

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _kind(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in MEDIA_SUFFIXES:
        return "media"
    if suffix in ARCHIVE_SUFFIXES:
        return "archive"
    return "other"


def _source_path(row: dict, file: dict) -> str | None:
    """A JD2 package path is evidence only when it stays inside the requested root."""
    name, save_to = file.get("name"), file.get("save_to")
    if not isinstance(name, str) or Path(name).name != name or not isinstance(save_to, str):
        return None
    root = Path(CATEGORY_PATHS[row["category"]]).resolve()
    candidate = (Path(save_to) / name).resolve()
    if candidate == root or root not in candidate.parents:
        return None
    return str(candidate)


def sync_link_inventory(row: dict, files: list[dict]) -> None:
    if row["jd2_job_id"] is None:
        return
    with _transaction() as cur:
        for file in files:
            if file.get("source") != "downloads" or file.get("id") is None:
                continue
            job_id = file.get("job_id")
            if str(job_id) != str(row["jd2_job_id"]):
                continue
            name = file.get("name")
            if not isinstance(name, str):
                continue
            try:
                link_id = int(file["id"])
                package_id = int(file["package_id"]) if file.get("package_id") is not None else None
            except (TypeError, ValueError):
                continue
            cur.execute("""INSERT INTO public.plex_download_links
                (request_id, link_id, jd2_job_id, package_id, source_path, name, kind, finished, extraction_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (request_id, link_id) DO UPDATE SET
                    package_id = EXCLUDED.package_id, source_path = EXCLUDED.source_path,
                    name = EXCLUDED.name, kind = EXCLUDED.kind, finished = EXCLUDED.finished,
                    extraction_status = EXCLUDED.extraction_status, observed_at = CURRENT_TIMESTAMP
                WHERE (plex_download_links.package_id, plex_download_links.source_path,
                       plex_download_links.name, plex_download_links.kind, plex_download_links.finished,
                       plex_download_links.extraction_status) IS DISTINCT FROM
                      (EXCLUDED.package_id, EXCLUDED.source_path, EXCLUDED.name, EXCLUDED.kind,
                       EXCLUDED.finished, EXCLUDED.extraction_status)""",
                (str(row["id"]), link_id, row["jd2_job_id"], package_id,
                 _source_path(row, file), name, _kind(name), bool(file["finished"]),
                 file.get("extraction_status")),
            )
            if cur.rowcount:
                cur.execute("""SELECT state FROM public.plex_download_events
                               WHERE request_id = %s AND file_id IS NULL ORDER BY id DESC LIMIT 1""",
                            (str(row["id"]),))
                latest = cur.fetchone()
                if latest and latest[0] == "completed":
                    _append_request_event(cur, str(row["id"]), "queued_for_plex",
                                          "JD2 link inventory changed")
        _maybe_complete(cur, str(row["id"]))


def refresh_link_inventory():
    """Called by the existing Plex monitor loop; no additional worker is needed."""
    with _transaction() as cur:
        cur.execute("""SELECT requests.id, requests.name, requests.category,
                              requests.jd2_job_id, requests.submission_outcome,
                              requests.requested_at,
                              NOT EXISTS (SELECT 1 FROM public.plex_download_links AS links
                                          WHERE links.request_id = requests.id) AS needs_discovery,
                              EXISTS (SELECT 1 FROM public.plex_download_links AS links
                                      WHERE links.request_id = requests.id AND NOT links.finished)
                                  AS has_unfinished_links
                       FROM public.plex_download_requests AS requests
                       WHERE requests.jd2_job_id IS NOT NULL
                       ORDER BY requests.requested_at DESC""")
        rows = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
        # Include every persisted owner, including other monitor batches, so a
        # conflicted UUID cannot be claimed by whichever batch sees it first.
        cur.execute("""SELECT link_id, jd2_job_id FROM public.plex_download_links
                       WHERE link_id IS NOT NULL AND jd2_job_id IS NOT NULL""")
        ownership = _known_link_jobs({"all": {"links": [
            {"link_id": link_id, "jd2_job_id": job_id}
            for link_id, job_id in cur.fetchall()
        ]}})
    for start in range(0, len(rows), 50):
        batch = rows[start:start + 50]
        job_ids = [row["jd2_job_id"] for row in batch]
        files_by_job, truncated = _query_job_files(
            job_ids, include_packages=True, known_link_jobs=ownership,
            discover_job_ids=_discovery_jobs(batch))
        if truncated:
            # A large job can exhaust the shared 1,000-link page cap and hide
            # another job's empty result. Probe only old, linkless jobs on their
            # own; each probe has the same fixed page bound. A truncated or
            # failed individual probe cannot advance the empty clock.
            eligible = []
            grace = _discovery_duration("REMIHUB_JD2_EMPTY_DISCOVERY_HOURS", 168, 1)
            now = datetime.now(timezone.utc)
            for row in batch:
                requested = row.get("requested_at")
                if (row["submission_outcome"] == "succeeded" and row["needs_discovery"]
                        and isinstance(requested, datetime) and now - _utc(requested) >= grace):
                    eligible.append(row)
            for row in eligible:
                try:
                    checked, incomplete = _query_job_files(
                        [row["jd2_job_id"]], include_packages=True, known_link_jobs=ownership,
                        discover_job_ids=[row["jd2_job_id"]], require_complete_ownership=True)
                except jd2.JDownloaderError:
                    continue
                if incomplete:
                    continue
                files = checked.get(row["jd2_job_id"], [])
                if not files:
                    try:
                        files = _reconcile_empty_discovery(row, ownership)
                    except jd2.JDownloaderError:
                        continue
                sync_link_inventory(row, files)
                record = read_tracking(str(row["id"]))
                merged, incomplete = _merge_observed_files(files, record["links"])
                summary = _summary(row, merged, inventory_incomplete=incomplete, tracking=record)
                record_request_lifecycle(str(row["id"]), summary["state"])
            continue
        for row in batch:
            files = files_by_job.get(row["jd2_job_id"], [])
            if row["submission_outcome"] == "succeeded" and not files:
                files = _reconcile_empty_discovery(row, ownership)
            sync_link_inventory(row, files)
            if row["submission_outcome"] is not None:
                record = read_tracking(str(row["id"]))
                merged, incomplete = _merge_observed_files(files, record["links"])
                summary = _summary(row, merged, inventory_incomplete=incomplete, tracking=record)
                record_request_lifecycle(str(row["id"]), summary["state"])


def _reconcile_empty_discovery(row: dict, ownership: dict[str, int | None]) -> list[dict]:
    """Bound an unresolvable empty job using two clean observations.

    Empty query results cannot establish that a crawl finished or was a duplicate.
    The existing monitor is the only caller; API reads never start the clock.
    """
    requested = row.get("requested_at")
    if not isinstance(requested, datetime):
        return []
    now = datetime.now(timezone.utc)
    grace = _discovery_duration("REMIHUB_JD2_EMPTY_DISCOVERY_HOURS", 168, 1)
    confirmation = _discovery_duration("REMIHUB_JD2_EMPTY_CONFIRM_HOURS", 0.5, 1 / 12)
    if now - _utc(requested) < grace:
        return []
    request_id = str(row["id"])
    with _transaction() as cur:
        cur.execute("SELECT id FROM public.plex_download_requests WHERE id = %s FOR UPDATE", (request_id,))
        if cur.fetchone() is None:
            return []
        cur.execute("""SELECT EXISTS (SELECT 1 FROM public.plex_download_links WHERE request_id = %s),
                              EXISTS (SELECT 1 FROM public.plex_download_files WHERE request_id = %s)""",
                    (request_id, request_id))
        has_links, has_files = cur.fetchone()
        if has_links or has_files:
            return []
        cur.execute("""SELECT state, detail, occurred_at FROM public.plex_download_events
                       WHERE request_id = %s AND file_id IS NULL ORDER BY id DESC LIMIT 1""", (request_id,))
        event = cur.fetchone()
        if event and event[0] in {"completed", "failed"}:
            return []
        if not event or event[0] != "discovering" or event[1] != EMPTY_DISCOVERY_DETAIL:
            _append_request_event(cur, request_id, "discovering", EMPTY_DISCOVERY_DETAIL)
            return []
        if now - _utc(event[2]) < confirmation:
            return []

    # Recheck this job immediately before declaring it unresolved. An API error,
    # incomplete page, or ownership uncertainty is not an empty observation.
    checked, truncated = _query_job_files(
        [row["jd2_job_id"]], include_packages=True, known_link_jobs=ownership,
        discover_job_ids=[row["jd2_job_id"]], require_complete_ownership=True)
    if truncated:
        return []
    files = checked.get(row["jd2_job_id"], [])
    if files:
        return files
    with _transaction() as cur:
        cur.execute("SELECT id FROM public.plex_download_requests WHERE id = %s FOR UPDATE", (request_id,))
        if cur.fetchone() is None:
            return []
        cur.execute("""SELECT EXISTS (SELECT 1 FROM public.plex_download_links WHERE request_id = %s),
                              EXISTS (SELECT 1 FROM public.plex_download_files WHERE request_id = %s)""",
                    (request_id, request_id))
        if any(cur.fetchone()):
            return []
        cur.execute("""SELECT state, detail FROM public.plex_download_events
                       WHERE request_id = %s AND file_id IS NULL ORDER BY id DESC LIMIT 1""", (request_id,))
        if cur.fetchone() == ("discovering", EMPTY_DISCOVERY_DETAIL):
            _append_request_event(cur, request_id, "failed", UNRESOLVED_DISCOVERY_DETAIL)
    return []


def read_tracking(request_id: str) -> dict:
    with _transaction() as cur:
        cur.execute("""SELECT link_id, jd2_job_id, package_id, source_path, name, kind,
                              finished, extraction_status FROM public.plex_download_links
                       WHERE request_id = %s ORDER BY link_id""", (request_id,))
        links = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
        cur.execute("""SELECT id, link_id, source_path, destination_path, state, outcome, error,
                              completed_at FROM public.plex_download_files
                       WHERE request_id = %s ORDER BY discovered_at, id""", (request_id,))
        processed = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
        cur.execute("""SELECT state, detail, occurred_at FROM public.plex_download_events
                       WHERE request_id = %s AND file_id IS NULL ORDER BY id DESC LIMIT 1""",
                    (request_id,))
        event_row = cur.fetchone()
        event = dict(zip([d[0] for d in cur.description], event_row)) if event_row else None
    return {"links": links, "processed": processed, "event": event}


def _append_request_event(cur, request_id: str, state: str, detail: str | None = None):
    cur.execute("SELECT id FROM public.plex_download_requests WHERE id = %s FOR UPDATE", (request_id,))
    cur.execute("""SELECT state, detail FROM public.plex_download_events
                   WHERE request_id = %s AND file_id IS NULL ORDER BY id DESC LIMIT 1""", (request_id,))
    if cur.fetchone() != (state, detail):
        cur.execute("""INSERT INTO public.plex_download_events (request_id, state, detail)
                       VALUES (%s, %s, %s)""", (request_id, state, detail))


def record_request_lifecycle(request_id: str, state: str):
    with _transaction() as cur:
        cur.execute("SELECT id FROM public.plex_download_requests WHERE id = %s FOR UPDATE", (request_id,))
        if cur.fetchone() is None:
            return
        cur.execute("""SELECT state, detail FROM public.plex_download_events
                       WHERE request_id = %s AND file_id IS NULL ORDER BY id DESC LIMIT 1""", (request_id,))
        latest = cur.fetchone()
        if latest == (state, EMPTY_DISCOVERY_DETAIL):
            return
        if latest == ("failed", UNRESOLVED_DISCOVERY_DETAIL) and state in {"discovering", "failed", "unavailable"}:
            return
        _append_request_event(cur, request_id, state)


def _maybe_complete(cur, request_id: str):
    cur.execute("""SELECT COALESCE(bool_and(observed_at <= CURRENT_TIMESTAMP - INTERVAL '60 seconds'), false)
                   FROM public.plex_download_links WHERE request_id = %s""", (request_id,))
    if not cur.fetchone()[0]:
        return
    cur.execute("""SELECT kind, finished, source_path, link_id FROM public.plex_download_links
                   WHERE request_id = %s""", (request_id,))
    links = cur.fetchall()
    if not links or any(kind == "archive" or not finished for kind, finished, _, _ in links):
        return
    media = [(path, link_id) for kind, _, path, link_id in links if kind == "media"]
    if not media or any(path is None for path, _ in media):
        return
    cur.execute("""SELECT source_path, link_id, state FROM public.plex_download_files
                   WHERE request_id = %s""", (request_id,))
    done = {(path, link_id) for path, link_id, state in cur.fetchall() if state == "completed"}
    if all(item in done for item in media):
        _append_request_event(cur, request_id, "completed")


def record_file_state(source: str, state: str, *, destination: str | None = None,
                      error: str | None = None, source_size: int | None = None) -> dict:
    """Persist a file transition; ownership requires one exact job-linked path."""
    source = str(Path(source).resolve())
    with _transaction() as cur:
        cur.execute("""SELECT id, request_id, jd2_job_id, link_id, state, destination_path
                       FROM public.plex_download_files WHERE source_path = %s
                       ORDER BY discovered_at DESC, id DESC LIMIT 1 FOR UPDATE""", (source,))
        existing = cur.fetchone()
        if existing is not None and existing[4] == "completed" and Path(source).exists():
            # A placed file's source has gone. A new file at this path is a new attempt.
            existing = None
        if existing is None:
            cur.execute("""SELECT links.request_id, links.jd2_job_id, links.link_id
                           FROM public.plex_download_links AS links
                           WHERE links.source_path = %s AND links.kind = 'media'
                             AND links.finished
                             AND NOT EXISTS (
                                 SELECT 1 FROM public.plex_download_links AS archive
                                 WHERE archive.request_id = links.request_id AND archive.kind = 'archive'
                             )
                             AND NOT EXISTS (
                                 SELECT 1 FROM public.plex_download_files AS placed
                                 WHERE placed.request_id = links.request_id
                                   AND placed.link_id = links.link_id AND placed.state = 'completed'
                             )""", (source,))
            matches = cur.fetchall()
            match = matches[0] if len(matches) == 1 else (None, None, None)
            cur.execute("""INSERT INTO public.plex_download_files
                (request_id, jd2_job_id, link_id, source_path, source_size, state)
                VALUES (%s, %s, %s, %s, %s, 'queued_for_plex') RETURNING id""",
                (*match, source, source_size))
            file_id = cur.fetchone()[0]
            cur.execute("""INSERT INTO public.plex_download_events (request_id, file_id, state)
                           VALUES (%s, %s, 'queued_for_plex')""", (match[0], file_id))
            existing = (file_id, *match, "queued_for_plex", None)
        file_id, request_id, job_id, link_id, previous, old_destination = existing
        if previous == "completed":
            return {"id": file_id, "request_id": request_id, "completed": True,
                    "destination": old_destination}
        if state != previous or destination != old_destination or error is not None:
            cur.execute("""UPDATE public.plex_download_files SET state = %s,
                destination_path = COALESCE(%s, destination_path),
                source_size = COALESCE(source_size, %s), error = %s,
                outcome = %s, updated_at = CURRENT_TIMESTAMP,
                started_at = CASE WHEN %s = 'processing' THEN COALESCE(started_at, CURRENT_TIMESTAMP)
                                  ELSE started_at END,
                completed_at = CASE WHEN %s = 'completed' THEN CURRENT_TIMESTAMP ELSE NULL END
                WHERE id = %s""", (state, destination, source_size, error, state, state, state, file_id))
            if state != previous:
                cur.execute("""INSERT INTO public.plex_download_events
                    (request_id, file_id, state, detail) VALUES (%s, %s, %s, %s)""",
                    (request_id, file_id, state, error))
        if request_id and state == "completed":
            _maybe_complete(cur, str(request_id))
    return {"id": file_id, "request_id": request_id, "completed": state == "completed",
            "destination": destination or old_destination}


def recover_processing_files():
    """Reconcile only moves whose recorded target can be verified after restart."""
    with _transaction() as cur:
        cur.execute("""SELECT source_path, destination_path, source_size FROM public.plex_download_files
                       WHERE state = 'processing'""")
        pending = cur.fetchall()
    for source, destination, size in pending:
        if not destination:
            continue
        if (Path(destination).is_file() and not Path(source).exists()
                and size is not None and Path(destination).stat().st_size == size):
            record_file_state(source, "completed", destination=destination)
        elif not Path(source).exists():
            record_file_state(source, "failed", error="Source missing and destination could not be verified")


def cleanup_download(request_id: str) -> dict:
    # The live audit did not establish whether removal preserves files.
    # This endpoint intentionally exposes no mutating JD2 operation.
    with _transaction() as cur:
        cur.execute("SELECT id FROM public.plex_download_requests WHERE id = %s", (request_id,))
        if cur.fetchone() is None:
            raise LookupError("Download request not found")
    raise RuntimeError("JD2 link-only removal is disabled pending semantic verification")


if __name__ == '__main__':
    temp = DownloadRequest(
        url="mega.nz/thisisatestfile",
        category="Movies",
        name="Some Test",
    )

    create_crawljob_file(temp)

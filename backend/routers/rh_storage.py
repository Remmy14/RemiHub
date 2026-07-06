# Python Imports
from __future__ import annotations

import os
from datetime import datetime, timezone
import subprocess
from typing import Any

# 3rd Party Imports
import asyncpg
from fastapi import APIRouter, HTTPException

# Local Imports
from backend.config import load_config


router = APIRouter(prefix="/rh-storage", tags=["RH Storage"])


_GB = 1024 ** 3


def _format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc).isoformat()


def _gb(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / _GB, 2)


def _get_rh_storage_database_url() -> str:
    url = os.environ.get("RH_STORAGE_DATABASE_URL")
    if not url:
        cfg = load_config("config/config.ini")
        config = cfg.get("RHStorage", {})
        url = config.get("db_url")
    return url


def _get_systemd_service_status(service_name: str) -> dict[str, Any]:
    """
    Returns basic systemd status for a local service.

    This is intentionally read-only and uses systemctl show so it is safe
    to call from the RemiHub backend.
    """
    try:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                service_name,
                "--property=ActiveState",
                "--property=SubState",
                "--property=LoadState",
                "--property=UnitFileState",
                "--property=ExecMainPID",
                "--property=ActiveEnterTimestamp",
                "--no-pager",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception as exc:
        return {
            "available": False,
            "service_name": service_name,
            "active": False,
            "active_state": "unknown",
            "sub_state": "unknown",
            "load_state": "unknown",
            "unit_file_state": "unknown",
            "pid": None,
            "active_since": None,
            "error": str(exc),
        }

    values: dict[str, str] = {}

    for line in result.stdout.splitlines():
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key] = value

    active_state = values.get("ActiveState", "unknown")
    sub_state = values.get("SubState", "unknown")

    pid_raw = values.get("ExecMainPID")
    pid = None
    if pid_raw and pid_raw.isdigit():
        pid_value = int(pid_raw)
        if pid_value > 0:
            pid = pid_value

    return {
        "available": result.returncode == 0,
        "service_name": service_name,
        "active": active_state == "active",
        "active_state": active_state,
        "sub_state": sub_state,
        "load_state": values.get("LoadState", "unknown"),
        "unit_file_state": values.get("UnitFileState", "unknown"),
        "pid": pid,
        "active_since": values.get("ActiveEnterTimestamp") or None,
        "error": result.stderr.strip() or None,
    }


async def _fetch_rows(conn: asyncpg.Connection, query: str, *args: Any) -> list[dict[str, Any]]:
    rows = await conn.fetch(query, *args)
    return [dict(row) for row in rows]


@router.get("/status")
async def get_rh_storage_status() -> dict[str, Any]:
    """
    Returns a dashboard-friendly RH-Storage status snapshot.

    This intentionally mirrors the terminal status page, but adds enough
    structure for a polished web UI.
    """
    database_url = _get_rh_storage_database_url()

    try:
        conn = await asyncpg.connect(database_url)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not connect to RH-Storage database: {exc}",
        ) from exc

    try:
        pools = await _fetch_rows(
            conn,
            """
            SELECT
                pool_id,
                name,
                mountpoint,
                replication,
                min_free_gb
            FROM pools
            ORDER BY pool_id
            """,
        )

        drift_counts = await _fetch_rows(
            conn,
            """
            SELECT
                pool_id,
                COUNT(*) FILTER (WHERE status = 'needs_repair'::drift_status) AS needs_repair,
                COUNT(*) FILTER (WHERE status = 'repairing'::drift_status) AS repairing,
                COUNT(*) FILTER (WHERE status = 'blocked'::drift_status) AS blocked,
                COUNT(*) AS total
            FROM drift
            GROUP BY pool_id
            """,
        )

        job_counts = await _fetch_rows(
            conn,
            """
            SELECT
                pool_id,
                COUNT(*) FILTER (WHERE status = 'queued'::job_status) AS queued,
                COUNT(*) FILTER (WHERE status = 'running'::job_status) AS running,
                COUNT(*) FILTER (WHERE status = 'succeeded'::job_status) AS succeeded,
                COUNT(*) FILTER (WHERE status = 'failed'::job_status) AS failed,
                COUNT(*) AS total
            FROM jobs
            GROUP BY pool_id
            """,
        )

        branches = await _fetch_rows(
            conn,
            """
            SELECT
                pool_id,
                path,
                online,
                total_bytes,
                free_bytes,
                last_selected_at,
                updated_at
            FROM branches
            ORDER BY pool_id, path
            """,
        )

        recent_jobs = await _fetch_rows(
            conn,
            """
            SELECT
                job_id::text,
                pool_id,
                type::text,
                status::text,
                rel_path,
                attempts,
                last_error AS error,
                created_at,
                updated_at,
                locked_at AS started_at,
                NULL::timestamp with time zone AS finished_at
            FROM jobs
            ORDER BY updated_at DESC
            LIMIT 20
            """,
        )

    finally:
        await conn.close()

    drift_by_pool = {row["pool_id"]: row for row in drift_counts}
    jobs_by_pool = {row["pool_id"]: row for row in job_counts}

    branches_by_pool: dict[str, list[dict[str, Any]]] = {}
    for branch in branches:
        pool_id = branch["pool_id"]

        branch["total_gb"] = _gb(branch.get("total_bytes"))
        branch["free_gb"] = _gb(branch.get("free_bytes"))

        total_bytes = branch.get("total_bytes")
        free_bytes = branch.get("free_bytes")
        if total_bytes and free_bytes is not None and total_bytes > 0:
            used_bytes = total_bytes - free_bytes
            branch["used_gb"] = _gb(used_bytes)
            branch["used_percent"] = round((used_bytes / total_bytes) * 100, 1)
        else:
            branch["used_gb"] = None
            branch["used_percent"] = None

        branch["last_selected_at"] = _format_dt(branch.get("last_selected_at"))
        branch["updated_at"] = _format_dt(branch.get("updated_at"))

        branches_by_pool.setdefault(pool_id, []).append(branch)

    pool_summaries: list[dict[str, Any]] = []

    # Build out Pool details
    for pool in pools:
        pool_id = pool["pool_id"]
        pool_branches = branches_by_pool.get(pool_id, [])

        total_bytes = sum(
            b.get("total_bytes") or 0
            for b in pool_branches
            if b.get("online")
        )
        free_bytes = sum(
            b.get("free_bytes") or 0
            for b in pool_branches
            if b.get("online")
        )
        used_bytes = total_bytes - free_bytes if total_bytes else 0

        pool_summaries.append(
            {
                "pool_id": pool_id,
                "name": pool["name"],
                "mountpoint": pool["mountpoint"],
                "replication": pool["replication"],
                "min_free_gb": pool["min_free_gb"],
                "branch_count": len(pool_branches),
                "online_branch_count": sum(1 for b in pool_branches if b.get("online")),
                "total_gb": _gb(total_bytes),
                "free_gb": _gb(free_bytes),
                "used_gb": _gb(used_bytes),
                "used_percent": round((used_bytes / total_bytes) * 100, 1) if total_bytes else None,
                "drift": {
                    "total": drift_by_pool.get(pool_id, {}).get("total", 0),
                    "needs_repair": drift_by_pool.get(pool_id, {}).get("needs_repair", 0),
                    "repairing": drift_by_pool.get(pool_id, {}).get("repairing", 0),
                    "blocked": drift_by_pool.get(pool_id, {}).get("blocked", 0),
                },
                "jobs": {
                    "total": jobs_by_pool.get(pool_id, {}).get("total", 0),
                    "queued": jobs_by_pool.get(pool_id, {}).get("queued", 0),
                    "running": jobs_by_pool.get(pool_id, {}).get("running", 0),
                    "succeeded": jobs_by_pool.get(pool_id, {}).get("succeeded", 0),
                    "failed": jobs_by_pool.get(pool_id, {}).get("failed", 0),
                },
                "branches": pool_branches,
            }
        )

    # Build out scanner job details
    for job in recent_jobs:
        job["created_at"] = _format_dt(job.get("created_at"))
        job["updated_at"] = _format_dt(job.get("updated_at"))
        job["started_at"] = _format_dt(job.get("started_at"))
        job["finished_at"] = _format_dt(job.get("finished_at"))

    # Get rh_storage systemctl service status
    service_status = _get_systemd_service_status("rh-storage.service")

    return {
        "success": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "service": service_status,
        "pools": pool_summaries,
        "recent_jobs": recent_jobs,
    }

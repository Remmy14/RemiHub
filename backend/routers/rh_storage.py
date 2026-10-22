# Python Imports
from __future__ import annotations

import os
from collections import defaultdict
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

# 3rd Party Imports
import asyncpg
from fastapi import APIRouter, Depends, HTTPException

# Local Imports
from backend.config import load_application_config
from backend.core.auth import require_admin_principal, require_current_principal
from backend.models.rh_storage_worker_models import DefaultWorkersUpdate, WorkerBoostRequest
from backend.services.service_health_service import systemd_status_for_rh_storage_compat


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
        cfg = load_application_config()
        config = cfg.get("RHStorage", {})
        url = config.get("db_url")
    return url


async def _fetch_rows(conn: asyncpg.Connection, query: str, *args: Any) -> list[dict[str, Any]]:
    rows = await conn.fetch(query, *args)
    return [dict(row) for row in rows]


# These endpoints deliberately use the same RH-Storage connection as /status.
# The singleton row is created by RH-Storage, not by RemiHub.
_WORKER_CONFIG_SELECT = """
    SELECT default_workers, boost_workers, boost_until, updated_at,
           CASE WHEN boost_until > NOW() THEN boost_workers
                ELSE default_workers END AS effective_workers,
           NOW() AS server_now
    FROM public.worker_scaling
    WHERE singleton IS TRUE
"""

_WORKER_HEARTBEATS_SELECT = """
    SELECT h.worker_id::text AS worker_id, h.hostname, h.pid, h.state::text AS state,
           h.pool_id::text AS pool_id, h.job_id::text AS job_id,
           h.last_seen_at, h.last_error, h.retiring, h.bytes_copied,
           h.total_bytes, h.job_started_at, h.last_progress_at,
           j.type::text AS job_type, j.rel_path,
           p.name AS pool_name, p.mountpoint AS pool_mountpoint,
           j.payload ->> 'source_branch_id' AS source_branch_id,
           j.payload ->> 'source_branch_path' AS source_branch_path,
           j.target_branch_id::text AS target_branch_id,
           j.payload ->> 'target_branch_path' AS target_branch_path,
           source_branch.path AS source_branch_registered_path,
           target_branch.path AS target_branch_registered_path
    FROM public.worker_heartbeats h
    LEFT JOIN public.jobs j ON j.job_id = h.job_id
    LEFT JOIN public.pools p ON p.pool_id = COALESCE(j.pool_id, h.pool_id)
    LEFT JOIN public.branches source_branch
           ON source_branch.branch_id::text = (j.payload ->> 'source_branch_id')
          AND source_branch.pool_id = j.pool_id
    LEFT JOIN public.branches target_branch
           ON target_branch.branch_id = j.target_branch_id
    WHERE h.last_seen_at >= $1::timestamptz - INTERVAL '45 seconds'
"""


def _worker_unavailable(detail: str = "RH-Storage worker data unavailable") -> HTTPException:
    return HTTPException(status_code=503, detail=detail)


async def _with_worker_connection(
    operation: Callable[[asyncpg.Connection], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    conn = None
    try:
        database_url = _get_rh_storage_database_url()
        if not database_url:
            raise _worker_unavailable()
        conn = await asyncpg.connect(database_url)
        return await operation(conn)
    except HTTPException:
        raise
    except Exception as exc:
        # Do not include connection strings or database error details in API responses.
        raise _worker_unavailable() from exc
    finally:
        if conn is not None:
            with suppress(Exception):
                await conn.close()


def _config_data(row: Any) -> dict[str, Any]:
    if row is None or row["effective_workers"] is None:
        raise _worker_unavailable("RH-Storage worker configuration unavailable")
    data = dict(row)
    for key in ("boost_until", "updated_at", "server_now"):
        data[key] = _format_dt(data[key])
    data["boost_active"] = data["boost_until"] is not None and (
        row["boost_until"] > row["server_now"]
    )
    return data


async def _read_worker_config(conn: asyncpg.Connection) -> dict[str, Any]:
    return _config_data(await conn.fetchrow(_WORKER_CONFIG_SELECT))


def _age_seconds(now: datetime, then: datetime | None) -> float | None:
    if then is None:
        return None
    return round(max(0.0, (now - then).total_seconds()), 1)


def _worker_data(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    data = dict(row)
    for key in ("last_seen_at", "job_started_at", "last_progress_at"):
        data[key] = _format_dt(data[key])
    data["heartbeat_age_seconds"] = _age_seconds(now, row["last_seen_at"])
    data["job_elapsed_seconds"] = _age_seconds(now, row["job_started_at"])
    data["last_progress_age_seconds"] = _age_seconds(now, row["last_progress_at"])
    copied, total = row["bytes_copied"], row["total_bytes"]
    data["progress_percent"] = (
        round(max(0.0, min(100.0, copied * 100 / total)), 1)
        if copied is not None and total is not None and total > 0 else None
    )
    return data


def _worker_monitor_data(config: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.fromisoformat(config["server_now"])
    groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["hostname"] is not None and row["pid"] is not None:
            groups[(row["hostname"], row["pid"])].append(row)

    latest = {key: max(row["last_seen_at"] for row in group) for key, group in groups.items()}
    newest = max(latest.values(), default=None)
    winners = [key for key, last_seen in latest.items() if last_seen == newest]
    ambiguous = len(winners) > 1
    current = winners[0] if len(winners) == 1 else None
    observed = groups[current] if current is not None else []
    workers = [_worker_data(row, now) for row in observed]
    active = [row for row in observed if row["state"] != "stopped"]
    counts = None if ambiguous else {
        "active": len(active),
        "busy": sum(row["state"] == "copying" for row in active),
        "idle": sum(row["state"] == "idle" for row in active),
        "retiring": sum(bool(row["retiring"]) for row in active),
        "healthy": sum(row["state"] in {"idle", "copying", "stopping"} for row in active),
        "unhealthy": sum(row["state"] not in {"idle", "copying", "stopping"} for row in active),
        "observed_gap": max(0, config["effective_workers"] - len(active)),
    }
    return {
        "configuration": config,
        "monitoring_status": "ambiguous" if ambiguous else (
            "available" if current is not None else "no_recent_heartbeats"
        ),
        "observed_daemon_pid": current[1] if current else None,
        "observed_hostname": current[0] if current else None,
        "observed_at": _format_dt(newest) if current else None,
        "counts": counts,
        "workers": workers,
    }


@router.get("/workers/config", dependencies=[Depends(require_current_principal)])
async def get_worker_config() -> dict[str, Any]:
    async def operation(conn: asyncpg.Connection) -> dict[str, Any]:
        return {"success": True, "data": await _read_worker_config(conn)}
    return await _with_worker_connection(operation)


@router.patch("/workers/config", dependencies=[Depends(require_admin_principal)])
async def patch_worker_config(request: DefaultWorkersUpdate) -> dict[str, Any]:
    async def operation(conn: asyncpg.Connection) -> dict[str, Any]:
        row = await conn.fetchrow(
            """WITH changed AS (
                   UPDATE public.worker_scaling
                   SET default_workers = $1, updated_at = NOW()
                   WHERE singleton IS TRUE
                   RETURNING default_workers, boost_workers, boost_until, updated_at
               )
               SELECT *, CASE WHEN boost_until > NOW() THEN boost_workers
                              ELSE default_workers END AS effective_workers,
                      NOW() AS server_now FROM changed""",
            request.default_workers,
        )
        return {"success": True, "data": _config_data(row)}
    return await _with_worker_connection(operation)


@router.post("/workers/boost", dependencies=[Depends(require_admin_principal)])
async def activate_worker_boost(request: WorkerBoostRequest) -> dict[str, Any]:
    async def operation(conn: asyncpg.Connection) -> dict[str, Any]:
        row = await conn.fetchrow(
            """WITH changed AS (
                   UPDATE public.worker_scaling
                   SET boost_workers = $1,
                       boost_until = NOW() + $2::integer * INTERVAL '1 minute',
                       updated_at = NOW()
                   WHERE singleton IS TRUE
                   RETURNING default_workers, boost_workers, boost_until, updated_at
               )
               SELECT *, CASE WHEN boost_until > NOW() THEN boost_workers
                              ELSE default_workers END AS effective_workers,
                      NOW() AS server_now FROM changed""",
            request.workers, request.duration_minutes,
        )
        return {"success": True, "data": _config_data(row)}
    return await _with_worker_connection(operation)


@router.delete("/workers/boost", dependencies=[Depends(require_admin_principal)])
async def clear_worker_boost() -> dict[str, Any]:
    async def operation(conn: asyncpg.Connection) -> dict[str, Any]:
        row = await conn.fetchrow(
            """WITH changed AS (
                   UPDATE public.worker_scaling
                   SET boost_workers = NULL, boost_until = NULL, updated_at = NOW()
                   WHERE singleton IS TRUE
                   RETURNING default_workers, boost_workers, boost_until, updated_at
               )
               SELECT *, CASE WHEN boost_until > NOW() THEN boost_workers
                              ELSE default_workers END AS effective_workers,
                      NOW() AS server_now FROM changed""",
        )
        return {"success": True, "data": _config_data(row)}
    return await _with_worker_connection(operation)


@router.get("/workers", dependencies=[Depends(require_current_principal)])
async def get_workers() -> dict[str, Any]:
    async def operation(conn: asyncpg.Connection) -> dict[str, Any]:
        row = await conn.fetchrow(_WORKER_CONFIG_SELECT)
        config = _config_data(row)
        heartbeats = await _fetch_rows(conn, _WORKER_HEARTBEATS_SELECT, row["server_now"])
        return {"success": True, "data": _worker_monitor_data(config, heartbeats)}
    return await _with_worker_connection(operation)


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
    service_status = systemd_status_for_rh_storage_compat("rh-storage.service")

    return {
        "success": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "service": service_status,
        "pools": pool_summaries,
        "recent_jobs": recent_jobs,
    }

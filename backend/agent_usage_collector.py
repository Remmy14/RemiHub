"""Optional worker-side Codex quota collector. Run with the worker's Codex home.

This entrypoint never runs in the web API process. No model turn is started.
"""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import queue
import signal
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from backend.core.agent_usage import normalize_quota

logger = logging.getLogger("remihub.codex_quota")
SOURCE = "codex_app_server_account_rateLimits_read"


class RateLimits(BaseModel):
    model_config = ConfigDict(extra="allow")
    rateLimits: dict | None = None
    rateLimitsByLimitId: dict | None = None


def _probe(codex_bin: str, output) -> None:
    try:
        # Codex inherits this private process group. The parent can stop the
        # whole probe tree on timeout, including the app-server subprocess.
        os.setsid()

        def _terminate(_signum, _frame):
            raise SystemExit("Codex quota probe timed out")

        signal.signal(signal.SIGTERM, _terminate)
        from openai_codex import CodexConfig
        from openai_codex.client import CodexClient
        client = CodexClient(CodexConfig(codex_bin=codex_bin, cwd="/tmp"))
        try:
            client.start()
            client.initialize()
            account = client.account_read({"refreshToken": False}).model_dump(mode="json")
            if not isinstance(account.get("account"), dict) or account["account"].get("type") != "chatgpt":
                raise ValueError("ChatGPT subscription authentication unavailable")
            response = client.request("account/rateLimits/read",
                                      {"excludeResetCreditDetails": True},
                                      response_model=RateLimits)
            output.put(("available", normalize_quota(response.model_dump(mode="json"))))
        finally:
            client.close()
    except Exception as exc:
        output.put(("collection_failed", type(exc).__name__))


def _stop_probe_tree(process) -> None:
    """Stop the dedicated probe process group and reap the Python probe."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        # The child may have timed out before it established its new group.
        if process.is_alive():
            process.terminate()
    process.join(2)
    try:
        # A surviving app-server can outlive the probe after graceful close.
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.is_alive():
        process.kill()
    process.join(2)


def collect_once(codex_bin: str, *, timeout_seconds: int = 15) -> tuple[str, object]:
    if timeout_seconds < 1 or timeout_seconds > 60:
        raise ValueError("Collector timeout must be between 1 and 60 seconds")
    path = Path(codex_bin)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        return "collection_failed", "CodexBinaryUnavailable"
    ctx = mp.get_context("spawn")
    output = ctx.Queue(maxsize=1)
    process = ctx.Process(target=_probe, args=(str(path), output), daemon=True)
    process.start()
    try:
        return _receive_probe(process, output, timeout_seconds)
    finally:
        output.close()


def _receive_probe(process, output, timeout_seconds: float) -> tuple[str, object]:
    """Drain the pipe before joining; a large valid result can block child exit."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_probe_tree(process)
            return "collection_failed", "Timeout"
        try:
            result = output.get(timeout=min(remaining, 0.1))
            process.join(max(0, deadline - time.monotonic()))
            if process.is_alive():
                _stop_probe_tree(process)
                return "collection_failed", "Timeout"
            return result
        except queue.Empty:
            if not process.is_alive():
                return "collection_failed", "CodexProcessExited"


def persist_snapshot(status: str, value: object) -> None:
    from backend.services.agent_worker_service import get_db_conn, put_db_conn
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO agent.codex_quota_snapshots
                    (slot, source, status, payload, collected_at, error_code)
                VALUES ('worker', %s, %s, %s::jsonb,
                        CASE WHEN %s = 'available' THEN CURRENT_TIMESTAMP ELSE NULL END, %s)
                ON CONFLICT (slot) DO UPDATE SET
                    source = EXCLUDED.source,
                    status = EXCLUDED.status,
                    payload = EXCLUDED.payload,
                    collected_at = CASE WHEN EXCLUDED.status = 'available' THEN CURRENT_TIMESTAMP
                                        ELSE NULL END,
                    last_attempt_at = CURRENT_TIMESTAMP,
                    error_code = EXCLUDED.error_code
            """, (SOURCE, status, json.dumps(value) if status == "available" else None,
                  status, value if status == "collection_failed" else None))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    codex_bin = os.environ.get("REMIHUB_CODEX_BIN", "")
    if not codex_bin:
        raise SystemExit("REMIHUB_CODEX_BIN is required")
    interval = 300
    while True:
        status, value = collect_once(codex_bin)
        try:
            persist_snapshot(status, value)
        except Exception as exc:
            logger.warning("Quota snapshot persistence failed: %s", type(exc).__name__)
        time.sleep(interval)


if __name__ == "__main__":
    main()

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from backend.database.database import get_db_conn, put_db_conn
from backend.notifications.notifications import Notification, insert_notification


logger = logging.getLogger("mead_task_worker")
MEAD_NOTIFICATION_MODULE = "Mead"


def mead_task_notification(task: dict) -> Notification:
    return Notification(
        title=task["title"],
        body=task.get("description") or "A Mead task is due.",
        module=MEAD_NOTIFICATION_MODULE,
        priority=1,
        data={
            "type": "mead_task",
            "action": "view_mead_task",
            "batch_id": str(task["batch_id"]),
            "task_id": str(task["id"]),
            "task_type": str(task["task_type"]),
            "user_id": str(task["user_id"]),
            "due_at": task["due_at"].isoformat()
            if hasattr(task["due_at"], "isoformat")
            else str(task["due_at"]),
        },
    )


def get_due_mead_tasks(conn, *, now: datetime | None = None, limit: int = 25) -> list[dict]:
    current_time = now or datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT task.id,
                   task.batch_id,
                   task.task_type,
                   task.title,
                   task.description,
                   task.due_at,
                   task.status,
                   task.notified_at,
                   task.notified_due_at,
                   task.source,
                   task.source_key,
                   task.metadata,
                   batch.user_id
            FROM public.mead_tasks AS task
            JOIN public.mead_batches AS batch
              ON batch.id = task.batch_id
            WHERE task.status = 'pending'
              AND task.due_at <= %s
              AND (
                  task.notified_due_at IS NULL
                  OR task.notified_due_at <> task.due_at
              )
            ORDER BY task.due_at ASC, task.created_at ASC, task.id ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (current_time, limit),
        )
        columns = [desc[0] for desc in cur.description]
        return [
            {
                column: value
                for column, value in zip(columns, row)
            }
            for row in cur.fetchall()
        ]


def mark_task_notified(conn, *, task_id: str, due_at: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.mead_tasks
            SET notified_at = CURRENT_TIMESTAMP,
                notified_due_at = %s,
                updated_at = now()
            WHERE id = %s
              AND status = 'pending'
              AND due_at = %s
            """,
            (due_at, task_id, due_at),
        )


def process_due_mead_tasks_once(
    *,
    now: datetime | None = None,
    limit: int = 25,
) -> int:
    conn = get_db_conn()
    processed = 0
    try:
        rows = get_due_mead_tasks(conn, now=now, limit=limit)
        for task in rows:
            notice = mead_task_notification(task)
            insert_notification(notice, conn=conn)
            mark_task_notified(conn, task_id=str(task["id"]), due_at=task["due_at"])
            processed += 1
        conn.commit()
        return processed
    except Exception:
        conn.rollback()
        logger.exception("Mead task worker error")
        raise
    finally:
        put_db_conn(conn)


def run_mead_task_worker():
    logger.info("Mead task worker started")
    while True:
        try:
            processed = process_due_mead_tasks_once()
            if processed:
                logger.info("Processed %s due Mead task notifications", processed)
        except Exception:
            logger.exception("Failed to process due Mead tasks")
        time.sleep(60)


if __name__ == "__main__":
    run_mead_task_worker()

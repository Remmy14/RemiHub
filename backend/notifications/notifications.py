# Python Imports
from pydantic import BaseModel, Field
from psycopg2.extras import Json

# 3rd Party Imports

AGENT_NOTIFICATION_MODULE = "Agent"
AGENT_NOTIFICATION_STATUSES = frozenset(
    {
        "awaiting_feedback",
        "awaiting_implementation_approval",
        "review_ready",
        "completed",
    }
)
AGENT_ACTION_BY_STATUS = {
    "awaiting_feedback": "add_follow_up",
    "awaiting_implementation_approval": "approve_implementation",
    "review_ready": "approve_deployment",
    "completed": "view_card",
}


class Notification(BaseModel):
    title: str
    body: str
    module: str
    priority: int = 0
    data: dict[str, str] = Field(default_factory=dict)


def _metadata_changed_files(metadata: dict | None) -> list[str]:
    if not isinstance(metadata, dict):
        return []

    for key in ("candidate", "workspace"):
        nested = metadata.get(key)
        if not isinstance(nested, dict):
            continue
        changed_files = nested.get("changed_files")
        if isinstance(changed_files, list):
            return [value for value in changed_files if isinstance(value, str)]

    changed_files = metadata.get("changed_files")
    if isinstance(changed_files, list):
        return [value for value in changed_files if isinstance(value, str)]
    return []


def agent_status_notification(
    *,
    card_id: str,
    run_id: str,
    card_title: str,
    phase: str,
    status: str,
    metadata: dict | None = None,
) -> Notification | None:
    if status not in AGENT_NOTIFICATION_STATUSES:
        return None

    changed_files = _metadata_changed_files(metadata)
    frontend_build_ready = (
        status == "completed"
        and any(path.startswith("frontend-web/") for path in changed_files)
    )

    if frontend_build_ready:
        title = "Frontend build is ready"
        body = f"A frontend update for {card_title} has been deployed and is ready."
    elif status == "awaiting_feedback":
        title = "Agent needs feedback"
        body = f"{card_title} needs your feedback before the agent can continue."
    elif status == "awaiting_implementation_approval":
        title = "Agent plan is ready"
        body = f"{card_title} is ready for implementation approval."
    elif status == "review_ready":
        title = "Implementation is ready for review"
        body = f"{card_title} is ready for review and deployment approval."
    else:
        title = "Agent task completed"
        body = f"{card_title} has been completed."

    data = {
        "type": "agent_status",
        "card_id": str(card_id),
        "run_id": str(run_id),
        "phase": str(phase),
        "status": str(status),
        "action": AGENT_ACTION_BY_STATUS[status],
    }
    if frontend_build_ready:
        data["frontend_build_ready"] = "true"

    return Notification(
        title=title,
        body=body,
        module=AGENT_NOTIFICATION_MODULE,
        priority=1 if status != "completed" else 0,
        data=data,
    )


def insert_notification(notification: Notification, conn=None):
    new_conn = False
    if not conn:
        # Keep this import lazy so notification construction is safe in tests and
        # other code paths that do not need a database connection.
        from backend.database.database import get_db_conn

        new_conn = True
        conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.notifications (title, body, module, priority, data)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
            """, (
                notification.title,
                notification.body,
                notification.module,
                notification.priority,
                Json(notification.data),
            ))
            row = cur.fetchone()

        if new_conn:
            conn.commit()
        return row[0] if row else None
    except Exception:
        if new_conn:
            conn.rollback()
        raise
    finally:
        if new_conn:
            from backend.database.database import put_db_conn

            put_db_conn(conn)

if __name__ == '__main__':
    # This will allow you to create a Test notification
    print('Creating a Test notification request')
    test_notification = Notification(
        title='Test Notification',
        body='This is a test notification created at {INSERT TIMESTAMP HERE}',
        module='Notification Module',
        priority=1,
    )

    try:
        insert_notification(test_notification)
    except Exception as e:
        print(f'Error creating Test notification: {e}')

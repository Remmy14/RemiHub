# Python Imports
import time
import logging

# 3rd Party Imports
import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

# Local Imports
from backend.core.firebase_auth import get_service_account_path
from backend.database.database import get_db_conn, put_db_conn


# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

logger = logging.getLogger("notification_worker")


FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_URL_TEMPLATE = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

def load_credentials():
    credentials = service_account.Credentials.from_service_account_file(
        str(get_service_account_path()),
        scopes=[FCM_SCOPE]
    )
    return credentials


def get_access_token(credentials):
    credentials.refresh(Request())
    return credentials.token


def normalize_notification_data(data: dict | None) -> dict[str, str]:
    if not data:
        return {}
    return {
        str(key): str(value)
        for key, value in data.items()
        if value is not None
    }


def send_fcm_notification(
    title: str,
    body: str,
    fcm_token: str,
    data: dict | None = None,
):
    credentials = load_credentials()
    access_token = get_access_token(credentials)

    project_id = credentials.project_id
    url = FCM_URL_TEMPLATE.format(project_id=project_id)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "message": {
            "token": fcm_token,
            "notification": {
                "title": title,
                "body": body
            },
            "android": {
                "priority": "HIGH"
            }
        }
    }

    normalized_data = normalize_notification_data(data)
    if normalized_data:
        payload["message"]["data"] = normalized_data

    response = httpx.post(url, headers=headers, json=payload)
    return response


def get_unsent_notifications(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, body, data
            FROM public.notifications
            WHERE sent = FALSE
            ORDER BY created_at ASC
            LIMIT 10;
        """)
        return cur.fetchall()


def get_active_device_tokens(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, device_id, fcm_token, device_name, platform
            FROM public.device_push_tokens
            WHERE is_active = TRUE
            ORDER BY updated_at DESC NULLS LAST, created_at DESC;
        """)
        return cur.fetchall()


def mark_notification_sent(conn, notif_id: int):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE public.notifications
            SET sent = TRUE, sent_at = CURRENT_TIMESTAMP
            WHERE id = %s;
        """, (notif_id,))


def deactivate_device_token(conn, token_id: int):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE public.device_push_tokens
            SET is_active = FALSE,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s;
        """, (token_id,))


def is_unregistered_token_response(resp) -> bool:
    if resp.status_code in (404, 410):
        return True

    try:
        payload = resp.json()
    except Exception:
        return False

    error = payload.get("error", {})
    status = error.get("status", "")
    message = error.get("message", "")

    if status == "NOT_FOUND":
        return True

    if "UNREGISTERED" in message.upper():
        return True

    return False


def process_notification(
    conn,
    notif_id: int,
    title: str,
    body: str,
    data: dict | None = None,
):
    device_rows = get_active_device_tokens(conn)

    if not device_rows:
        logger.info(f"No active device tokens found for notification {notif_id}")
        return

    success_count = 0

    for token_row in device_rows:
        token_id, device_id, fcm_token, device_name, platform = token_row

        try:
            resp = send_fcm_notification(title, body, fcm_token, data=data)
        except Exception as e:
            logger.error(
                f"Error sending notification to device_id={device_id} "
                f"({device_name}, {platform}): {e}"
            )
            continue

        if resp.status_code == 200:
            success_count += 1
            logger.debug(
                f"Notification sent to device_id={device_id} "
                f"({device_name}, {platform})"
            )
            continue

        logger.warning(
            f"Failed to send notification to device_id={device_id} "
            f"({device_name}, {platform}): {resp.status_code} - {resp.text}"
        )

        if is_unregistered_token_response(resp):
            deactivate_device_token(conn, token_id)
            logger.info(
                f"Deactivated stale token for device_id={device_id} "
                f"({device_name}, {platform})"
            )

    if success_count > 0:
        mark_notification_sent(conn, notif_id)
        logger.info(f"Notification marked sent: {title}")
    else:
        logger.warning(f"Notification not marked sent (all sends failed): {title}")


def run_notification_worker():
    logger.info("Notification worker started")

    while True:
        conn = get_db_conn()
        try:
            rows = get_unsent_notifications(conn)

            if rows:
                logger.debug(f"Processing {len(rows)} pending notifications")

            for row in rows:
                notif_id, title, body, data = row
                process_notification(conn, notif_id, title, body, data=data)

            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Notification worker error: {e}")
        finally:
            put_db_conn(conn)

        time.sleep(10)


if __name__ == "__main__":
    run_notification_worker()

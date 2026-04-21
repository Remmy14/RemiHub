# Python Imports
import requests
import time

# 3rd Party Imports
import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

# Local Imports
from backend.database.database import get_db_conn, put_db_conn
from backend import config


FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_URL_TEMPLATE = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

# Path to your service account file
SERVICE_ACCOUNT_FILE = "config/firebase-service-account.json"

def load_credentials():
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=[FCM_SCOPE]
    )
    return credentials

def get_access_token(credentials):
    credentials.refresh(Request())
    return credentials.token

def send_fcm_notification(title: str, body: str, fcm_token: str):
    credentials = load_credentials()
    access_token = get_access_token(credentials)

    # Get project ID from the creds
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

    response = httpx.post(url, headers=headers, json=payload)
    return response

def get_unsent_notifications(conn):
    with conn.cursor() as cur:
        cur.execute(
            '''
            SELECT id, title, body
            FROM notifications
            WHERE sent = FALSE
            ORDER BY created_at ASC
            LIMIT 10;
            '''
        )
        return cur.fetchall()

def get_active_device_tokens(conn):
    with conn.cursor() as cur:
        cur.execute(
            '''
            SELECT id, device_id, fcm_token, device_name, platform
            FROM device_push_tokens
            WHERE is_active = TRUE
            ORDER BY updated_at DESC NULLS LAST, created_at DESC;
            '''
        )
        return cur.fetchall()



def mark_notification_sent(conn, notif_id: int):
    with conn.cursor() as cur:
        cur.execute(
            '''
            UPDATE notifications
            SET sent = TRUE, sent_at = CURRENT_TIMESTAMP
            WHERE id = %s;
            ''',
            (notif_id,),
        )


def deactivate_device_token(conn, token_id: int):
    with conn.cursor() as cur:
        cur.execute(
            '''
            UPDATE device_push_tokens
            SET is_active = FALSE,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s;
            ''',
            (token_id,),
        )


def is_unregistered_token_response(resp) -> bool:
    if resp.status_code in (404, 410):
        return True

    try:
        payload = resp.json()
    except Exception:
        return False

    error = payload.get('error', {})
    status = error.get('status', '')
    message = error.get('message', '')

    if status == 'NOT_FOUND':
        return True

    message_upper = message.upper()
    if 'UNREGISTERED' in message_upper:
        return True

    return False


def process_notification(conn, notif_id: int, title: str, body: str):
    device_rows = get_active_device_tokens(conn)

    if not device_rows:
        print(f'No active device tokens found for notification {notif_id}')
        return

    success_count = 0

    for token_row in device_rows:
        token_id, device_id, fcm_token, device_name, platform = token_row

        try:
            resp = send_fcm_notification(title, body, fcm_token)
        except Exception as e:
            print(
                f'Error sending notification to device_id={device_id} '
                f'({device_name}, {platform}): {e}'
            )
            continue

        if resp.status_code == 200:
            success_count += 1
            print(
                f'Notification sent to device_id={device_id} '
                f'({device_name}, {platform})'
            )
            continue

        print(
            f'Failed to send notification to device_id={device_id} '
            f'({device_name}, {platform}): {resp.status_code} - {resp.text}'
        )

        if is_unregistered_token_response(resp):
            deactivate_device_token(conn, token_id)
            print(
                f'Deactivated stale token for device_id={device_id} '
                f'({device_name}, {platform})'
            )

    if success_count > 0:
        mark_notification_sent(conn, notif_id)
        print(f'Notification marked sent: {title}')
    else:
        print(f'Notification not marked sent because all sends failed: {title}')


def run_notification_worker():
    print('Notification worker started')

    while True:
        conn = get_db_conn()
        try:
            rows = get_unsent_notifications(conn)

            for row in rows:
                notif_id, title, body = row
                process_notification(conn, notif_id, title, body)

            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f'⚠️ Notification worker error: {e}')
        finally:
            put_db_conn(conn)

        time.sleep(10)

if __name__ == '__main__':
    run_notification_worker()

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


def run_notification_worker():
    print("Notification worker started")

    # TODO: Replace with real token logic
    _config = config.load_config('config/config.ini')
    fcm_token = _config['Notifications']['fcm_token']

    while True:
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:

                # Pull all new notification requests from the database
                cur.execute("""
                    SELECT id, title, body FROM notifications
                    WHERE sent = FALSE
                    ORDER BY created_at ASC
                    LIMIT 10;
                """)
                rows = cur.fetchall()

                # Parse through all new notifications (realistically only 1 at a time)
                for row in rows:
                    notif_id, title, body = row

                    # Send the notification request
                    resp = send_fcm_notification(title, body, fcm_token)

                    # Handle notification status and update the database
                    if resp.status_code == 200:
                        cur.execute("""
                            UPDATE notifications
                            SET sent = TRUE, sent_at = CURRENT_TIMESTAMP
                            WHERE id = %s;
                        """, (notif_id,))
                        print(f"Notification sent: {title}")
                    else:
                        print(f"Failed to send notification: {resp.status_code} - {resp.text}")

            conn.commit()
        except Exception as e:
            print(f"⚠️ Notification worker error: {e}")
        finally:
            put_db_conn(conn)

        time.sleep(10)


if __name__ == '__main__':
    run_notification_worker()

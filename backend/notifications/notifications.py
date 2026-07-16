# Python Imports
from pydantic import BaseModel, Field
from psycopg2.extras import Json

# 3rd Party Imports

# Local Imports
from backend.database.database import get_db_conn, put_db_conn


class Notification(BaseModel):
    title: str
    body: str
    module: str
    priority: int = 0
    data: dict[str, str] = Field(default_factory=dict)

def insert_notification(notification: Notification, conn=None):
    new_conn = False
    if not conn:
        new_conn = True
        conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.notifications (title, body, module, priority, data)
                VALUES (%s, %s, %s, %s, %s);
            """, (
                notification.title,
                notification.body,
                notification.module,
                notification.priority,
                Json(notification.data),
            ))

        conn.commit()
    finally:
        if new_conn:
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

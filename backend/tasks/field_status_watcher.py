# Python Imports
from datetime import date, datetime
import logging
import re
import time
from logging.handlers import RotatingFileHandler

# 3rd Party Imports
from bs4 import BeautifulSoup
import requests
from typing import Optional, Tuple

# Local Imports
from backend.database.database import get_db_conn, put_db_conn
from backend.notifications.notifications import insert_notification, Notification

FIELD_STATUS_URL = "https://lakotasports.org/facility_status.php"


# ----------------------
# Configure Logging
# ----------------------
logger = logging.getLogger('FieldStatusMonitor')
logger.setLevel(logging.INFO)

log_handler = RotatingFileHandler('backend/logs/field_status_monitor.log', maxBytes=1_000_000, backupCount=3)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
log_handler.setFormatter(formatter)
logger.addHandler(log_handler)


def extract_updated_timestamp(soup: BeautifulSoup) -> Tuple[Optional[datetime], Optional[date]]:
    heading = soup.find('h5', class_='heading-component-title')
    if not heading:
        return None, None

    text = heading.get_text(strip=True)
    match = re.search(r'updated\s+(.*\d{4}\s*\d{1,2}:\d{2}\s*[ap]m)', text, re.IGNORECASE)
    if not match:
        return None, None

    try:
        # Parse full datetime
        dt = datetime.strptime(match.group(1), '%B %d, %Y %I:%M %p')
        return dt, dt.date()
    except Exception as e:
        logger.error(f'Error: {e}')
        return None, None

def scrape_field_statuses():
    response = requests.get(FIELD_STATUS_URL)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    # Get today's date
    today = date.today()

    # Get our most recent udpated time
    last_seen = get_last_updated_timestamp_from_db()

    # Get the updated time from the soup
    updated_time, updated_date = extract_updated_timestamp(soup)

    # Check to see if we should even proceed
    # 1. Was "last_seen" successfully found?
    if not last_seen:
        logger.error('Last_Seen time not set')
        return
    # 2. Is the update from today?
    elif updated_date < today:
        # print('Status not yet updated today')
        return
    # 3. Is the updated_time a newer one than we've stored?
    elif updated_time <= last_seen:
        # print('No update detected, skipping notification.')
        return

    logger.info(f'New update found. {updated_time}')

    # We have a new latest update time, save it for later
    set_last_updated_timestamp_in_db(updated_time)

    # Begin parsing the fields
    field_statuses = []
    for row in soup.select('div.row.mt-2'):
        try:
            badge = row.select_one('.badge')
            if not badge:
                continue  # skip rows without a badge

            status = badge.text.strip()
            status = 'Closed' if 'closed' in status.lower() else 'Open'

            # look for the first .col-auto after the badge
            col_autos = row.select('.col-auto')
            if not col_autos:
                continue

            # Grab just the first non-empty field name
            field_name = col_autos[0].get_text(strip=True)
            if not field_name:
                continue

            field_statuses.append({
                'field_name': field_name,
                'status': status
            })

        except Exception as e:
            print(f'Error parsing row: {e}')

    logger.info(f'Field Statuses Found:')
    logger.info(status for status in field_statuses)
    return field_statuses

def check_field_statuses_and_notify():
    today = date.today()
    field_statuses = scrape_field_statuses()

    # If there is no update this returns blank
    if not field_statuses:
        return

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            # Get fields being monitored for today
            cur.execute("""
                SELECT id, field_name
                FROM field_watch
                WHERE target_date = %s
            """, (today,))
            watched_fields = cur.fetchall()

            for watch_id, watched_field in watched_fields:
                for field_status in field_statuses:
                    if field_status['field_name'].strip().lower() == watched_field.strip().lower():
                        current_status = field_status['status']
                        previous_status = get_today_logged_status(watch_id, today)

                        if previous_status != current_status:
                            insert_or_update_field_log(watch_id, today, current_status)
                            print(f"Field {watched_field} is now {current_status} (was {previous_status}) — Notify!")

                            notice = Notification(
                                title="Field Status Update!",
                                body=f"{watched_field} has been marked as {current_status} on {today.strftime('%B %d')}",
                                module='Field Watcher',
                            )

                            logger.info(f'Sending notification for {notice.title} - {notice.body}')
                            insert_notification(notice)
                        break

    except Exception as e:
        logger.error(f'Error in module: {e}')
    finally:
        put_db_conn(conn)

# Data base helpers ===========================================
def get_last_updated_timestamp_from_db():
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT last_updated FROM field_watch_meta WHERE id = 1")
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        put_db_conn(conn)


def set_last_updated_timestamp_in_db(new_timestamp: datetime):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE field_watch_meta SET last_updated = %s WHERE id = 1
            """, (new_timestamp,))
            conn.commit()
    finally:
        put_db_conn(conn)


def get_today_logged_status(watch_id: int, today: date) -> str | None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT status FROM field_watch_log
                WHERE watch_id = %s AND logged_date = %s
            """, (watch_id, today))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        put_db_conn(conn)


def insert_or_update_field_log(watch_id: int, today: date, status: str):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO field_watch_log (watch_id, logged_date, status)
                VALUES (%s, %s, %s)
                ON CONFLICT (watch_id, logged_date)
                DO UPDATE SET status = EXCLUDED.status
            """, (watch_id, today, status))
            conn.commit()
    finally:
        put_db_conn(conn)


# Monitor main entry point. This will kick the monitor off once a minute
def run_monitor():
    while True:
        try:
            check_field_statuses_and_notify()
        except Exception as e:
            print(f'Error: {e}')

        time.sleep(60)
    

if __name__ == '__main__':
    run_monitor()

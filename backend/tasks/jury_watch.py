# Python Imports
import datetime
from datetime import date, datetime
import logging
import re
import time
from logging.handlers import RotatingFileHandler

# 3rd Party Imports
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Local Imports
from backend.database.database import get_db_conn, put_db_conn
from backend.notifications.notifications import insert_notification, Notification


JURY_STATUS_URL = "https://jury.bcohio.gov/"
BADGE_NUMBER = '310703'
BIRTH_DATE = '11/08/1988'
MESSAGE_TEXT = 'Check this message  every day after 3:30 PM during your term of service(beginning the Friday prior) to determine if you are required to report for a trial.  This message will change and clearly state if you are to report the next business day. Please do NOT report unless this message tells you to.'

SCRAPE_DATE_START = date(year=2025, month=8, day=1)
SCRAPE_DATE_END = date(year=2025, month=8, day=14)


# ----------------------
# Configure Logging
# ----------------------
logger = logging.getLogger('JuryStatusMonitor')
logger.setLevel(logging.INFO)

log_handler = RotatingFileHandler('backend/logs/jury_status_monitor.log', maxBytes=1_000_000, backupCount=3)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
log_handler.setFormatter(formatter)
logger.addHandler(log_handler)


def scrape_status():
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    service = Service("M:/Q_Drive/Projects/drivers/chromedriver.exe")
    driver = webdriver.Chrome(service=service, options=options)

    try:
        logger.info('Loading Juror page...')
        driver.get(JURY_STATUS_URL)
        wait = WebDriverWait(driver, 60)

        logger.info('Attempting login...')
        badge_input = wait.until(EC.presence_of_element_located((By.ID, 'badgeNumber')))
        birthdate_input = driver.find_element(By.ID, 'birthDay')
        login_button = driver.find_element(By.XPATH, '//button[text()="Sign In"]')

        badge_input.send_keys(BADGE_NUMBER)
        birthdate_input.send_keys(BIRTH_DATE)
        login_button.click()

        logger.info('Waiting for Juror instructions...')

        status_div = wait.until(EC.presence_of_element_located((By.ID, 'contentStatus')))
        message_text = status_div.text

        if message_text:
            return message_text
        else:
            return None

    except:
        return None


def check_jury_status_and_notify():
    status_message = scrape_status()

    # If there is no update this returns blank
    if not status_message:
        # This returns without setting our DB check, so it will automatically retry
        return

    # Check to see if the message is telling us to report
    if status_message != MESSAGE_TEXT:
        # The message has changed, we may need to report
        notice = Notification(
            title="Jury Status Update!",
            body=f"Jury Status has been updated:\n{status_message}",
            module='Jury Watcher',
        )
    else:
        status_message = 'No update...'
        notice = Notification(
            title="Jury Status Update!",
            body=f"It is after 3:30PM and the jury status has not updated...",
            module='Jury Watcher',
        )

    logger.info(f'Sending notification for {notice.title} - {notice.body}')
    insert_notification(notice)

    # We have done our checks/notifications, update the db
    update_todays_check(status_message)


def update_todays_check(status):
    conn = get_db_conn()
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE jury_watch
                SET last_checked = %s,
                    last_result = %s
                WHERE id = 1;
            """, (datetime.now(), status))

        conn.commit()
    except Exception as e:
        print(f'Error in module: {e}')
    finally:
        put_db_conn(conn)


def already_checked_today():
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT last_checked FROM jury_watch WHERE id = 1;")
            result = cur.fetchone()
            if result and result[0]:
                last_checked_date = result[0].date()

                # If we already checked today, return True, else False
                return last_checked_date == date.today()

        conn.commit()
    except Exception as e:
        print(f'Error in module: {e}')
    finally:
        put_db_conn(conn)


# Monitor main entry point. This will kick the monitor off once a minute
def run_monitor():
    while True:
        # Get today's date
        today = date.today()
        now = datetime.now()

        SCRAPE_WINDOW_START = datetime(year=2025, month=8, day=today.day, hour=15, minute=35)
        SCRAPE_WINDOW_END = datetime(year=2025, month=8, day=today.day, hour=16, minute=0)

        # Are we inside our window?
        if today < SCRAPE_DATE_START or today > SCRAPE_DATE_END:
            logger.info("We're not ready to start scraping...")
            time.sleep(43200)
            continue

        # Have we already scraped today?
        if already_checked_today():
            time.sleep(1800)
            continue

        # Are we nearing the time we need to scrape? (IE approaching 3PM)
        if now < SCRAPE_WINDOW_START or now > SCRAPE_WINDOW_END:
            time.sleep(60)
            continue

        # We are inside our scrape window, let's check the status
        try:
            logger.info('Getting our daily juror status')
            check_jury_status_and_notify()
        except Exception as e:
            logger.error(f'Error: {e}')

        time.sleep(60)


if __name__ == '__main__':
    run_monitor()

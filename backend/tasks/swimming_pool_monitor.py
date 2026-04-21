# Python Imports
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import time

# 3rd Party Imports
from playwright.sync_api import sync_playwright

# Local Imports
from backend.database.database import get_db_conn, put_db_conn
from backend.database import pool_watch_meta
from backend import config

# ----------------------
# Configure Logging
# ----------------------
logger = logging.getLogger('PoolMonitor')
logger.setLevel(logging.INFO)

log_handler = RotatingFileHandler('backend/logs/pool_monitor.log', maxBytes=1_000_000, backupCount=3)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
log_handler.setFormatter(formatter)
logger.addHandler(log_handler)

# ----------------------
# Save to Database
# ----------------------
def save_data_to_database(pool_data):
    inlet = pool_data['inlet']
    outlet = pool_data['outlet']
    outdoor = pool_data['outdoor']
    set_temp = pool_data['set']

    sql = '''
        INSERT INTO pool_temperature_log (inlet_temp_f, outlet_temp_f, outdoor_air_temp_f, set_temp_f)
        VALUES (%s, %s, %s, %s)
    '''
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (inlet, outlet, outdoor, set_temp))
        conn.commit()
        logger.info(f"Saved data to database: inlet={inlet}, outlet={outlet}, outdoor={outdoor}, set={set}")
    except Exception as e:
        logger.exception(f"Error saving to database: {e}")
    finally:
        put_db_conn(conn)

# ----------------------
# Scrape Data From Raymote
# ----------------------
def fetch_raymote_temperatures(_config):
    url = _config['url']
    email = _config['email']
    password = _config['password']

    with sync_playwright() as p:
        browser = p.chromium.launch() #headless=True)
        context = browser.new_context(viewport={'width': 1920, 'height': 1080})
        page = context.new_page()

        page.goto(url, wait_until='domcontentloaded')

        # Login
        page.wait_for_selector('#email', timeout=60_000)
        page.fill('#email', email)
        page.fill('#password', password)

        # Your Selenium used a specific button xpath. In Playwright, prefer a role/text locator:
        page.get_by_role('button', name='Log In').click()

        # Wait for dashboard widgets
        inlet_sel = '#WEB_LABEL2 span.widgets--widget-web-label--value'
        outlet_sel = '#WEB_LABEL4 span.widgets--widget-web-label--value'
        outdoor_sel = '#WEB_LABEL1 span.widgets--widget-web-label--value'
        set_sel = '#WEB_LABEL35 span.widgets--widget-web-label--value'

        page.wait_for_selector(inlet_sel, timeout=60_000)

        inlet_text = page.locator(inlet_sel).inner_text().strip()
        outlet_text = page.locator(outlet_sel).inner_text().strip()
        outdoor_text = page.locator(outdoor_sel).inner_text().strip()
        set_text = page.locator(set_sel).inner_text().strip()

        inlet_temp = float(inlet_text)
        outlet_temp = float(outlet_text)
        outdoor_temp = float(outdoor_text)

        set_temp = float(set_text) if set_text.replace('.', '', 1).isdigit() else None

        context.close()
        browser.close()

        return {
            'inlet': inlet_temp,
            'outlet': outlet_temp,
            'outdoor': outdoor_temp,
            'set': set_temp,
        }


# ----------------------
# Main Loop
# ----------------------
def run_pool_monitor(_config=None):
    if not _config:
        _config = config.load_config('config/config.ini')

    last_run_minute = None

    logger.info("Pool monitor started.")
    while True:
        summer_mode = pool_watch_meta.get_summer_mode()
        if not summer_mode:
            logger.info('Pool monitor is disabled (winter mode). Skipping scrape.')
            time.sleep(60)
            continue

        now = datetime.now()
        if now.minute in [0, 15, 30, 45] and now.minute != last_run_minute:
            print('Beginning to scrape pool temps')
            try:
                temps = fetch_raymote_temperatures(_config['Pool Monitor'])
                save_data_to_database(temps)
                last_run_minute = now.minute
            except Exception as e:
                logger.error(f"Error during scheduled run: {e}")
        else:
            print('Not time to scrape pool temps')
            time.sleep(60)

# ----------------------
# Entry Point
# ----------------------
if __name__ == '__main__':
    _config = config.load_config('config/config.ini')
    # run_pool_monitor(_config['Pool Monitor'])

    temps = fetch_raymote_temperatures(_config['Pool Monitor'])
    # save_data_to_database(temps)
    print(temps)

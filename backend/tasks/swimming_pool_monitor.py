# Python Imports
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import time

# 3rd Party Imports
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Local Imports
from backend.database.database import get_db_conn, put_db_conn
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

    options = Options()
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    service = Service("M:/Q_Drive/Projects/drivers/chromedriver.exe")
    driver = webdriver.Chrome(service=service, options=options)

    try:
        logger.info('Loading Raymote page...')
        driver.get(url)
        wait = WebDriverWait(driver, 60)

        logger.info('Attempting login...')
        email_input = wait.until(EC.presence_of_element_located((By.ID, 'email')))
        password_input = driver.find_element(By.ID, 'password')
        login_button = driver.find_element(By.XPATH, '//button[span[text()="Log In"]]')

        email_input.send_keys(_config['email'])
        password_input.send_keys(_config['password'])
        login_button.click()

        logger.info('Waiting for dashboard...')
        inlet_elem = wait.until(EC.presence_of_element_located(
            (By.XPATH, '//div[@id="WEB_LABEL2"]//span[contains(@class, "widgets--widget-web-label--value")]')
        ))
        outlet_elem = wait.until(EC.presence_of_element_located(
            (By.XPATH, '//div[@id="WEB_LABEL4"]//span[contains(@class, "widgets--widget-web-label--value")]')
        ))
        outdoor_elem = wait.until(EC.presence_of_element_located(
            (By.XPATH, '//div[@id="WEB_LABEL1"]//span[contains(@class, "widgets--widget-web-label--value")]')
        ))
        set_elem = wait.until(EC.presence_of_element_located(
            (By.XPATH, '//div[@id="WEB_LABEL35"]//span[contains(@class, "widgets--widget-web-label--value")]')
        ))

        inlet_temp = float(inlet_elem.text)
        outlet_temp = float(outlet_elem.text)
        outdoor_temp = float(outdoor_elem.text)
        set_temp = set_elem.text

        if set_temp.isnumeric():
            set_temp = float(set_temp)
        else:
            set_temp = None

        logger.info('Temperatures fetched successfully.')
        return {
            'inlet': inlet_temp,
            'outlet': outlet_temp,
            'outdoor': outdoor_temp,
            'set': set_temp,
        }

    except Exception as e:
        logger.exception(f"Error fetching Raymote temperatures: {e}")
        raise
    finally:
        driver.quit()

# ----------------------
# Main Loop
# ----------------------
def run_pool_monitor(_config=None):
    if not _config:
        _config = config.load_config('config/config.ini')

    last_run_minute = None

    logger.info("Pool monitor started.")
    while True:
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
    save_data_to_database(temps)

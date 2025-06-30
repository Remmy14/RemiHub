# Python Imports
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
import re
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
import time

# Local imports
from backend.database.database import get_db_conn, put_db_conn
from backend.pool import load_pool_from_db

options = Options()
options.add_argument("--headless")
options.add_argument("--disable-logging")
options.add_argument("--log-level=3")

OFFLINE = True

_executor = ThreadPoolExecutor(max_workers=1)

def should_fetch_leaderboard():
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT event_status FROM indy_pool_draft_status LIMIT 1")
            status = cur.fetchone()[0]
            return status == 'RACE_ACTIVE'
    except:
        return False
    finally:
        put_db_conn(conn)

async def _get_leaderboard_data():
    return await asyncio.get_event_loop().run_in_executor(
        executor=_executor,
        func=_get_leaderboard_data_blocking,
    )

def _get_leaderboard_data_blocking():
    service = Service("M:/Q_Drive/Projects/drivers/chromedriver.exe")
    driver = webdriver.Chrome(service=service, options=options)
    if OFFLINE:
        # We're offline, load the cached version
        path = os.path.abspath("cleaned_leaderboard.html")
        driver.get(f"file:///{path}")
    else:
        driver.get("https://proud-island-0d704c910.4.azurestaticapps.net/")

        # Sleep to allow javascript to load
        time.sleep(5)

        # Save the rendered HTML to a local file
        with open("cached_leaderboard_test.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)

    leaderboard = []
    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")

    for row in rows:
        try:
            # First get the car number
            # Find the <img> inside the driver cell (adjust if needed)
            img = row.find_element(By.CSS_SELECTOR, "td:nth-child(2) img")
            img_src = img.get_attribute("src")

            # Extract number from URL like ".../45-RedGreyTrim-T.png"
            match = re.search(r"/(\d+)-[^/]*\.png", img_src)
            number = match.group(1) if match else "UNKNOWN"

            position = row.find_element(By.CSS_SELECTOR, "td:nth-child(1) div").text.strip()
            lap = row.find_element(By.CSS_SELECTOR, "td:nth-child(15) div").text.strip()
            speed = row.find_element(By.CSS_SELECTOR, "td:nth-child(6) div").text.strip()
            status = row.find_element(By.CSS_SELECTOR, "td:nth-child(3) div").text.strip()

            if not position:
                continue

            # Add the driver to our list
            leaderboard.append({
                "position": int(position),
                "laps": int(lap) if lap.isdigit() else 0,
                "speed": float(speed) if speed.replace('.', '', 1).isdigit() else 0.0,
                "number": number,
                "status": status,
            })

        except Exception as e:
            # Suppress expected errors from empty/incomplete rows
            continue

    driver.quit()
    return leaderboard

async def save_leaderboard_to_db():
    # Step 1 - get the latest leaderboard data from web scrape
    leaderboard_data = await _get_leaderboard_data()

    # Step 2 - Put the data into the db
    conn = get_db_conn()
    table_name = 'indy_pool_leaderboard'
    try:
        with conn.cursor() as cur:
            # Step 1: Clear existing leaderboard
            cur.execute(f"DELETE FROM {table_name}")

            # Step 2: Insert new rows
            for row in leaderboard_data:
                cur.execute(f"""
                    INSERT INTO {table_name} (
                        car_number,
                        position,
                        status,
                        laps_completed,
                        updated_at
                    ) VALUES (%s, %s, %s, %s, %s)
                """, (
                    row['number'],
                    row['position'],
                    row.get('status', 'Unknown'),
                    row.get('laps', 0),
                    datetime.now()
                ))

        conn.commit()
    finally:
        put_db_conn(conn)

def load_leaderboard_from_db() -> list[dict]:
    conn = get_db_conn()
    table_name = 'indy_pool_leaderboard'
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT car_number, position, status, laps_completed, updated_at
                FROM {table_name}
                ORDER BY position ASC
            """)
            rows = cur.fetchall()

            return [
                {
                    'number': row[0],
                    'position': row[1],
                    'status': row[2],
                    'laps_completed': row[3],
                    'updated_at': row[4].isoformat()
                }
                for row in rows
            ]
    finally:
        put_db_conn(conn)

def generate_pool_standings_json(pool_id: int, ) -> list[dict]:
    # Update our pool standings
    # Load the cached "pool" data (ie. names mapped to drivers)
    pool = load_pool_from_db(pool_id)

    # Load the latest cache of leaderboard data
    leaderboard = load_leaderboard_from_db()

    # Build a mapping from driver name to their position
    driver_map = {}
    for entry in leaderboard:
        number = entry.get("number", '')
        if number:
            driver_map[number] = {
                "position": entry["position"],
            }

    standings = []

    # Marry the leaderboard to the pool to get the standings
    for name, drivers in pool.items():
        participant_drivers = []
        positions = []

        # Pull out the drivers that are specific to this person
        for driver in drivers:
            car_number = driver["number"]
            if car_number in driver_map:
                entry = driver_map[car_number]
                positions.append(entry["position"])
                participant_drivers.append({
                    "name": driver["name"],  # leaderboard format
                    "number": car_number,
                    "position": entry["position"]
                })

        # Sort those drivers so they appear nice and neat and orderly
        participant_drivers.sort(key=lambda x: x['position'])

        if positions:
            avg = sum(positions) / len(positions)
            standings.append({
                "name": name,
                "drivers": participant_drivers,
                "average_position": round(avg, 2)
            })

    # Sort the standings by average position
    standings.sort(key=lambda x: x["average_position"])

    return standings

def save_pool_standings_to_db(pool_id: int, ):
    standings = generate_pool_standings_json(pool_id)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM indy_pool_standings_cache WHERE pool_id=%s;", (pool_id, ))  # Replace previous cache
            cur.execute("""
                INSERT INTO indy_pool_standings_cache (updated_at, standings_json, pool_id)
                VALUES (%s, %s, %s)
            """, (
                datetime.now(),
                json.dumps(standings),
                pool_id,
            ))
        conn.commit()
    finally:
        put_db_conn(conn)

def load_pool_standings_from_db(pool_id: int):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT standings_json, updated_at
                FROM indy_pool_standings_cache
                WHERE pool_id=%s
                ORDER BY updated_at DESC
                LIMIT 1
            """, (
                pool_id,
            ))
            row = cur.fetchone()
            if row:
                standings_json, updated_at = row
                return standings_json, updated_at.isoformat()
            return None, None
    finally:
        put_db_conn(conn)


if __name__ == '__main__':
    print(generate_pool_standings_json())

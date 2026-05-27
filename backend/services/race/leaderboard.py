# Python Imports
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
import re
from playwright.sync_api import sync_playwright

# Local imports
from backend.database.database import get_db_conn, put_db_conn
from backend.services.race.pool import load_pool_from_db

OFFLINE = False
_executor = ThreadPoolExecutor(max_workers=1)

# Browser caching
_playwright = None
_browser = None
_page = None
_browser_created_at = None
_BROWSER_MAX_AGE_SECONDS = 600  # 10 minutes


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

def _get_or_create_page():
    global _playwright, _browser, _page, _browser_created_at

    now = datetime.now().timestamp()

    # Recycle old browser
    if (
        _browser_created_at is not None
        and (now - _browser_created_at) > _BROWSER_MAX_AGE_SECONDS
    ):
        print("Recycling Playwright browser due to age")
        _reset_browser()

    if _page is not None:
        return _page

    print("Launching new Playwright browser")

    _playwright = sync_playwright().start()

    _browser = _playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    _page = _browser.new_page()
    _browser_created_at = now

    return _page

def _reset_browser():
    global _playwright, _browser, _page

    try:
        if _page:
            _page.close()
    except:
        pass

    try:
        if _browser:
            _browser.close()
    except:
        pass

    try:
        if _playwright:
            _playwright.stop()
    except:
        pass

    _playwright = None
    _browser = None
    _page = None
    _browser_created_at = None

async def _get_leaderboard_data():
    return await asyncio.get_event_loop().run_in_executor(
        executor=_executor,
        func=_get_leaderboard_data_blocking_with_recovery,
    )

def _get_leaderboard_data_blocking_with_recovery():
    try:
        return _get_leaderboard_data_blocking()
    except Exception as e:
        print(f"Exception getting leaderboard data: {e}")
        _reset_browser()
        return []

def _get_leaderboard_data_blocking():
    url = "https://proud-island-0d704c910.4.azurestaticapps.net/"
    leaderboard = []

    page = _get_or_create_page()

    page.goto(
        url=url,
        wait_until="domcontentloaded",
        timeout=15000,
    )

    page.wait_for_selector("table tbody tr img", state="attached", timeout=30000)
    rows = page.locator("table tbody tr")

    for i in range(rows.count()):
        row = rows.nth(i)
        if not row.is_visible():
            continue

        try:
            img_src = row.locator("td:nth-child(2) img").get_attribute("src") or ""
            match = re.search(r"/(\d+)-[^/]*\.png", img_src)
            number = match.group(1) if match else "UNKNOWN"

            position_cell = row.locator("td:nth-child(1)").inner_text().strip()
            lap_text = row.locator("td:nth-child(15)").inner_text().strip()
            speed_text = row.locator("td:nth-child(6)").inner_text().strip()
            status = row.locator("td:nth-child(3)").inner_text().strip()

            position_match = re.search(r"\d+", position_cell)
            if not position_match:
                continue

            position_text = position_match.group(0)

            leaderboard.append({
                "position": int(position_text),
                "laps": int(lap_text) if lap_text.isdigit() else 0,
                "speed": float(speed_text) if speed_text.replace(".", "", 1).isdigit() else 0.0,
                "number": number,
                "status": status,
            })

        except Exception as e:
            print(f"Error in table loop: {e}")
            continue
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

async def get_leaderboard():
    return await _get_leaderboard_data()

if __name__ == "__main__":
    import time
    num = 0
    while num < 5:
        num += 1
        print(f"starting loop {num}")
        start = time.time()
        data = asyncio.run(_get_leaderboard_data())
        end = time.time()
        delta = end - start

        print(f"Data: {data}")
        print(f"That took {delta} seconds")

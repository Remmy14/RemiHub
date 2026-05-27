import json
import logging
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

# Local Imports
from backend.database.database import get_db_conn, put_db_conn

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "speed_test.log"

logger = logging.getLogger("speed_test")
logger.setLevel(logging.INFO)

if not logger.handlers:
    file_handler = logging.FileHandler(LOG_FILE)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.propagate = False


def insert_speed_test_result(result: dict):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO speed_test_log (
                    recorded_at,
                    ping_ms,
                    download_mbps,
                    upload_mbps,
                    server_name,
                    server_sponsor,
                    server_id,
                    server_distance_km,
                    client_ip,
                    client_isp,
                    raw_timestamp
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    result["recorded_at"],
                    result["ping_ms"],
                    result["download_mbps"],
                    result["upload_mbps"],
                    result["server_name"],
                    result["server_sponsor"],
                    result["server_id"],
                    result["server_distance_km"],
                    result["client_ip"],
                    result["client_isp"],
                    result["raw_timestamp"],
                ),
            )
        conn.commit()

    except Exception as e:
        conn.rollback()
        logger.error("Failed to insert speed test result: %s", e)

    finally:
        put_db_conn(conn)


def run_speedtest():
    try:
        logger.debug("Running fresh Speedtest")
        result = subprocess.run(
            ["speedtest-cli", "--secure", "--json"],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

        data = json.loads(result.stdout)

        logger.info(f"Speedtest result: {data['download'] / 1_000_000} Mbps")
        return {
            "recorded_at": datetime.now(UTC),
            "ping_ms": data["ping"],
            # Divide these numbers by 1 million to get Mbps
            "download_mbps": data["download"] / 1_000_000,
            "upload_mbps": data["upload"] / 1_000_000,
            "server_name": data["server"]["name"],
            "server_sponsor": data["server"]["sponsor"],
            "server_id": data["server"]["id"],
            "server_distance_km": data["server"]["d"],
            "client_ip": data["client"]["ip"],
            "client_isp": data["client"]["isp"],
            "raw_timestamp": data["timestamp"],
        }

    except Exception as exc:
        logger.error("Speedtest failed: %s", exc)
        return None


def main_loop(interval_seconds=900):
    logger.info("Starting speedtest monitor")

    while True:
        start_time = time.time()

        result = run_speedtest()

        if result:
            insert_speed_test_result(result)
        else:
            logger.warning("No result recorded")

        elapsed = time.time() - start_time
        sleep_time = max(0, interval_seconds - elapsed)
        time.sleep(sleep_time)


def run_monitor():
    logger.info("Starting speed test monitor sub thread")
    interval_seconds = 300
    main_loop(interval_seconds=interval_seconds)


if __name__ == "__main__":
    main_loop(interval_seconds=300)

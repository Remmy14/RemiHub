# Python Imports
from datetime import datetime

# 3rd Party Imports

# Local Imports
from backend.database.database import get_db_conn, put_db_conn


def get_latest_pool_temp():
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT timestamp, inlet_temp_f, outdoor_air_temp_f, set_temp_f
                FROM pool_temperature_log
                ORDER BY timestamp DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                return {
                    "timestamp": row[0].isoformat(),
                    "inletTemp": row[1],
                    "airTemp": row[2],
                    "setTemp": row[3],
                }

            return None
    finally:
        put_db_conn(conn)


def get_pool_temps_in_range(start_time: datetime, end_time: datetime) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT timestamp, inlet_temp_f, outdoor_air_temp_f, set_temp_f
                FROM pool_temperature_log
                WHERE timestamp BETWEEN %s AND %s
                ORDER BY timestamp ASC
            """, (start_time, end_time))
            rows = cur.fetchall()
            return [
                {
                    "timestamp": row[0].isoformat(),
                    "inletTemp": row[1],
                    "airTemp": row[2],
                    "setTemp": row[3],
                }
                for row in rows
            ]
    finally:
        put_db_conn(conn)

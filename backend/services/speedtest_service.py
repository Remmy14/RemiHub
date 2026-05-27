from backend.database.database import get_db_conn, put_db_conn


def get_latest_speed_test() -> dict | None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
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
                FROM speed_test_log
                ORDER BY recorded_at DESC
                LIMIT 1;
                """
            )
            row = cur.fetchone()

        if not row:
            return None

        return {
            "id": row[0],
            "recorded_at": row[1].isoformat() if row[1] else None,
            "ping_ms": row[2],
            "download_mbps": row[3],
            "upload_mbps": row[4],
            "server_name": row[5],
            "server_sponsor": row[6],
            "server_id": row[7],
            "server_distance_km": row[8],
            "client_ip": row[9],
            "client_isp": row[10],
            "raw_timestamp": row[11],
        }
    finally:
        put_db_conn(conn)


def get_speed_test_readings(start: str, end: str) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
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
                FROM speed_test_log
                WHERE recorded_at >= %s
                  AND recorded_at <= %s
                ORDER BY recorded_at ASC;
                """,
                (start, end),
            )
            rows = cur.fetchall()

        return [
            {
                "id": row[0],
                "recorded_at": row[1].isoformat() if row[1] else None,
                "ping_ms": row[2],
                "download_mbps": row[3],
                "upload_mbps": row[4],
                "server_name": row[5],
                "server_sponsor": row[6],
                "server_id": row[7],
                "server_distance_km": row[8],
                "client_ip": row[9],
                "client_isp": row[10],
                "raw_timestamp": row[11],
            }
            for row in rows
        ]
    finally:
        put_db_conn(conn)

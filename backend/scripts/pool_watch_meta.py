# Python Imports

# 3rd Party Imports

# Local Imports
from backend.database.database import get_db_conn, put_db_conn


def get_summer_mode() -> bool:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT summer_mode
                FROM pool_watch_meta
                WHERE id = 1
            """)
            row = cur.fetchone()

            if row is None:
                cur.execute("""
                    INSERT INTO pool_watch_meta (id, summer_mode)
                    VALUES (1, TRUE)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING summer_mode
                """)
                row = cur.fetchone()
                conn.commit()

            return bool(row[0])
    finally:
        put_db_conn(conn)


def set_summer_mode(summer_mode: bool) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pool_watch_meta (id, summer_mode, updated_at)
                VALUES (1, %s, NOW())
                ON CONFLICT (id)
                DO UPDATE SET summer_mode = EXCLUDED.summer_mode, updated_at = NOW()
                RETURNING summer_mode, updated_at
            """, (summer_mode,))
            row = cur.fetchone()
        conn.commit()
        return {
            'summerMode': bool(row[0]),
            'updatedAt': row[1].isoformat()
        }
    finally:
        put_db_conn(conn)


if __name__ == "__main__":
    set_summer_mode(False)

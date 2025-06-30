# Python Imports

# 3rd Part Imports

# Local Imports
from backend.database.database import get_db_conn, put_db_conn

def save_pool_to_db(pool_id: int, pool_data: dict):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            # Step 1: Clear existing assignments
            cur.execute("DELETE FROM indy_pool_assignments WHERE pool_id=%s;", (pool_id, ))

            # Step 2: Insert new assignments
            for participant, drivers in pool_data.items():
                for driver in drivers:
                    cur.execute(f"""
                        INSERT INTO indy_pool_assignments (
                            participant_name,
                            car_number,
                            driver_name, 
                            pool_id
                        ) VALUES (%s, %s, %s, %s)
                    """, (
                        participant,
                        driver['number'],
                        driver['name'],
                        pool_id,
                    ))
        conn.commit()
    finally:
        put_db_conn(conn)

def load_pool_from_db(pool_id: int, ) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT participant_name, car_number, driver_name
                FROM indy_pool_assignments
                WHERE pool_id=%s;
            """, (pool_id, ))
            rows = cur.fetchall()

            pool = {}
            for participant_name, car_number, driver_name in rows:
                if participant_name not in pool:
                    pool[participant_name] = []
                pool[participant_name].append({
                    'number': car_number,
                    'name': driver_name
                })

            # Sort drivers inside each participant for clean UI
            for participant_drivers in pool.values():
                participant_drivers.sort(key=lambda d: int(d['number']))

            return pool
    finally:
        put_db_conn(conn)

def get_all_pools():
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, participant_count FROM indy_pools ORDER BY id ASC")
            rows = cur.fetchall()
            return [{'id': row[0], 'name': row[1], 'participantCount': row[2]} for row in rows]
    finally:
        put_db_conn(conn)

def create_pool(name: str, participant_count: int):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO indy_pools (name, participant_count) VALUES (%s, %s) RETURNING id",
                (name, participant_count)
            )
            pool_id = cur.fetchone()[0]
        conn.commit()
        return {"success": True, "id": pool_id, "name": name}
    finally:
        put_db_conn(conn)


if __name__ == '__main__':
    print(get_all_pools())
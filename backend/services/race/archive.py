# Local Imports
from backend.database.database import get_db_conn, put_db_conn


def archive_pool(
    pool_id: int,
    year: int,
    race_name: str = "Indianapolis 500",
    notes: str | None = None,
) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT name
                FROM indy_pools
                WHERE id = %s;
            """, (pool_id,))
            pool_row = cur.fetchone()

            if not pool_row:
                return {
                    "success": False,
                    "message": f"Pool {pool_id} does not exist.",
                }

            pool_name = pool_row[0]

            cur.execute("""
                SELECT id
                FROM indy_pool_archives
                WHERE year = %s
                  AND race_name = %s
                  AND pool_id = %s;
            """, (year, race_name, pool_id))
            existing_archive = cur.fetchone()

            if existing_archive:
                return {
                    "success": False,
                    "message": "This pool has already been archived for this race/year.",
                    "archive_id": existing_archive[0],
                }

            cur.execute("""
                SELECT COUNT(*)
                FROM indy_pool_assignments
                WHERE pool_id = %s;
            """, (pool_id,))
            assignment_count = cur.fetchone()[0]

            if assignment_count == 0:
                return {
                    "success": False,
                    "message": "This pool has no assignments to archive.",
                }

            cur.execute("""
                INSERT INTO indy_pool_archives (
                    year,
                    race_name,
                    pool_id,
                    pool_name,
                    notes
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
            """, (
                year,
                race_name,
                pool_id,
                pool_name,
                notes,
            ))

            archive_id = cur.fetchone()[0]

            cur.execute("""
                INSERT INTO indy_pool_archive_entries (
                    archive_id,
                    participant_name,
                    car_number,
                    driver_name,
                    pick_number,
                    starting_position,
                    finishing_position,
                    final_status,
                    laps_completed
                )
                SELECT
                    %s AS archive_id,
                    assignments.participant_name,
                    assignments.car_number,
                    assignments.driver_name,
                    assignments.pick_number,
                    starting_grid.starting_position,
                    leaderboard.position AS finishing_position,
                    leaderboard.status AS final_status,
                    leaderboard.laps_completed
                FROM indy_pool_assignments assignments
                LEFT JOIN indy_pool_starting_grid starting_grid
                    ON starting_grid.car_number = assignments.car_number
                LEFT JOIN indy_pool_leaderboard leaderboard
                    ON leaderboard.car_number = assignments.car_number
                WHERE assignments.pool_id = %s
                ORDER BY assignments.pick_number ASC;
            """, (archive_id, pool_id))

            entries_archived = cur.rowcount

        conn.commit()

        return {
            "success": True,
            "message": "Pool archived successfully.",
            "archive_id": archive_id,
            "entries_archived": entries_archived,
        }

    except Exception as e:
        conn.rollback()
        return {
            "success": False,
            "message": f"Error archiving pool: {e}",
        }

    finally:
        put_db_conn(conn)


def get_archives() -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id,
                    year,
                    race_name,
                    pool_id,
                    pool_name,
                    archived_at,
                    notes
                FROM indy_pool_archives
                ORDER BY year DESC, race_name ASC, pool_name ASC;
            """)

            rows = cur.fetchall()

            return [
                {
                    "id": row[0],
                    "year": row[1],
                    "race_name": row[2],
                    "pool_id": row[3],
                    "pool_name": row[4],
                    "archived_at": row[5].isoformat() if row[5] else None,
                    "notes": row[6],
                }
                for row in rows
            ]

    finally:
        put_db_conn(conn)


def get_archive_entries(archive_id: int) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id,
                    year,
                    race_name,
                    pool_id,
                    pool_name,
                    archived_at,
                    notes
                FROM indy_pool_archives
                WHERE id = %s;
            """, (archive_id,))

            archive_row = cur.fetchone()

            if not archive_row:
                return {
                    "success": False,
                    "message": f"Archive {archive_id} does not exist.",
                }

            cur.execute("""
                SELECT
                    participant_name,
                    car_number,
                    driver_name,
                    pick_number,
                    starting_position,
                    finishing_position,
                    final_status,
                    laps_completed
                FROM indy_pool_archive_entries
                WHERE archive_id = %s
                ORDER BY pick_number ASC NULLS LAST, participant_name ASC, driver_name ASC;
            """, (archive_id,))

            rows = cur.fetchall()

            return {
                "success": True,
                "archive": {
                    "id": archive_row[0],
                    "year": archive_row[1],
                    "race_name": archive_row[2],
                    "pool_id": archive_row[3],
                    "pool_name": archive_row[4],
                    "archived_at": archive_row[5].isoformat() if archive_row[5] else None,
                    "notes": archive_row[6],
                },
                "entries": [
                    {
                        "participant_name": row[0],
                        "car_number": row[1],
                        "driver_name": row[2],
                        "pick_number": row[3],
                        "starting_position": row[4],
                        "finishing_position": row[5],
                        "final_status": row[6],
                        "laps_completed": row[7],
                    }
                    for row in rows
                ],
            }

    finally:
        put_db_conn(conn)

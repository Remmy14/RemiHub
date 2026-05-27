from pathlib import Path

from backend.database.database import get_db_conn, put_db_conn


RELEASES_BASE_DIR = Path("/opt/remihub")


def get_latest_app_release(platform: str) -> dict | None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    platform,
                    version_code,
                    version_name,
                    apk_filename,
                    apk_relative_path,
                    apk_sha256,
                    file_size_bytes,
                    created_at,
                    is_active
                FROM app_release
                WHERE platform = %s
                  AND is_active = TRUE
                ORDER BY version_code DESC, created_at DESC
                LIMIT 1;
                """,
                (platform,),
            )
            row = cur.fetchone()

        if not row:
            return None

        return {
            "id": row[0],
            "platform": row[1],
            "version_code": row[2],
            "version_name": row[3],
            "apk_filename": row[4],
            "apk_relative_path": row[5],
            "apk_sha256": row[6],
            "file_size_bytes": row[7],
            "created_at": row[8].isoformat() if row[8] else None,
            "is_active": row[9],
        }
    finally:
        put_db_conn(conn)


def get_app_release_by_id(release_id: int) -> dict | None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    platform,
                    version_code,
                    version_name,
                    apk_filename,
                    apk_relative_path,
                    apk_sha256,
                    file_size_bytes,
                    created_at,
                    is_active
                FROM app_release
                WHERE id = %s
                LIMIT 1;
                """,
                (release_id,),
            )
            row = cur.fetchone()

        if not row:
            return None

        return {
            "id": row[0],
            "platform": row[1],
            "version_code": row[2],
            "version_name": row[3],
            "apk_filename": row[4],
            "apk_relative_path": row[5],
            "apk_sha256": row[6],
            "file_size_bytes": row[7],
            "created_at": row[8].isoformat() if row[8] else None,
            "is_active": row[9],
        }
    finally:
        put_db_conn(conn)


def resolve_release_file_path(apk_relative_path: str) -> Path:
    return RELEASES_BASE_DIR / apk_relative_path

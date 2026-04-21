from pydantic import BaseModel
from fastapi import APIRouter, HTTPException

from backend.database.database import get_db_conn, put_db_conn


router = APIRouter(prefix="/notifications", tags=["notifications"])


class DeviceTokenRegistrationRequest(BaseModel):
    device_id: str
    platform: str
    fcm_token: str
    device_name: str | None = None
    app_version: str | None = None


@router.post("/register-token")
def register_token(payload: DeviceTokenRegistrationRequest):
    if not payload.device_id.strip():
        raise HTTPException(status_code=400, detail="device_id cannot be blank")

    if not payload.platform.strip():
        raise HTTPException(status_code=400, detail="platform cannot be blank")

    if not payload.fcm_token.strip():
        raise HTTPException(status_code=400, detail="fcm_token cannot be blank")

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO device_push_tokens (
                    device_id,
                    platform,
                    fcm_token,
                    device_name,
                    app_version,
                    is_active,
                    last_seen_at
                )
                VALUES (%s, %s, %s, %s, %s, TRUE, CURRENT_TIMESTAMP)
                ON CONFLICT (device_id)
                DO UPDATE SET
                    platform = EXCLUDED.platform,
                    fcm_token = EXCLUDED.fcm_token,
                    device_name = EXCLUDED.device_name,
                    app_version = EXCLUDED.app_version,
                    is_active = TRUE,
                    last_seen_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING
                    id,
                    device_id,
                    platform,
                    device_name,
                    app_version,
                    is_active,
                    last_seen_at,
                    created_at,
                    updated_at;
                """,
                (
                    payload.device_id,
                    payload.platform,
                    payload.fcm_token,
                    payload.device_name,
                    payload.app_version,
                ),
            )
            row = cur.fetchone()

        conn.commit()

        return {
            "success": True,
            "data": {
                "id": row[0],
                "device_id": row[1],
                "platform": row[2],
                "device_name": row[3],
                "app_version": row[4],
                "is_active": row[5],
                "last_seen_at": row[6].isoformat() if row[6] else None,
                "created_at": row[7].isoformat() if row[7] else None,
                "updated_at": row[8].isoformat() if row[8] else None,
            },
        }
    except Exception as e:
        conn.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to register device push token: {e}",
        )
    finally:
        put_db_conn(conn)

# Python Imports
from datetime import date

# 3rd Party Imports
from fastapi import HTTPException
from pydantic import BaseModel
from typing import Optional

# Local Imports
from backend.database.database import get_db_conn, put_db_conn


class FieldWatchRequest(BaseModel):
    field_name: str
    target_date: date

class FieldWatchResponse(BaseModel):
    id: int
    field_name: str
    target_date: date
    last_known_status: Optional[str] = None

def add_field_watch_to_db(req: FieldWatchRequest):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO field_watch (field_name, target_date)
                VALUES (%s, %s)
            """, (req.field_name, req.target_date))
            conn.commit()
        return {"success": True, "message": "Field watch added"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_db_conn(conn)


def get_field_watches_from_db():
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, field_name, target_date, last_known_status
                FROM field_watch
                WHERE target_date >= CURRENT_DATE
                ORDER BY target_date ASC
            """)
            rows = cur.fetchall()
            results = [
                FieldWatchResponse(
                    id=row[0],
                    field_name=row[1],
                    target_date=row[2],
                    last_known_status=row[3]
                )
                for row in rows
            ]
            return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_db_conn(conn)

def delete_field_watch_from_db(watch_id: int):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM field_watch WHERE id = %s", (watch_id,))
            conn.commit()
        return {"success": True, "message": f"Field watch {watch_id} deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_db_conn(conn)
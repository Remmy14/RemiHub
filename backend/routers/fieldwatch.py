# Python Imports

# 3rd Party Imports
from fastapi import APIRouter, HTTPException

# Local Imports
from backend.services import field_service
from backend.services.field_service import FieldWatchRequest, FieldWatchResponse

router = APIRouter()

@router.post("/fieldwatch/add")
def add_field_watch(req: FieldWatchRequest):
    return field_service.add_field_watch_to_db(req)

@router.get("/fieldwatch/upcoming", response_model=list[FieldWatchResponse])
def get_upcoming_field_watches():
    return field_service.get_field_watches_from_db()

@router.delete("/fieldwatch/delete/{watch_id}")
def delete_field_watch(watch_id: int):
    return field_service.delete_field_watch_from_db(watch_id)

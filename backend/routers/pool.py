# Python Imports
from datetime import datetime

# 3rd Party Imports
from fastapi import APIRouter, Query

# Local Imports
from backend.services import pool_service

router = APIRouter(prefix="/pool", tags=["Pool"])

@router.get("/getLatestTemp")
def get_latest_temps():
    data = pool_service.get_latest_pool_temp()
    if data:
        return {"success": True, "data": data}
    return {"success": False, "message": "No temperature data available"}


@router.get("/getTempsRange")
def get_temps_in_range(
    start: datetime = Query(..., description="Start datetime in ISO format"),
    end: datetime = Query(..., description="End datetime in ISO format")
):
    data = pool_service.get_pool_temps_in_range(start, end)
    return {"success": True, "data": data}

@router.get('/getMode')
def get_pool_mode():
    data = pool_service.get_pool_mode()
    return {'success': True, 'data': data}


@router.post('/setMode')
def set_pool_mode(
    summerMode: bool = Query(..., description='true = enabled/summer, false = disabled/winter')
):
    data = pool_service.set_pool_mode(summerMode)
    return {'success': True, 'data': data}

from datetime import datetime

from fastapi import APIRouter, Query

from backend.services import weather_service


router = APIRouter(prefix="/weather", tags=["Weather"])


@router.get("/getLatest")
def get_latest_weather():
    data = weather_service.get_latest_weather_reading()

    if data:
        return {"success": True, "data": data}

    return {"success": False, "message": "No weather data available"}


@router.get("/getReadingsRange")
def get_weather_readings_range(
    start: datetime = Query(..., description="Start datetime in ISO format"),
    end: datetime = Query(..., description="End datetime in ISO format"),
):
    data = weather_service.get_weather_readings_in_range(start, end)
    return {"success": True, "data": data}

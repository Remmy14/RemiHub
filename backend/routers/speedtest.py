from fastapi import APIRouter, HTTPException, Query

from backend.services.speedtest_service import (
    get_latest_speed_test,
    get_speed_test_readings,
)


router = APIRouter(prefix="/speedtest", tags=["speedtest"])


@router.get("/latest")
def latest_speed_test():
    result = get_latest_speed_test()

    if not result:
        raise HTTPException(status_code=404, detail="No speed test results found")

    return {
        "success": True,
        "data": result,
    }


@router.get("/readings")
def speed_test_readings(
    start: str = Query(..., description="ISO-8601 start datetime"),
    end: str = Query(..., description="ISO-8601 end datetime"),
):
    results = get_speed_test_readings(start=start, end=end)

    return {
        "success": True,
        "data": results,
    }

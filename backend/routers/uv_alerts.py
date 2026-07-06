from fastapi import APIRouter, HTTPException, Query

from backend.models.uv_alert_models import UvAlertSettingsUpdate
from backend.services import uv_alert_service
from backend.tasks import uv_alert_check

router = APIRouter(prefix="/uv", tags=["UV Alerts"])


@router.get("/settings")
def get_uv_alert_settings():
    data = uv_alert_service.get_uv_alert_settings()
    return {"success": True, "data": data}


@router.post("/settings")
def update_uv_alert_settings(settings: UvAlertSettingsUpdate):
    data = uv_alert_service.update_uv_alert_settings(
        enabled=settings.enabled,
        profile_name=settings.profileName,
        alert_start_hour=settings.alertStartHour,
        alert_end_hour=settings.alertEndHour,
    )
    return {"success": True, "data": data}


@router.get("/state")
def get_uv_alert_state():
    data = uv_alert_service.get_uv_alert_state()
    return {"success": True, "data": data}


@router.post("/action")
def apply_uv_alert_action(
    action: str = Query(..., description="not_pool_day, ignore_today, snooze_1_hour, or sunscreen_applied")
):
    try:
        data = uv_alert_service.apply_uv_alert_action(action)
        return {"success": True, "data": data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/clearSuppression")
def clear_uv_alert_suppression():
    data = uv_alert_service.clear_uv_alert_suppression()
    return {"success": True, "data": data}


@router.get("/decision")
def get_uv_alert_decision():
    data = uv_alert_check.get_uv_alert_decision()
    return {"success": True, "data": data}

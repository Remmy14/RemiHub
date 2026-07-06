from pydantic import BaseModel

DEFAULT_PROFILE_NAME = "default"

UV_ACTION_NOT_POOL_DAY = "not_pool_day"
UV_ACTION_IGNORE_TODAY = "ignore_today"
UV_ACTION_SNOOZE_1_HOUR = "snooze_1_hour"
UV_ACTION_SUNSCREEN_APPLIED = "sunscreen_applied"

VALID_UV_ALERT_ACTIONS = {
    UV_ACTION_NOT_POOL_DAY,
    UV_ACTION_IGNORE_TODAY,
    UV_ACTION_SNOOZE_1_HOUR,
    UV_ACTION_SUNSCREEN_APPLIED,
}


class UvAlertSettingsUpdate(BaseModel):
    enabled: bool | None = None
    profileName: str | None = None
    alertStartHour: int | None = None
    alertEndHour: int | None = None


class UvAlertDecision(BaseModel):
    shouldAlert: bool
    reason: str
    uv: float | None = None
    dosePoints: float = 0
    threshold: float | None = None
    profileName: str = DEFAULT_PROFILE_NAME
    suppressedUntil: str | None = None


class UvAlertActionResult(BaseModel):
    action: str
    suppressUntil: str | None = None
    suppressReason: str | None = None
    sunscreenAppliedAt: str | None = None

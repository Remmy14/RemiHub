from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import os
from pathlib import Path
from typing import Callable, Any


GARMIN_TOKENSTORE_ENV = "GARMIN_TOKENSTORE"
GARMIN_PROVIDER = "GARMIN"
METERS_PER_MILE = Decimal("1609.344")


class GarminProviderError(RuntimeError):
    code = "GARMIN_ERROR"


class GarminAuthError(GarminProviderError):
    code = "GARMIN_AUTH_ERROR"


class GarminRateLimitError(GarminProviderError):
    code = "GARMIN_RATE_LIMIT"


class GarminNetworkError(GarminProviderError):
    code = "GARMIN_NETWORK_ERROR"


class GarminApiError(GarminProviderError):
    code = "GARMIN_API_ERROR"


class GarminMalformedResponseError(GarminProviderError):
    code = "GARMIN_MALFORMED_RESPONSE"


@dataclass(frozen=True)
class GarminActivityCandidate:
    activity_id: str
    activity_name: str | None
    start_time_local: str | None
    distance: Decimal | None
    duration: int | None

    def to_api_dict(self) -> dict:
        return {
            "activityId": self.activity_id,
            "activityName": self.activity_name,
            "startTimeLocal": self.start_time_local,
            "distance": float(self.distance) if self.distance is not None else None,
            "duration": self.duration,
        }


@dataclass(frozen=True)
class GarminRunningActivity:
    external_provider: str
    external_activity_id: str
    external_activity_uuid: str | None
    external_activity_name: str | None
    completed_distance_miles: Decimal
    duration_seconds: int
    moving_duration_seconds: int | None
    average_speed_meters_per_second: Decimal | None
    average_hr: Decimal | None
    max_hr: Decimal | None
    training_load: Decimal | None
    aerobic_training_effect: Decimal | None
    anaerobic_training_effect: Decimal | None
    training_effect_label: str | None
    vo2_max: Decimal | None
    hr_zone_1_seconds: int | None
    hr_zone_2_seconds: int | None
    hr_zone_3_seconds: int | None
    hr_zone_4_seconds: int | None
    hr_zone_5_seconds: int | None
    average_cadence_spm: Decimal | None
    average_power_watts: Decimal | None
    average_stride_length_meters: Decimal | None
    elevation_gain_meters: Decimal | None
    elevation_loss_meters: Decimal | None
    calories: Decimal | None
    steps: int | None

    def to_insert_params(self) -> dict:
        return self.__dict__.copy()


@dataclass(frozen=True)
class GarminActivityResolution:
    activities: list[GarminRunningActivity]
    candidates: list[GarminActivityCandidate]


def configured_tokenstore() -> str:
    tokenstore = os.environ.get(GARMIN_TOKENSTORE_ENV, "").strip()
    if not tokenstore:
        raise GarminAuthError(f"Missing required environment variable: {GARMIN_TOKENSTORE_ENV}")
    return tokenstore


def _garmin_class():
    from garminconnect import Garmin

    return Garmin


def _decimal(value: Any, *, required: bool = False) -> Decimal | None:
    if value is None:
        if required:
            raise GarminMalformedResponseError("Garmin activity summary is missing a required metric")
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise GarminMalformedResponseError("Garmin activity summary contains an invalid decimal") from exc


def _seconds(value: Any, *, required: bool = False) -> int | None:
    decimal_value = _decimal(value, required=required)
    if decimal_value is None:
        return None
    return int(decimal_value.to_integral_value(rounding=ROUND_HALF_UP))


def _int_count(value: Any) -> int | None:
    decimal_value = _decimal(value)
    if decimal_value is None:
        return None
    return int(decimal_value.to_integral_value(rounding=ROUND_HALF_UP))


def _activity_type_key(summary: dict) -> str | None:
    activity_type = summary.get("activityType")
    if isinstance(activity_type, dict):
        value = activity_type.get("typeKey")
        return str(value) if value is not None else None
    return None


def normalize_activity_summary(summary: dict) -> GarminRunningActivity:
    if not isinstance(summary, dict):
        raise GarminMalformedResponseError("Garmin activity summary is not an object")
    if _activity_type_key(summary) != "running":
        raise GarminMalformedResponseError("Garmin activity summary is not a running activity")

    activity_id = summary.get("activityId")
    if activity_id is None or str(activity_id).strip() == "":
        raise GarminMalformedResponseError("Garmin activity summary is missing activityId")

    distance_meters = _decimal(summary.get("distance"), required=True)
    duration_seconds = _seconds(summary.get("duration"), required=True)
    return GarminRunningActivity(
        external_provider=GARMIN_PROVIDER,
        external_activity_id=str(activity_id),
        external_activity_uuid=(
            str(summary["activityUUID"]) if summary.get("activityUUID") is not None else None
        ),
        external_activity_name=(
            str(summary["activityName"]) if summary.get("activityName") is not None else None
        ),
        completed_distance_miles=distance_meters / METERS_PER_MILE,
        duration_seconds=duration_seconds,
        moving_duration_seconds=_seconds(summary.get("movingDuration")),
        average_speed_meters_per_second=_decimal(summary.get("averageSpeed")),
        average_hr=_decimal(summary.get("averageHR")),
        max_hr=_decimal(summary.get("maxHR")),
        training_load=_decimal(summary.get("activityTrainingLoad")),
        aerobic_training_effect=_decimal(summary.get("aerobicTrainingEffect")),
        anaerobic_training_effect=_decimal(summary.get("anaerobicTrainingEffect")),
        training_effect_label=(
            str(summary["trainingEffectLabel"])
            if summary.get("trainingEffectLabel") is not None
            else None
        ),
        vo2_max=_decimal(summary.get("vO2MaxValue")),
        hr_zone_1_seconds=_seconds(summary.get("hrTimeInZone_1")),
        hr_zone_2_seconds=_seconds(summary.get("hrTimeInZone_2")),
        hr_zone_3_seconds=_seconds(summary.get("hrTimeInZone_3")),
        hr_zone_4_seconds=_seconds(summary.get("hrTimeInZone_4")),
        hr_zone_5_seconds=_seconds(summary.get("hrTimeInZone_5")),
        average_cadence_spm=_decimal(summary.get("averageRunningCadenceInStepsPerMinute")),
        average_power_watts=_decimal(summary.get("avgPower")),
        average_stride_length_meters=(
            _decimal(summary.get("avgStrideLength")) / Decimal("100")
            if summary.get("avgStrideLength") is not None
            else None
        ),
        elevation_gain_meters=_decimal(summary.get("elevationGain")),
        elevation_loss_meters=_decimal(summary.get("elevationLoss")),
        calories=_decimal(summary.get("calories")),
        steps=_int_count(summary.get("steps")),
    )


def summarize_candidate(summary: dict) -> GarminActivityCandidate:
    if not isinstance(summary, dict) or summary.get("activityId") is None:
        raise GarminMalformedResponseError("Garmin activity candidate is malformed")
    return GarminActivityCandidate(
        activity_id=str(summary["activityId"]),
        activity_name=(
            str(summary["activityName"]) if summary.get("activityName") is not None else None
        ),
        start_time_local=(
            str(summary["startTimeLocal"]) if summary.get("startTimeLocal") is not None else None
        ),
        distance=_decimal(summary.get("distance")),
        duration=_seconds(summary.get("duration")),
    )


def _translate_garmin_error(exc: Exception) -> GarminProviderError:
    name = exc.__class__.__name__.lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if status == 429 or "ratelimit" in name or "too many" in str(exc).lower():
        return GarminRateLimitError("Garmin rate limit reached")
    if "auth" in name or "credential" in name or "token" in name:
        return GarminAuthError("Garmin authentication failed")
    if "connection" in name or "timeout" in name or "network" in name:
        return GarminNetworkError("Garmin network request failed")
    return GarminApiError("Garmin API request failed")


def _garmin_call(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except GarminProviderError:
        raise
    except Exception as exc:
        raise _translate_garmin_error(exc) from exc


def _runtime_api(tokenstore: str | None = None):
    resolved_tokenstore = tokenstore or configured_tokenstore()
    Garmin = _garmin_class()
    api = Garmin()
    _garmin_call(lambda: api.login(resolved_tokenstore))
    return api


def fetch_running_activity_summaries(
    scheduled_date: date,
    *,
    tokenstore: str | None = None,
) -> list[dict]:
    api = _runtime_api(tokenstore)
    activities = _garmin_call(
        lambda: api.get_activities_by_date(
            scheduled_date,
            scheduled_date,
            activitytype="running",
            sortorder="asc",
        )
    )
    if not isinstance(activities, list):
        raise GarminMalformedResponseError("Garmin activities response is not a list")
    return activities


def list_running_activity_candidates(
    scheduled_date: date,
    *,
    tokenstore: str | None = None,
) -> list[GarminActivityCandidate]:
    return [
        summarize_candidate(activity)
        for activity in fetch_running_activity_summaries(scheduled_date, tokenstore=tokenstore)
    ]


def get_running_activities(
    scheduled_date: date,
    *,
    tokenstore: str | None = None,
) -> list[GarminRunningActivity]:
    return [
        normalize_activity_summary(activity)
        for activity in fetch_running_activity_summaries(scheduled_date, tokenstore=tokenstore)
    ]


def resolve_running_activities(
    scheduled_date: date,
    *,
    tokenstore: str | None = None,
) -> GarminActivityResolution:
    summaries = fetch_running_activity_summaries(scheduled_date, tokenstore=tokenstore)
    if len(summaries) > 1:
        return GarminActivityResolution(
            activities=[],
            candidates=[summarize_candidate(activity) for activity in summaries],
        )
    return GarminActivityResolution(
        activities=[normalize_activity_summary(activity) for activity in summaries],
        candidates=[],
    )


def find_running_activity(
    scheduled_date: date,
    *,
    activity_id: str,
    tokenstore: str | None = None,
) -> GarminRunningActivity | None:
    selected = str(activity_id)
    for activity in fetch_running_activity_summaries(scheduled_date, tokenstore=tokenstore):
        if str(activity.get("activityId")) == selected:
            return normalize_activity_summary(activity)
    return None


def bootstrap_garmin_tokenstore(
    *,
    email: str,
    password: str,
    tokenstore: str | Path,
    prompt_mfa: Callable[[], str] | None = None,
) -> None:
    if not email.strip():
        raise GarminAuthError("Garmin email is required")
    if not password:
        raise GarminAuthError("Garmin password is required")
    Garmin = _garmin_class()
    kwargs = {"prompt_mfa": prompt_mfa} if prompt_mfa is not None else {}
    api = Garmin(email, password, **kwargs)
    _garmin_call(lambda: api.login(str(tokenstore)))

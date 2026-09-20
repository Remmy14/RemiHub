from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from backend.core.auth import AuthenticatedPrincipal, require_current_principal
from backend.models.vehicle_models import (
    FuelRecordCreate,
    FuelRecordUpdate,
    MaintenanceEventCreate,
    MaintenanceEventUpdate,
    MaintenanceScheduleCreate,
    MaintenanceScheduleUpdate,
    ManualOdometerObservationCreate,
    VehicleCreate,
    VehicleUpdate,
)
from backend.services import vehicle_service


router = APIRouter(prefix="/vehicles", tags=["Vehicles"])


def _handle_service_error(exc: ValueError) -> HTTPException:
    if isinstance(exc, vehicle_service.VehicleNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, vehicle_service.VehicleConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("")
def list_vehicles(
    include_archived: bool = False,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    return {
        "success": True,
        "data": vehicle_service.list_vehicles(
            user_id=principal.id,
            include_archived=include_archived,
        ),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_vehicle(
    request: VehicleCreate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.create_vehicle(
                user_id=principal.id,
                **request.model_dump(mode="json"),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/{vehicle_id}")
def get_vehicle(
    vehicle_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.get_vehicle(
                user_id=principal.id,
                vehicle_id=vehicle_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.patch("/{vehicle_id}")
def update_vehicle(
    vehicle_id: str,
    request: VehicleUpdate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.update_vehicle(
                principal.id,
                vehicle_id,
                **request.model_dump(mode="json", exclude_unset=True),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/{vehicle_id}/archive")
def archive_vehicle(
    vehicle_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.archive_vehicle(
                user_id=principal.id,
                vehicle_id=vehicle_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/{vehicle_id}/restore")
def restore_vehicle(
    vehicle_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.restore_vehicle(
                user_id=principal.id,
                vehicle_id=vehicle_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/{vehicle_id}/odometer-observations")
def list_odometer_observations(
    vehicle_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.list_odometer_observations(
                user_id=principal.id,
                vehicle_id=vehicle_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/{vehicle_id}/odometer-observations", status_code=status.HTTP_201_CREATED)
def create_manual_odometer_observation(
    vehicle_id: str,
    request: ManualOdometerObservationCreate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.create_manual_odometer_observation(
                user_id=principal.id,
                vehicle_id=vehicle_id,
                odometer_miles=request.odometer_miles,
                observed_at=request.observed_at,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/{vehicle_id}/fuel-records")
def list_fuel_records(
    vehicle_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.list_fuel_records(
                user_id=principal.id,
                vehicle_id=vehicle_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/{vehicle_id}/fuel-records", status_code=status.HTTP_201_CREATED)
def create_fuel_record(
    vehicle_id: str,
    request: FuelRecordCreate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.create_fuel_record(
                user_id=principal.id,
                vehicle_id=vehicle_id,
                **request.model_dump(mode="json"),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/{vehicle_id}/fuel-records/{fuel_record_id}")
def get_fuel_record(
    vehicle_id: str,
    fuel_record_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.get_fuel_record(
                user_id=principal.id,
                vehicle_id=vehicle_id,
                fuel_record_id=fuel_record_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.patch("/{vehicle_id}/fuel-records/{fuel_record_id}")
def update_fuel_record(
    vehicle_id: str,
    fuel_record_id: str,
    request: FuelRecordUpdate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.update_fuel_record(
                principal.id,
                vehicle_id,
                fuel_record_id,
                **request.model_dump(mode="json", exclude_unset=True),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/{vehicle_id}/fuel-summary")
def get_fuel_summary(
    vehicle_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.get_fuel_summary(
                user_id=principal.id,
                vehicle_id=vehicle_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/{vehicle_id}/maintenance-schedules")
def list_maintenance_schedules(
    vehicle_id: str,
    include_archived: bool = False,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.list_maintenance_schedules(
                user_id=principal.id,
                vehicle_id=vehicle_id,
                include_archived=include_archived,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/{vehicle_id}/maintenance-schedules", status_code=status.HTTP_201_CREATED)
def create_maintenance_schedule(
    vehicle_id: str,
    request: MaintenanceScheduleCreate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.create_maintenance_schedule(
                user_id=principal.id,
                vehicle_id=vehicle_id,
                **request.model_dump(mode="json"),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/{vehicle_id}/maintenance-schedules/{schedule_id}")
def get_maintenance_schedule(
    vehicle_id: str,
    schedule_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.get_maintenance_schedule(
                user_id=principal.id,
                vehicle_id=vehicle_id,
                schedule_id=schedule_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.patch("/{vehicle_id}/maintenance-schedules/{schedule_id}")
def update_maintenance_schedule(
    vehicle_id: str,
    schedule_id: str,
    request: MaintenanceScheduleUpdate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.update_maintenance_schedule(
                principal.id,
                vehicle_id,
                schedule_id,
                **request.model_dump(mode="json", exclude_unset=True),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/{vehicle_id}/maintenance-schedules/{schedule_id}/archive")
def archive_maintenance_schedule(
    vehicle_id: str,
    schedule_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.set_maintenance_schedule_active(
                user_id=principal.id,
                vehicle_id=vehicle_id,
                schedule_id=schedule_id,
                active=False,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/{vehicle_id}/maintenance-schedules/{schedule_id}/restore")
def restore_maintenance_schedule(
    vehicle_id: str,
    schedule_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.set_maintenance_schedule_active(
                user_id=principal.id,
                vehicle_id=vehicle_id,
                schedule_id=schedule_id,
                active=True,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/{vehicle_id}/maintenance-events")
def list_maintenance_events(
    vehicle_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.list_maintenance_events(
                user_id=principal.id,
                vehicle_id=vehicle_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/{vehicle_id}/maintenance-events", status_code=status.HTTP_201_CREATED)
def create_maintenance_event(
    vehicle_id: str,
    request: MaintenanceEventCreate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.create_maintenance_event(
                user_id=principal.id,
                vehicle_id=vehicle_id,
                **request.model_dump(mode="json"),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/{vehicle_id}/maintenance-events/{event_id}")
def get_maintenance_event(
    vehicle_id: str,
    event_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.get_maintenance_event(
                user_id=principal.id,
                vehicle_id=vehicle_id,
                event_id=event_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.patch("/{vehicle_id}/maintenance-events/{event_id}")
def update_maintenance_event(
    vehicle_id: str,
    event_id: str,
    request: MaintenanceEventUpdate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.update_maintenance_event(
                principal.id,
                vehicle_id,
                event_id,
                **request.model_dump(mode="json", exclude_unset=True),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/{vehicle_id}/maintenance-status")
def get_maintenance_status(
    vehicle_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.get_maintenance_status(
                user_id=principal.id,
                vehicle_id=vehicle_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/{vehicle_id}/summary")
def get_vehicle_summary(
    vehicle_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": vehicle_service.get_vehicle_summary(
                user_id=principal.id,
                vehicle_id=vehicle_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)

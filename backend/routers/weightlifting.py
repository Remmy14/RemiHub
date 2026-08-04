from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.core.auth import AuthenticatedPrincipal, require_current_principal
from backend.models.weightlifting_models import (
    WeightliftingEntryClear,
    WeightliftingEntryUpdate,
    WeightliftingEntryUpsert,
    WeightliftingExerciseCreate,
    WeightliftingExerciseReorder,
    WeightliftingExerciseUpdate,
    WeightliftingSettingsUpdate,
)
from backend.services import weightlifting_service


router = APIRouter(prefix="/weightlifting", tags=["Weightlifting"])


def _handle_service_error(exc: ValueError) -> HTTPException:
    if isinstance(exc, weightlifting_service.WeightliftingNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, weightlifting_service.WeightliftingConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/settings")
def get_settings(
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    return {
        "success": True,
        "data": weightlifting_service.get_settings(principal.id),
    }


@router.put("/settings")
def update_settings(
    request: WeightliftingSettingsUpdate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": weightlifting_service.update_settings(
                user_id=principal.id,
                weight_unit=request.weight_unit.value,
                default_weight_increment=request.default_weight_increment,
                default_target_reps=request.default_target_reps,
                default_sets=request.default_sets,
                days=[day.model_dump(mode="json") for day in request.days],
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/exercises")
def list_exercises(
    include_archived: bool = False,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    return {
        "success": True,
        "data": weightlifting_service.list_exercises(
            user_id=principal.id,
            include_archived=include_archived,
        ),
    }


@router.post("/exercises", status_code=status.HTTP_201_CREATED)
def create_exercise(
    request: WeightliftingExerciseCreate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": weightlifting_service.create_exercise(
                user_id=principal.id,
                name=request.name,
                display_order=request.display_order,
                notes=request.notes,
                target_reps=request.target_reps,
                target_sets=request.target_sets,
                weight_increment=request.weight_increment,
                weight_unit=request.weight_unit.value if request.weight_unit else None,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.patch("/exercises/{exercise_id}")
def update_exercise(
    exercise_id: str,
    request: WeightliftingExerciseUpdate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        payload = request.model_dump(exclude_unset=True)
        if "weight_unit" in payload and payload["weight_unit"] is not None:
            payload["weight_unit"] = payload["weight_unit"].value
        return {
            "success": True,
            "data": weightlifting_service.update_exercise(
                principal.id,
                exercise_id,
                **payload,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.put("/exercises/order")
def reorder_exercises(
    request: WeightliftingExerciseReorder,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": weightlifting_service.reorder_exercises(
                user_id=principal.id,
                exercises=[item.model_dump(mode="json") for item in request.exercises],
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/exercises/{exercise_id}/archive")
def archive_exercise(
    exercise_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": weightlifting_service.set_exercise_active(
                user_id=principal.id,
                exercise_id=exercise_id,
                active=False,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/exercises/{exercise_id}/restore")
def restore_exercise(
    exercise_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": weightlifting_service.set_exercise_active(
                user_id=principal.id,
                exercise_id=exercise_id,
                active=True,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/grid")
def get_weekly_grid(
    week_start: date = Query(...),
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    return {
        "success": True,
        "data": weightlifting_service.get_weekly_grid(
            user_id=principal.id,
            week_start=week_start,
        ),
    }


@router.put("/entries")
def upsert_entry(
    request: WeightliftingEntryUpsert,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": weightlifting_service.upsert_entry(
                user_id=principal.id,
                exercise_id=str(request.exercise_id),
                week_start=request.week_start,
                workout_day_slot=request.workout_day_slot,
                workout_date=request.workout_date,
                weight=request.weight,
                reps=request.reps,
                sets=request.sets,
                notes=request.notes,
                completed=request.completed,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.patch("/entries/{entry_id}")
def update_entry(
    entry_id: str,
    request: WeightliftingEntryUpdate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": weightlifting_service.update_entry(
                principal.id,
                entry_id,
                **request.model_dump(exclude_unset=True),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.delete("/entries")
def clear_entry(
    request: WeightliftingEntryClear,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": weightlifting_service.clear_entry(
                user_id=principal.id,
                exercise_id=str(request.exercise_id),
                week_start=request.week_start,
                workout_day_slot=request.workout_day_slot,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/exercises/{exercise_id}/history")
def get_exercise_history(
    exercise_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": weightlifting_service.get_exercise_history(
                user_id=principal.id,
                exercise_id=exercise_id,
                limit=limit,
                offset=offset,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)

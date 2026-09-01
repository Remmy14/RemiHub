from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.core.auth import AuthenticatedPrincipal, require_current_principal
from backend.models.fitness_models import (
    GarminActivitySelectionRequest,
    LiftingTemplateExercisesReplace,
    PlanInstanceCleanupRequest,
    PlanInstantiateRequest,
    PlanTemplateCreate,
    PlanTemplateItemsReplace,
    PlanTemplateUpdate,
    RecurringSeriesRequest,
    ScheduledWorkoutCreate,
    ScheduledWorkoutTemplateReplace,
    WorkoutCompleteRequest,
    WorkoutRescheduleRequest,
    WorkoutTemplateCreate,
    WorkoutTemplateUpdate,
)
from backend.services import fitness_service


router = APIRouter(prefix="/fitness", tags=["Fitness"])


def _handle_service_error(exc: ValueError) -> HTTPException:
    if isinstance(exc, fitness_service.FitnessNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, fitness_service.FitnessConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/workout-templates")
def list_workout_templates(
    include_archived: bool = False,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    return {
        "success": True,
        "data": fitness_service.list_workout_templates(
            user_id=principal.id,
            include_archived=include_archived,
        ),
    }


@router.post("/workout-templates", status_code=status.HTTP_201_CREATED)
def create_workout_template(
    request: WorkoutTemplateCreate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.create_workout_template(
                user_id=principal.id,
                name=request.name,
                workout_type=request.type.value,
                notes=request.notes,
                planned_distance_miles=request.planned_distance_miles,
                exercises=[item.model_dump(mode="json") for item in request.exercises],
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/workout-templates/{template_id}")
def get_workout_template(
    template_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.get_workout_template(
                user_id=principal.id,
                template_id=template_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/workout-templates/{template_id}/completed-workouts")
def list_completed_workouts_for_template(
    template_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.list_completed_workouts_for_template(
                user_id=principal.id,
                template_id=template_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.patch("/workout-templates/{template_id}")
def update_workout_template(
    template_id: str,
    request: WorkoutTemplateUpdate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.update_workout_template(
                principal.id,
                template_id,
                **request.model_dump(mode="json", exclude_unset=True),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/workout-templates/{template_id}/archive")
def archive_workout_template(
    template_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.set_workout_template_active(
                user_id=principal.id,
                template_id=template_id,
                active=False,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/workout-templates/{template_id}/restore")
def restore_workout_template(
    template_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.set_workout_template_active(
                user_id=principal.id,
                template_id=template_id,
                active=True,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.put("/workout-templates/{template_id}/lifting-exercises")
def replace_lifting_template_exercises(
    template_id: str,
    request: LiftingTemplateExercisesReplace,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.replace_lifting_template_exercises(
                user_id=principal.id,
                template_id=template_id,
                exercises=[item.model_dump(mode="json") for item in request.exercises],
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/plan-templates")
def list_plan_templates(
    include_archived: bool = False,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    return {
        "success": True,
        "data": fitness_service.list_plan_templates(
            user_id=principal.id,
            include_archived=include_archived,
        ),
    }


@router.post("/plan-templates", status_code=status.HTTP_201_CREATED)
def create_plan_template(
    request: PlanTemplateCreate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.create_plan_template(
                user_id=principal.id,
                name=request.name,
                notes=request.notes,
                items=[item.model_dump(mode="json") for item in request.items],
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/plan-templates/{plan_template_id}")
def get_plan_template(
    plan_template_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.get_plan_template(
                user_id=principal.id,
                plan_template_id=plan_template_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.patch("/plan-templates/{plan_template_id}")
def update_plan_template(
    plan_template_id: str,
    request: PlanTemplateUpdate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.update_plan_template(
                principal.id,
                plan_template_id,
                **request.model_dump(mode="json", exclude_unset=True),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/plan-templates/{plan_template_id}/archive")
def archive_plan_template(
    plan_template_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.set_plan_template_active(
                user_id=principal.id,
                plan_template_id=plan_template_id,
                active=False,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/plan-templates/{plan_template_id}/restore")
def restore_plan_template(
    plan_template_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.set_plan_template_active(
                user_id=principal.id,
                plan_template_id=plan_template_id,
                active=True,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.put("/plan-templates/{plan_template_id}/items")
def replace_plan_template_items(
    plan_template_id: str,
    request: PlanTemplateItemsReplace,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.replace_plan_template_items(
                user_id=principal.id,
                plan_template_id=plan_template_id,
                items=[item.model_dump(mode="json") for item in request.items],
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/plan-templates/{plan_template_id}/instances", status_code=status.HTTP_201_CREATED)
def instantiate_plan_template(
    plan_template_id: str,
    request: PlanInstantiateRequest,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.instantiate_plan_template(
                user_id=principal.id,
                plan_template_id=plan_template_id,
                start_date=request.start_date,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/plan-instances")
def list_plan_instances(
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    return {
        "success": True,
        "data": fitness_service.list_plan_instances(user_id=principal.id),
    }


@router.get("/plan-instances/current")
def get_current_plan_instance(
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    return {
        "success": True,
        "data": fitness_service.get_current_plan_instance(user_id=principal.id),
    }


@router.get("/plan-instances/{plan_instance_id}")
def get_plan_instance(
    plan_instance_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.get_plan_instance(
                user_id=principal.id,
                instance_id=plan_instance_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/plan-instances/{plan_instance_id}/complete")
def complete_plan_instance(
    plan_instance_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.complete_plan_instance(
                user_id=principal.id,
                instance_id=plan_instance_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/plan-instances/{plan_instance_id}/remove-unstarted")
def remove_unstarted_plan_instance(
    plan_instance_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.remove_unstarted_plan_instance(
                user_id=principal.id,
                instance_id=plan_instance_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/plan-instances/{plan_instance_id}/remove-remaining")
def remove_remaining_plan_workouts(
    plan_instance_id: str,
    request: PlanInstanceCleanupRequest | None = None,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.remove_remaining_plan_workouts(
                user_id=principal.id,
                instance_id=plan_instance_id,
                from_date=request.from_date if request else None,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/scheduled-workouts")
def list_scheduled_workouts(
    start_date: date = Query(...),
    end_date: date = Query(...),
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.list_scheduled_workouts(
                user_id=principal.id,
                start_date=start_date,
                end_date=end_date,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/training-calendar")
def training_calendar(
    start_date: date = Query(...),
    end_date: date = Query(...),
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.training_calendar(
                user_id=principal.id,
                start_date=start_date,
                end_date=end_date,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/today")
def today_workouts(
    target_date: date | None = Query(default=None, alias="date"),
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        selected_date = target_date if isinstance(target_date, date) else None
        return {
            "success": True,
            "data": fitness_service.today_workouts(
                user_id=principal.id,
                target_date=selected_date or fitness_service.current_fitness_date(),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/history")
def list_workout_history(
    start_date: date = Query(...),
    end_date: date = Query(...),
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.list_workout_history(
                user_id=principal.id,
                start_date=start_date,
                end_date=end_date,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/recurring-series/preview")
def preview_recurring_series(
    request: RecurringSeriesRequest,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.preview_recurring_series(
                user_id=principal.id,
                workout_template_id=str(request.workout_template_id),
                start_date=request.start_date,
                weekdays=request.weekdays,
                duration_weeks=request.duration_weeks,
                end_date=request.end_date,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/recurring-series", status_code=status.HTTP_201_CREATED)
def create_recurring_series(
    request: RecurringSeriesRequest,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.create_recurring_series(
                user_id=principal.id,
                workout_template_id=str(request.workout_template_id),
                start_date=request.start_date,
                weekdays=request.weekdays,
                duration_weeks=request.duration_weeks,
                end_date=request.end_date,
                idempotency_key=request.idempotency_key,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/recurring-series/{series_id}")
def get_recurring_series(
    series_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.get_recurring_series(
                user_id=principal.id,
                series_id=series_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/recurring-series/{series_id}/remove-remaining")
def remove_remaining_recurring_workouts(
    series_id: str,
    request: PlanInstanceCleanupRequest | None = None,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.remove_remaining_recurring_workouts(
                user_id=principal.id,
                series_id=series_id,
                from_date=request.from_date if request else None,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/scheduled-workouts", status_code=status.HTTP_201_CREATED)
def create_scheduled_workout(
    request: ScheduledWorkoutCreate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.create_scheduled_workout(
                user_id=principal.id,
                workout_template_id=str(request.workout_template_id),
                scheduled_date=request.scheduled_date,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.delete("/scheduled-workouts/{scheduled_workout_id}")
def remove_scheduled_workout(
    scheduled_workout_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.remove_scheduled_workout(
                user_id=principal.id,
                scheduled_workout_id=scheduled_workout_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/scheduled-workouts/{scheduled_workout_id}")
def get_scheduled_workout(
    scheduled_workout_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.get_scheduled_workout(
                user_id=principal.id,
                scheduled_workout_id=scheduled_workout_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/scheduled-workouts/{scheduled_workout_id}/historical-efforts")
def get_historical_efforts(
    scheduled_workout_id: str,
    limit: str | None = Query(default="5", pattern="^(5|10|all)$"),
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.get_historical_efforts(
                user_id=principal.id,
                scheduled_workout_id=scheduled_workout_id,
                limit=limit,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/scheduled-workouts/{scheduled_workout_id}/undo-reschedule")
def undo_reschedule(
    scheduled_workout_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.undo_reschedule(
                user_id=principal.id,
                scheduled_workout_id=scheduled_workout_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/scheduled-workouts/{scheduled_workout_id}/replace-template")
def replace_scheduled_workout_template(
    scheduled_workout_id: str,
    request: ScheduledWorkoutTemplateReplace,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.replace_scheduled_workout_template(
                user_id=principal.id,
                scheduled_workout_id=scheduled_workout_id,
                workout_template_id=str(request.workout_template_id),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/scheduled-workouts/{scheduled_workout_id}/complete")
def complete_scheduled_workout(
    scheduled_workout_id: str,
    request: WorkoutCompleteRequest | None = None,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.complete_scheduled_workout(
                user_id=principal.id,
                scheduled_workout_id=scheduled_workout_id,
                running=(
                    request.running.model_dump(mode="json")
                    if request and request.running
                    else None
                ),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/scheduled-workouts/{scheduled_workout_id}/garmin/complete")
def attempt_garmin_scheduled_workout_completion(
    scheduled_workout_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.attempt_garmin_scheduled_workout_completion(
                user_id=principal.id,
                scheduled_workout_id=scheduled_workout_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/scheduled-workouts/{scheduled_workout_id}/garmin/complete-selection")
def complete_scheduled_workout_with_garmin_activity(
    scheduled_workout_id: str,
    request: GarminActivitySelectionRequest,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.complete_scheduled_workout_with_garmin_activity(
                user_id=principal.id,
                scheduled_workout_id=scheduled_workout_id,
                activity_id=request.activity_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/scheduled-workouts/{scheduled_workout_id}/skip")
def skip_scheduled_workout(
    scheduled_workout_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.skip_scheduled_workout(
                user_id=principal.id,
                scheduled_workout_id=scheduled_workout_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/scheduled-workouts/{scheduled_workout_id}/reschedule")
def reschedule_scheduled_workout(
    scheduled_workout_id: str,
    request: WorkoutRescheduleRequest,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": fitness_service.reschedule_scheduled_workout(
                user_id=principal.id,
                scheduled_workout_id=scheduled_workout_id,
                scheduled_date=request.scheduled_date,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)

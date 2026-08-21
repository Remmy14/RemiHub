from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.core.auth import AuthenticatedPrincipal, require_current_principal
from backend.models.mead_models import (
    MeadBatchCreate,
    MeadBatchUpdate,
    MeadEventCreate,
    MeadGravityReadingCreate,
    MeadRecipeItemCreate,
    MeadRecipeItemsReplace,
    MeadRecipeItemUpdate,
    MeadTaskComplete,
    MeadTaskCreate,
    MeadTaskReschedule,
)
from backend.services import mead_service


router = APIRouter(prefix="/mead", tags=["Mead"])


def _handle_service_error(exc: ValueError) -> HTTPException:
    if isinstance(exc, mead_service.MeadNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, mead_service.MeadConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/batches")
def list_batches(
    include_archived: bool = False,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    return {
        "success": True,
        "data": mead_service.list_batches(
            user_id=principal.id,
            include_archived=include_archived,
        ),
    }


@router.post("/batches", status_code=status.HTTP_201_CREATED)
def create_batch(
    request: MeadBatchCreate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.create_batch(
                user_id=principal.id,
                **request.model_dump(mode="json"),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/batches/{batch_id}")
def get_batch(
    batch_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.get_batch(user_id=principal.id, batch_id=batch_id),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.patch("/batches/{batch_id}")
def update_batch(
    batch_id: str,
    request: MeadBatchUpdate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.update_batch(
                principal.id,
                batch_id,
                **request.model_dump(mode="json", exclude_unset=True),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/batches/{batch_id}/archive")
def archive_batch(
    batch_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.archive_batch(
                user_id=principal.id,
                batch_id=batch_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.put("/batches/{batch_id}/recipe-items")
def replace_recipe_items(
    batch_id: str,
    request: MeadRecipeItemsReplace,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.replace_recipe_items(
                user_id=principal.id,
                batch_id=batch_id,
                items=[item.model_dump(mode="json") for item in request.items],
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post(
    "/batches/{batch_id}/recipe-items",
    status_code=status.HTTP_201_CREATED,
)
def create_recipe_item(
    batch_id: str,
    request: MeadRecipeItemCreate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.create_recipe_item(
                user_id=principal.id,
                batch_id=batch_id,
                **request.model_dump(mode="json"),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.patch("/recipe-items/{item_id}")
def update_recipe_item(
    item_id: str,
    request: MeadRecipeItemUpdate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.update_recipe_item(
                principal.id,
                item_id,
                **request.model_dump(mode="json", exclude_unset=True),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.delete("/recipe-items/{item_id}")
def delete_recipe_item(
    item_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.delete_recipe_item(
                user_id=principal.id,
                item_id=item_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/batches/{batch_id}/timeline")
def get_timeline(
    batch_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.get_timeline(
                user_id=principal.id,
                batch_id=batch_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/batches/{batch_id}/events", status_code=status.HTTP_201_CREATED)
def add_event(
    batch_id: str,
    request: MeadEventCreate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.add_event(
                user_id=principal.id,
                batch_id=batch_id,
                **request.model_dump(mode="json"),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post(
    "/batches/{batch_id}/gravity-readings",
    status_code=status.HTTP_201_CREATED,
)
def add_gravity_reading(
    batch_id: str,
    request: MeadGravityReadingCreate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.add_gravity_reading(
                user_id=principal.id,
                batch_id=batch_id,
                event_at=request.event_at,
                gravity=request.gravity,
                notes=request.notes,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.get("/batches/{batch_id}/tasks")
def list_tasks(
    batch_id: str,
    status_filter: str | None = Query(default=None, alias="status"),
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.list_tasks(
                user_id=principal.id,
                batch_id=batch_id,
                status=status_filter,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/batches/{batch_id}/tasks", status_code=status.HTTP_201_CREATED)
def create_task(
    batch_id: str,
    request: MeadTaskCreate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.create_task(
                user_id=principal.id,
                batch_id=batch_id,
                **request.model_dump(mode="json"),
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/tasks/{task_id}/complete")
def complete_task(
    task_id: str,
    request: MeadTaskComplete | None = None,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        payload = request.model_dump(mode="json") if request else {}
        return {
            "success": True,
            "data": mead_service.complete_task(
                user_id=principal.id,
                task_id=task_id,
                **payload,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/tasks/{task_id}/reschedule")
def reschedule_task(
    task_id: str,
    request: MeadTaskReschedule,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.reschedule_task(
                user_id=principal.id,
                task_id=task_id,
                due_at=request.due_at,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(
    task_id: str,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {
            "success": True,
            "data": mead_service.cancel_task(
                user_id=principal.id,
                task_id=task_id,
            ),
        }
    except ValueError as exc:
        raise _handle_service_error(exc)

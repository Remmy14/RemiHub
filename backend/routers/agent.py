from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from backend.core.auth import AuthenticatedPrincipal, require_admin_principal
from backend.models.agent_models import (
    AgentCardCreate,
    AgentDecisionRequest,
    AgentMessageCreate,
)
from backend.services import agent_service


router = APIRouter(
    prefix="/agent",
    tags=["Agent"],
    dependencies=[Depends(require_admin_principal)],
)


def _raise_http_error(exc: Exception) -> None:
    if isinstance(exc, agent_service.AgentCardNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    if isinstance(exc, agent_service.AgentConflictError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    raise exc


@router.post("/cards", status_code=status.HTTP_201_CREATED)
def create_card(
    request: AgentCardCreate,
    principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    try:
        card = agent_service.create_card(
            title=request.title,
            description=request.description,
            created_by=principal.id,
            client_message_id=(
                str(request.client_message_id)
                if request.client_message_id is not None
                else None
            ),
        )
    except Exception as exc:
        _raise_http_error(exc)

    return {"success": True, "data": card}


@router.get("/cards")
def list_cards(
    include_closed: bool = False,
    _principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    cards = agent_service.list_cards(include_closed=include_closed)
    return {"success": True, "data": cards}


@router.get("/cards/{card_id}")
def get_card(
    card_id: UUID,
    _principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    try:
        card = agent_service.get_card(str(card_id))
    except Exception as exc:
        _raise_http_error(exc)

    return {"success": True, "data": card}


@router.post("/cards/{card_id}/messages")
def add_follow_up(
    card_id: UUID,
    request: AgentMessageCreate,
    principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    try:
        card = agent_service.add_follow_up(
            card_id=str(card_id),
            content=request.content,
            created_by=principal.id,
            client_message_id=(
                str(request.client_message_id)
                if request.client_message_id is not None
                else None
            ),
        )
    except Exception as exc:
        _raise_http_error(exc)

    return {"success": True, "data": card}


@router.post("/cards/{card_id}/approve-implementation")
def approve_implementation(
    card_id: UUID,
    request: AgentDecisionRequest,
    principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    try:
        card = agent_service.approve_implementation(
            card_id=str(card_id),
            approved_by=principal.id,
            notes=request.notes,
        )
    except Exception as exc:
        _raise_http_error(exc)

    return {"success": True, "data": card}


@router.post("/cards/{card_id}/approve-deployment")
def approve_deployment(
    card_id: UUID,
    request: AgentDecisionRequest,
    principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    try:
        card = agent_service.approve_deployment(
            card_id=str(card_id),
            approved_by=principal.id,
            notes=request.notes,
        )
    except Exception as exc:
        _raise_http_error(exc)

    return {"success": True, "data": card}


@router.post("/cards/{card_id}/cancel")
def cancel_card(
    card_id: UUID,
    request: AgentDecisionRequest,
    principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    try:
        card = agent_service.cancel_card(
            card_id=str(card_id),
            cancelled_by=principal.id,
            notes=request.notes,
        )
    except Exception as exc:
        _raise_http_error(exc)

    return {"success": True, "data": card}


@router.post("/cards/{card_id}/close")
def close_card(
    card_id: UUID,
    request: AgentDecisionRequest,
    principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    try:
        card = agent_service.close_card(
            card_id=str(card_id),
            closed_by=principal.id,
            notes=request.notes,
        )
    except Exception as exc:
        _raise_http_error(exc)

    return {"success": True, "data": card}

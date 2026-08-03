from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from backend.core.auth import AuthenticatedPrincipal, require_admin_principal
from backend.models.agent_models import (
    AgentCardCreate,
    AgentCardListResponse,
    AgentCardResponse,
    AgentDecisionRequest,
    AgentErrorResponse,
    AgentGitHubSyncRetryRequest,
    AgentMessageCreate,
)
from backend.services import agent_service


AUTH_ERROR_RESPONSES = {
    status.HTTP_401_UNAUTHORIZED: {"model": AgentErrorResponse},
    status.HTTP_403_FORBIDDEN: {"model": AgentErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": AgentErrorResponse},
}
CARD_ERROR_RESPONSES = {
    **AUTH_ERROR_RESPONSES,
    status.HTTP_400_BAD_REQUEST: {"model": AgentErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": AgentErrorResponse},
    status.HTTP_409_CONFLICT: {"model": AgentErrorResponse},
}


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


@router.post(
    "/cards",
    status_code=status.HTTP_201_CREATED,
    response_model=AgentCardResponse,
    responses={
        **AUTH_ERROR_RESPONSES,
        status.HTTP_400_BAD_REQUEST: {"model": AgentErrorResponse},
        status.HTTP_409_CONFLICT: {"model": AgentErrorResponse},
    },
)
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


@router.get(
    "/cards",
    response_model=AgentCardListResponse,
    responses=AUTH_ERROR_RESPONSES,
)
def list_cards(
    include_closed: bool = False,
    _principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    cards = agent_service.list_cards(include_closed=include_closed)
    return {"success": True, "data": cards}


@router.get(
    "/cards/{card_id}",
    response_model=AgentCardResponse,
    responses={
        **AUTH_ERROR_RESPONSES,
        status.HTTP_404_NOT_FOUND: {"model": AgentErrorResponse},
    },
)
def get_card(
    card_id: UUID,
    _principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    try:
        card = agent_service.get_card(str(card_id))
    except Exception as exc:
        _raise_http_error(exc)

    return {"success": True, "data": card}


@router.post(
    "/cards/{card_id}/messages",
    response_model=AgentCardResponse,
    responses=CARD_ERROR_RESPONSES,
)
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


@router.post(
    "/cards/{card_id}/approve-implementation",
    response_model=AgentCardResponse,
    responses=CARD_ERROR_RESPONSES,
)
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


@router.post(
    "/cards/{card_id}/approve-deployment",
    response_model=AgentCardResponse,
    responses=CARD_ERROR_RESPONSES,
)
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


@router.post(
    "/cards/{card_id}/retry",
    response_model=AgentCardResponse,
    responses=CARD_ERROR_RESPONSES,
)
def retry_card(
    card_id: UUID,
    request: AgentDecisionRequest,
    principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    try:
        card = agent_service.retry_card(
            card_id=str(card_id),
            requested_by=principal.id,
            notes=request.notes,
        )
    except Exception as exc:
        _raise_http_error(exc)

    return {"success": True, "data": card}


@router.post(
    "/cards/{card_id}/deployments/{deployment_run_id}/retry-github-sync",
    response_model=AgentCardResponse,
    responses=CARD_ERROR_RESPONSES,
)
def retry_deployment_github_sync(
    card_id: UUID,
    deployment_run_id: UUID,
    request: AgentGitHubSyncRetryRequest,
    principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    try:
        card = agent_service.retry_deployment_github_sync(
            card_id=str(card_id),
            deployment_run_id=str(deployment_run_id),
            requested_by=principal.id,
            notes=request.notes,
        )
    except Exception as exc:
        _raise_http_error(exc)

    return {"success": True, "data": card}


@router.post(
    "/cards/{card_id}/cancel",
    response_model=AgentCardResponse,
    responses=CARD_ERROR_RESPONSES,
)
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


@router.post(
    "/cards/{card_id}/close",
    response_model=AgentCardResponse,
    responses=CARD_ERROR_RESPONSES,
)
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

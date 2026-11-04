from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.core.auth import AuthenticatedPrincipal, require_current_principal
from backend.models.blue_jackets_models import AttendanceUpdate
from backend.services import blue_jackets_service as service, nhl_client


router = APIRouter(prefix="/blue-jackets", tags=["Blue Jackets"])


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, (service.GameNotFound, service.AttendanceRequired)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, service.GameConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, nhl_client.NHLUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/games")
def list_games(
    season: int = Query(...), date_from: date | None = None,
    date_to: date | None = None, opponent: str | None = None,
    attendance: str | None = None,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {"success": True, "data": service.list_games(
            user_id=principal.id, season=season, date_from=date_from,
            date_to=date_to, opponent=opponent, attendance=attendance)}
    except (ValueError, nhl_client.NHLUnavailableError) as exc:
        raise _error(exc) from exc


@router.put("/games/{game_id}/attendance")
def set_attendance(
    game_id: int, request: AttendanceUpdate,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {"success": True, "data": service.set_attendance(
            user_id=principal.id, game_id=game_id, status=request.status.value)}
    except (ValueError, nhl_client.NHLUnavailableError) as exc:
        raise _error(exc) from exc


@router.delete("/games/{game_id}/attendance", status_code=status.HTTP_204_NO_CONTENT)
def remove_attendance(
    game_id: int, principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        service.remove_attendance(user_id=principal.id, game_id=game_id)
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("/games/{game_id}/stats")
def get_game_stats(
    game_id: int, principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {"success": True, "data": service.get_stats(user_id=principal.id, game_id=game_id)}
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/games/{game_id}/stats/refresh")
def refresh_game_stats(
    game_id: int, principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {"success": True, "data": service.refresh_stats(user_id=principal.id, game_id=game_id)}
    except (ValueError, nhl_client.NHLUnavailableError) as exc:
        raise _error(exc) from exc


@router.get("/stats")
def get_summary(
    season: int | None = None,
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    try:
        return {"success": True, "data": service.attendance_summary(user_id=principal.id, season=season)}
    except ValueError as exc:
        raise _error(exc) from exc

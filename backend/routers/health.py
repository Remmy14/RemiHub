from fastapi import APIRouter, Depends, status

from backend.core.auth import require_admin_principal
from backend.models.agent_models import AgentErrorResponse
from backend.models.health_models import ServiceHealthSnapshotResponse
from backend.services import service_health_service


AUTH_ERROR_RESPONSES = {
    status.HTTP_401_UNAUTHORIZED: {"model": AgentErrorResponse},
    status.HTTP_403_FORBIDDEN: {"model": AgentErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": AgentErrorResponse},
}


router = APIRouter(
    prefix="/health",
    tags=["Health"],
    dependencies=[Depends(require_admin_principal)],
)


@router.get(
    "/services",
    response_model=ServiceHealthSnapshotResponse,
    responses=AUTH_ERROR_RESPONSES,
)
def get_service_health_snapshot():
    return service_health_service.get_service_health_snapshot()

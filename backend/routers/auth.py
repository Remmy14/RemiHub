from fastapi import APIRouter, Depends

from backend.core.auth import AuthenticatedPrincipal, require_current_principal


router = APIRouter(prefix="/auth", tags=["authentication"])


@router.get("/me")
def authenticated_user(
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
):
    return {
        "success": True,
        "data": {
            "id": principal.id,
            "email": principal.email,
            "display_name": principal.display_name,
            "role": principal.role,
        },
    }

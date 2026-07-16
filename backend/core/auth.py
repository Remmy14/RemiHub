from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.core.firebase_auth import (
    FirebaseConfigurationError,
    verify_firebase_id_token,
)
from backend.services.auth_service import (
    InactiveUserError,
    UserNotAuthorizedError,
    resolve_authenticated_user,
)


logger = logging.getLogger("remihub.auth")
bearer_scheme = HTTPBearer(auto_error=False)


class AuthMode(str, Enum):
    DISABLED = "disabled"
    TRANSITION = "transition"
    REQUIRED = "required"


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    id: str
    firebase_uid: str
    email: str
    display_name: str | None
    role: str


def get_auth_mode() -> AuthMode:
    configured = os.environ.get("REMIHUB_AUTH_MODE", AuthMode.TRANSITION.value)
    try:
        return AuthMode(configured.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in AuthMode)
        raise RuntimeError(
            f"Invalid REMIHUB_AUTH_MODE={configured!r}; expected one of: {allowed}"
        ) from exc


def _authentication_required() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _invalid_token() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired authentication token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _principal_from_credentials(
    credentials: HTTPAuthorizationCredentials,
) -> AuthenticatedPrincipal:
    if credentials.scheme.lower() != "bearer" or not credentials.credentials.strip():
        raise _invalid_token()

    try:
        decoded_token = verify_firebase_id_token(credentials.credentials)
    except FirebaseConfigurationError as exc:
        logger.error("Firebase authentication is unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        ) from exc
    except Exception as exc:
        logger.warning(
            "Firebase token verification failed (%s)",
            type(exc).__name__,
        )
        raise _invalid_token() from exc

    try:
        user = resolve_authenticated_user(decoded_token)
    except (UserNotAuthorizedError, InactiveUserError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Failed to resolve authenticated RemiHub user")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        ) from exc

    return AuthenticatedPrincipal(
        id=str(user["id"]),
        firebase_uid=user["firebase_uid"],
        email=user["email"],
        display_name=user["display_name"],
        role=user["role"],
    )


def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedPrincipal | None:
    mode = get_auth_mode()

    if mode is AuthMode.DISABLED:
        return None

    if credentials is None:
        if mode is AuthMode.TRANSITION:
            return None
        raise _authentication_required()

    return _principal_from_credentials(credentials)


def require_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedPrincipal:
    if credentials is None:
        raise _authentication_required()
    return _principal_from_credentials(credentials)


def require_admin_principal(
    principal: AuthenticatedPrincipal = Depends(require_current_principal),
) -> AuthenticatedPrincipal:
    if principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )

    return principal


# Kept as a compatibility alias for any existing router imports.
require_app_auth = get_current_principal

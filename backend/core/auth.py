from fastapi import Header, HTTPException, status
import os

REMihub_API_KEY = os.getenv("REMihub_API_KEY")


def require_app_auth(x_remihub_key: str | None = Header(default=None)) -> None:
    if REMihub_API_KEY is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server auth not configured",
        )

    if x_remihub_key != REMihub_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

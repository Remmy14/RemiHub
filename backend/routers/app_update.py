# Python Imports
import logging

# 3rd Party Imports
from fastapi.responses import FileResponse
from fastapi import APIRouter, HTTPException, Query, Depends

# Local Imports
from backend.core.auth import require_app_auth
from backend.services.app_update_service import (
    get_app_release_by_id,
    get_latest_app_release,
    resolve_release_file_path,
)


router = APIRouter(
    prefix="/app-update",
    tags=["app-update"],
    # dependencies=[Depends(require_app_auth)],
)

@router.get("/latest")
def latest_app_update(platform: str = Query(...)):
    release = get_latest_app_release(platform=platform)

    if not release:
        raise HTTPException(
            status_code=404,
            detail=f"No active app release found for platform={platform}",
        )

    package = {
        "success": True,
        "data": {
            **release,
            "download_url": f"/app-update/download/{release['id']}",
        },
    }

    logging.info(f"RETURN PACKAGE: {package}")

    return package


@router.get("/download/{release_id}")
def download_app_release(release_id: int):
    release = get_app_release_by_id(release_id)

    if not release:
        raise HTTPException(
            status_code=404,
            detail=f"Release id={release_id} not found",
        )

    file_path = resolve_release_file_path(release["apk_relative_path"])

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Release file not found on disk: {release['apk_filename']}",
        )

    return FileResponse(
        path=str(file_path),
        filename=release["apk_filename"],
        media_type="application/vnd.android.package-archive",
    )

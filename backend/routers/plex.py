# Python Imports

# 3rd Party Imports
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

# Local Imports
from backend.core.auth import require_current_principal
from backend.services import plex_service

router = APIRouter(prefix="/plex", tags=["Plex"])


@router.post("/addDownload")
def add_download(req: plex_service.DownloadRequest):
    if req.category not in plex_service.CATEGORY_PATHS:
        raise HTTPException(status_code=400, detail="Invalid category")

    try:
        result = plex_service.submit_downloads(req)
    except plex_service.EmptyDownloadUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse(content=result, media_type="application/json")

@router.get("/recentRequests")
def getRecentRequests():
    try:
        data = plex_service.get_recent_download_requests()
        return data

    except Exception as e:
        return JSONResponse(
            content={"success": False, "message": str(e)},
            media_type="application/json"
        )


@router.get("/downloads", dependencies=[Depends(require_current_principal)])
def get_downloads(limit: int = Query(10, ge=1, le=25), offset: int = Query(0, ge=0, le=10000)):
    return plex_service.get_downloads(limit, offset)


@router.get("/downloads/{request_id}", dependencies=[Depends(require_current_principal)])
def get_download(request_id: uuid.UUID):
    result = plex_service.get_downloads(limit=1, request_id=str(request_id))
    if not result["data"]:
        raise HTTPException(status_code=404, detail="Download request not found")
    return {"success": True, "data": result["data"][0], "jd2_available": result["jd2_available"]}


@router.post("/downloads/{request_id}/cleanup", dependencies=[Depends(require_current_principal)])
def cleanup_download(request_id: uuid.UUID):
    try:
        return plex_service.cleanup_download(str(request_id))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

# Python Imports

# 3rd Party Imports
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

# Local Imports
from backend.services import plex_service

router = APIRouter(prefix="/plex", tags=["Plex"])


@router.post("/addDownload")
def add_download(req: plex_service.DownloadRequest):
    if req.category not in plex_service.CATEGORY_PATHS:
        raise HTTPException(status_code=400, detail="Invalid category")

    plex_service.create_crawljob_file(req)

    return JSONResponse(
        content={"success": True, "message": "Download job added."},
        media_type="application/json"
    )

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



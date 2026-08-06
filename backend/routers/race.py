# Python Imports

# 3rd Party Imports
from fastapi import APIRouter, Body, Depends, Query

# Local Imports
from backend.core.auth import AuthenticatedPrincipal, require_admin_principal
from backend.services.race import race_service

# Declare our Race module router
router = APIRouter(prefix="/race", tags=["Race"])

PUBLIC_RACE_API_ROUTES = frozenset(
    {
        ("GET", "/race/getPools"),
        ("GET", "/race/getPoolAssignments"),
        ("GET", "/race/getDraftOrder"),
        ("GET", "/race/getCurrentPick"),
        ("GET", "/race/getRecentPicks"),
        ("GET", "/race/getDraftStatus"),
        ("GET", "/race/getLeaderboard"),
        ("GET", "/race/getStartingGridStatus"),
        ("GET", "/race/getArchives"),
        ("GET", "/race/getArchiveEntries"),
    }
)

ADMIN_RACE_API_ROUTES = frozenset(
    {
        ("POST", "/race/createPool"),
        ("POST", "/race/submitDraftOrder"),
        ("POST", "/race/resetStatus"),
        ("POST", "/race/startDraftNow"),
        ("POST", "/race/submitPick"),
        ("POST", "/race/startRace"),
        ("POST", "/race/stopRace"),
    }
)


# Pool Endpoints
# --------------------------------
@router.get("/getPools")
def get_all_pools():
    return race_service.get_all_pools()

@router.post("/createPool")
def create_pool(
    request: dict = Body(...),
    _principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    name = request.get("name")
    participant_count = request.get("participantCount", 10)
    return race_service.create_pool(name, participant_count)

@router.get("/getPoolAssignments")
def get_pool_assignments(pool_id: int = Query(...)):
    return race_service.load_pool(pool_id)

# --------------------------------

# Draft Endpoints
# --------------------------------
# Handle the pre-race Family Draft order draw
@router.post("/submitDraftOrder")
def submit_draft_order(
    pool_id: int,
    order: list[dict] = Body(...),
    _principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    race_service.reset_draft(pool_id, order)
    return {"success": True, "message": "Draft order initialized."}

@router.post("/resetStatus")
def reset_all_status(
    _principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    race_service.reset_race_to_square_one()

@router.get("/getDraftOrder")
def get_draft_order(pool_id: int):
    return race_service.get_draft_order_by_pool(pool_id)

@router.post("/startDraftNow")
def start_draft_now(
    pool_id: int,
    _principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    return race_service.start_draft(pool_id)

@router.post("/submitPick")
def submit_pick(
    pool_id: int,
    car_number: str = Body(...),
    _principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    return race_service.submit_pick(pool_id, car_number)

@router.get("/getCurrentPick")
def current_pick(pool_id: int, ):
    state = race_service.get_current_draft_pick_by_pool(pool_id)
    return {
        "pick_number": state["current_pick"],
        "participant": state["participant"]
    }

@router.get("/getRecentPicks")
def get_recent_picks(pool_id: int = Query(...), limit: int = Query(5)):
    return race_service.get_recent_picks(pool_id, limit)

@router.get("/getDraftStatus")
def draft_status(pool_id: int):
    return race_service.get_draft_status(pool_id)
# --------------------------------

# Pool Leaderboard Endpoints
# --------------------------------
@router.get("/getLeaderboard")
def get_leaderboard(pool_id: int = Query(...)):
    return race_service.get_leaderboard(pool_id)

@router.get("/getStartingGridStatus")
def get_grid_status(pool_id: int = Query(...)):
    return race_service.get_starting_grid_status(pool_id)

# Start the race
@router.post("/startRace")
def start_race(
    _principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    race_service.set_race_draft_status(status="RACE_ACTIVE")
    return {"success": True, "message": "Race tracking is now active."}

# Start the race
@router.post("/stopRace")
def stop_race(
    _principal: AuthenticatedPrincipal = Depends(require_admin_principal),
):
    race_service.set_race_draft_status("RACE_COMPLETED")
    return {"success": True, "message": "Race tracking is now completed."}
# --------------------------------

# Archive Endpoints
# --------------------------------
@router.get("/getArchives")
def get_archives():
    return {
        "success": True,
        "archives": race_service.get_archives(),
    }


@router.get("/getArchiveEntries")
def get_archive_entries(archive_id: int = Query(...)):
    return race_service.get_archive_entries(archive_id)
# --------------------------------

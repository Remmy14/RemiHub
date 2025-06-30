# Python Imports

# 3rd Party Imports
from fastapi import APIRouter, Body, Query

# Local Imports
from backend.services import race_service

# Declare our Race module router
router = APIRouter(prefix="/race", tags=["Race"])

# Pool Endpoints
# --------------------------------
@router.get("/getPools")
def get_all_pools():
    return race_service.get_all_pools()

@router.post("/createPool")
def create_pool(request: dict = Body(...)):
    name = request.get("name")
    participant_count = request.get("participantCount", 10)
    return race_service.create_pool(name, participant_count)
# --------------------------------

# Draft Endpoints
# --------------------------------
# Handle the pre-race Family Draft order draw
@router.post("/submitDraftOrder")
def submit_draft_order(pool_id: int, order: list[dict] = Body(...)):
    race_service.reset_draft(pool_id, order)
    return {"success": True, "message": "Draft order initialized."}

@router.post("/resetStatus")
def reset_all_status():
    race_service.reset_race_to_square_one()

@router.get("/getDraftOrder")
def get_draft_order(pool_id: int):
    return race_service.get_draft_order_by_pool(pool_id)

@router.post("/startDraftNow")
def start_draft_now(pool_id: int, ):
    return race_service.start_draft(pool_id)

@router.post("/submitPick")
def submit_pick(pool_id: int, car_number: str = Body(...)):
    return race_service.submit_pick(pool_id, car_number)

@router.get("/getCurrentPick")
def current_pick(pool_id: int, ):
    state = race_service.get_current_draft_pick_by_pool(pool_id)
    return {
        "pick_number": state["current_pick"],
        "participant": state["participant"]
    }

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
def start_race():
    race_service.set_race_draft_status("RACE_ACTIVE")
    return {"success": True, "message": "Race tracking is now active."}

# Start the race
@router.post("/stopRace")
def stop_race():
    race_service.set_race_draft_status("RACE_COMPLETED")
    return {"success": True, "message": "Race tracking is now completed."}
# --------------------------------

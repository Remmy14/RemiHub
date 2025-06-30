# Python Imports
import asyncio
from datetime import datetime

# Local Imports
from backend import draft
from backend import leaderboard
from backend import pool


LEADERBOARD_UPDATE_DELAY = 15
# Begin to update the leaderboard on a regular basis
async def update_leaderboard_loop():
    while True:
        if should_fetch_leaderboard():
            # It's time to update our leaderboard
            try:
                await update_leaderboard_and_standings()
            except Exception as e:
                print("Error updating leaderboard:", e)

        # Sleep
        await asyncio.sleep(LEADERBOARD_UPDATE_DELAY)


# Expose Pool Functionalities
def save_pool(pool_id: int, pool_data: dict) -> dict:
    """
    Save a full pool assignment to the database.
    """
    pool.save_pool_to_db(pool_id, pool_data)
    return {"success": True, "message": "Pool assignments saved."}

def load_pool(pool_id: int) -> dict:
    """
    Load all driver assignments in a pool.
    """
    return pool.load_pool_from_db(pool_id)

def get_all_pools() -> list[dict]:
    """
    Retrieve metadata for all existing pools.
    """
    return pool.get_all_pools()

# Expose Draft functionalities
def submit_pick(pool_id: int, car_number: str) -> dict:
    success = draft.make_pick(pool_id, car_number)
    return {
        "success": success,
        "message": "Driver assigned." if success else "Driver already taken or invalid."
    }

def reset_race_to_square_one():
    draft.reset_draft_to_square_one()

def get_draft_order_by_pool(pool_id):
    return draft.get_draft_order(pool_id)

def start_draft(pool_id: int):
    draft.start_draft(pool_id)

def get_current_draft_pick_by_pool(pool_id: int):
    return draft.get_current_draft_pick(pool_id)

def get_draft_status(pool_id: int):
    return draft.get_draft_status(pool_id)

def get_starting_grid_status(pool_id: int):
    return draft.get_starting_grid_status(pool_id)

def set_race_draft_status(status: str, pool_id: int):
    return draft.set_race_draft_status(status, pool_id)

# Expose Race Leaderboard Functionalities
def get_leaderboard(pool_id: int):
    try:
        standings, updated_at = leaderboard.load_pool_standings_from_db(pool_id)
        if not standings:
            return {
                "success": True,
                "message": "Standings not available yet.",
                "updatedAt": updated_at or datetime.now().isoformat(),
            }
        return {
            "success": True,
            "standings": standings,
            "updatedAt": updated_at,
        }
    except:
        return {
            "success": False,
            "message": "Error getting Pool Cache.",
            "updatedAt": datetime.now().isoformat(),
        }

def should_fetch_leaderboard() -> bool:
    return leaderboard.should_fetch_leaderboard()

async def update_leaderboard_and_standings():
    await leaderboard.save_leaderboard_to_db()

    # Update standings for all pools
    pools = pool.get_all_pools()
    for _pool in pools:
        leaderboard.save_pool_standings_to_db(_pool['id'])


# Python Imports
import asyncio
from contextlib import asynccontextmanager
import sys
import threading

# 3rd Party Imports
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles

# Local Imports
from backend.services import race_service
from backend.database.database import get_db_conn, put_db_conn
from backend.routers import race, pool, plex, fieldwatch, auto_logins
from backend.tasks import swimming_pool_monitor, plex_dl_monitor, notification_worker, field_status_watcher, jury_watch

TEST_MODE = False

# Add the main directory to the path for some reason
sys.path.append('M:/Q_Drive/Projects/RemiHub/')

# Define the lifespan of the app
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background tasks
    if not TEST_MODE:
        # 1 - Kick off our Race Day family pool monitor
        asyncio.create_task(race_service.update_leaderboard_loop())

        threads = [
            notification_worker.run_notification_worker,
            swimming_pool_monitor.run_pool_monitor,
            plex_dl_monitor.main,
            field_status_watcher.run_monitor,
            jury_watch.run_monitor,
        ]

        # 0 - Kick off the Threads
        for thread in threads:
            threading.Thread(target=thread, daemon=True).start()

    yield
    # (Optional) Cleanup tasks go here

# Create the API and add the routers
app = FastAPI(lifespan=lifespan)
routers = [
    race.router,
    pool.router,
    plex.router,
    fieldwatch.router,
    auto_logins.router,
]

for router in routers:
    app.include_router(router)

# Allow connections from anywhere
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://192.168.1.106:5173"],  # Allows connections from React server to restrict
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount our images directory
app.mount('/static', StaticFiles(directory='M:/Q_Drive/Projects/RemiHub/backend/static/'), name='static')

# Serve React build at /race
race_path = "M:/Q_Drive/Projects/RemiHub/frontend-web/dist/"
app.mount("/race", StaticFiles(directory=race_path, html=True), name="race")

def db_dependency():
    conn = get_db_conn()
    try:
        yield conn
    finally:
        put_db_conn(conn)

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)  # No Content


if __name__ == '__main__':
    pass

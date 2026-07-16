# Python Imports
import asyncio
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import logging
from pathlib import Path
import threading

# 3rd Party Imports
from fastapi import Depends, FastAPI
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles

# Local Imports
from backend.services.race import race_service
from backend.core.auth import AuthMode, get_auth_mode, get_current_principal
from backend.database.database import get_db_conn, put_db_conn
from backend.routers import (
        app_update,
        auth,
        auto_logins,
        autographs,
        fieldwatch,
        finance,
        notifications,
        plex,
        pool,
        race,
        rh_storage,
        speedtest,
        weather,
        kids_investing,
        spotify,
    )
from backend.tasks import (
        swimming_pool_monitor,
        plex_dl_monitor,
        notification_worker,
        field_status_watcher,
        # jury_watch,
        speed_test_worker,
        weather_monitor,
        finance_worker,
        kids_investing_worker,
    )

TEST_MODE = False
logger = logging.getLogger("remihub.main")

# Add the main directory to the path for some reason
BASE_DIR = Path(__file__).resolve().parent.parent  # /opt/remihub
STATIC_DIR = BASE_DIR / 'backend' / 'static'
WEB_DIST_DIR = BASE_DIR / 'frontend-web' / 'dist'

# Load environment variables
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ENV_PATH = _PROJECT_ROOT / "config" / "remihub.env"

load_dotenv(dotenv_path=_ENV_PATH, override=False)

# Define the lifespan of the app
@asynccontextmanager
async def lifespan(app: FastAPI):
    auth_mode = get_auth_mode()
    logger.info("RemiHub API authentication mode: %s", auth_mode.value)
    if auth_mode is AuthMode.TRANSITION:
        logger.warning(
            "API authentication is in transition mode; requests without credentials are still permitted"
        )

    # Start background tasks
    if not TEST_MODE:
        # 1 - Kick off our Race Day family pool monitor
        asyncio.create_task(race_service.update_leaderboard_loop())

        threads = [
            notification_worker.run_notification_worker,
            swimming_pool_monitor.run_pool_monitor,
            plex_dl_monitor.main,
            field_status_watcher.run_monitor,
            # jury_watch.run_monitor,
            speed_test_worker.run_monitor,
            weather_monitor.run_weather_monitor,
            finance_worker.run_finance_worker,
            kids_investing_worker.run_kids_investing_worker,
        ]

        # 0 - Kick off the Threads
        for thread in threads:
            threading.Thread(target=thread, daemon=True).start()

    yield
    # (Optional) Cleanup tasks go here

# Create the API and add the routers
app = FastAPI(lifespan=lifespan)

# Authentication verification has its own strict dependency so it remains
# testable while the rest of the API is temporarily in transition mode.
app.include_router(auth.router)

routers = [
    race.router,
    pool.router,
    plex.router,
    fieldwatch.router,
    auto_logins.router,
    notifications.router,
    app_update.router,
    speedtest.router,
    autographs.router,
    weather.router,
    rh_storage.router,
    finance.router,
    kids_investing.router,
    spotify.router,
]

for router in routers:
    app.include_router(
        router,
        dependencies=[Depends(get_current_principal)],
    )

# Allow connections from anywhere
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://192.168.1.106:5173"],  # Allows connections from React server to restrict
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount our images directory
app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')

# Serve React pages for race and draft
@app.get("/race/draft", include_in_schema=False)
@app.get("/race/draft/{full_path:path}", include_in_schema=False)
async def serve_race_draft(full_path: str = ""):
    return FileResponse(WEB_DIST_DIR / "index.html")

@app.get("/storage", include_in_schema=False)
@app.get("/storage/{full_path:path}", include_in_schema=False)
async def serve_storage_status(full_path: str = ""):
    return FileResponse(WEB_DIST_DIR / "index.html")

app.mount('/race', StaticFiles(directory=str(WEB_DIST_DIR), html=True), name='race')

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

"""Periodic lightweight schedules and attended-only detail retries."""
from __future__ import annotations

from datetime import date
import logging
import time

from backend.services import blue_jackets_service as service, nhl_client


logger = logging.getLogger("remihub.blue_jackets_worker")


def synchronize_schedules_once(today: date | None = None) -> list[dict]:
    current = nhl_client.current_season(today)
    results = []
    for year in range(2021, current // 10000 + 1):
        season = nhl_client.season_for_year(year)
        try:
            result = service.synchronize_schedule(season)
            results.append(result)
            logger.info("Blue Jackets schedule synced: season=%s entries=%s distinct=%s duplicates=%s",
                        season, result["eligible_entries"], result["distinct_games"],
                        len(result["duplicate_game_ids"]))
        except (nhl_client.NHLUnavailableError, nhl_client.NHLDataError):
            logger.exception("Blue Jackets schedule synchronization failed: %s", season)
    return results


def synchronize_attended_once() -> int:
    processed = 0
    for row in service.due_attended_games():
        try:
            service.refresh_stats(user_id=str(row["user_id"]), game_id=row["nhl_game_id"], force=False)
            processed += 1
        except (nhl_client.NHLUnavailableError, nhl_client.NHLDataError, service.AttendanceRequired):
            logger.exception("Blue Jackets attended game retrieval deferred: %s", row["nhl_game_id"])
    return processed


def run_blue_jackets_worker() -> None:
    logger.info("Blue Jackets worker started")
    last_schedule_day = None
    while True:
        try:
            today = date.today()
            if last_schedule_day != today:
                synced = synchronize_schedules_once(today)
                if len(synced) == nhl_client.current_season(today) // 10000 - 2021 + 1:
                    last_schedule_day = today
            processed = synchronize_attended_once()
            if processed:
                logger.info("Blue Jackets attended details checked: %s", processed)
        except Exception:
            logger.exception("Blue Jackets worker cycle failed")
        time.sleep(30 * 60)

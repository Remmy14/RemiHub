"""Small, bounded client for the public NHL schedule and game landing API."""
from __future__ import annotations

from datetime import date
import logging

import requests


BASE_URL = "https://api-web.nhle.com/v1"
FIRST_SEASON = 20212022
logger = logging.getLogger("remihub.nhl")


class NHLDataError(ValueError):
    pass


class NHLUnavailableError(RuntimeError):
    pass


def season_for_year(year: int) -> int:
    if year < 2021 or year > 2098:
        raise NHLDataError("Season must begin in 2021 or later")
    return year * 10000 + year + 1


def validate_season(season: int) -> int:
    if (season < FIRST_SEASON or season_for_year(season // 10000) != season
            or season // 10000 > current_season() // 10000 + 1):
        raise NHLDataError("Invalid NHL season; use YYYYyyyy from 20212022 onward")
    return season


def current_season(today: date | None = None) -> int:
    day = today or date.today()
    return season_for_year(day.year if day.month >= 7 else day.year - 1)


def _get_json(path: str) -> dict:
    try:
        response = requests.get(f"{BASE_URL}{path}", timeout=15)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise NHLUnavailableError("NHL API request failed") from exc
    if not isinstance(payload, dict):
        raise NHLUnavailableError("NHL API returned an unexpected response")
    return payload


def get_schedule(season: int) -> dict:
    return _get_json(f"/club-schedule-season/CBJ/{validate_season(season)}")


def get_landing(game_id: int) -> dict:
    if game_id <= 0:
        raise NHLDataError("Invalid NHL game ID")
    return _get_json(f"/gamecenter/{game_id}/landing")


def _team_abbrev(value: object) -> str | None:
    if isinstance(value, dict):
        abbrev = value.get("abbrev")
        if isinstance(abbrev, str):
            return abbrev.upper()
    return None


def parse_schedule(payload: dict, season: int) -> tuple[list[dict], list[int]]:
    """Return eligible games and duplicate NHL IDs, retaining the last entry."""
    validate_season(season)
    entries = payload.get("games")
    if not isinstance(entries, list):
        raise NHLDataError("NHL schedule is missing games")
    games: dict[int, dict] = {}
    duplicates: list[int] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("gameType") not in (2, 3):
            continue
        try:
            game_id = int(entry["id"])
            actual_season = int(entry["season"])
            game_date = date.fromisoformat(entry["gameDate"])
            home = _team_abbrev(entry.get("homeTeam"))
            away = _team_abbrev(entry.get("awayTeam"))
            state = entry["gameState"]
        except (KeyError, ValueError, TypeError) as exc:
            raise NHLDataError("Eligible NHL schedule game has invalid metadata") from exc
        if actual_season != season or (home == "CBJ") == (away == "CBJ") or not isinstance(state, str):
            raise NHLDataError("Eligible NHL schedule game has inconsistent metadata")
        if game_id in games:
            duplicates.append(game_id)
        games[game_id] = {
            "nhl_game_id": game_id,
            "season": season,
            "game_type": entry["gameType"],
            "game_date": game_date,
            "start_time_utc": entry.get("startTimeUTC"),
            "opponent_abbrev": away if home == "CBJ" else home,
            "cbj_home": home == "CBJ",
            "game_state": state,
        }
    return list(games.values()), duplicates


def _nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise NHLDataError("NHL game statistic is invalid")
    return value


def parse_landing(payload: dict, game: dict) -> dict:
    """Only OFF with scores is official; unknown result type stays unavailable."""
    try:
        game_id = int(payload["id"])
        season = int(payload["season"])
        game_type = int(payload["gameType"])
        state = payload["gameState"]
    except (KeyError, ValueError, TypeError) as exc:
        raise NHLDataError("NHL landing response has invalid identity") from exc
    home = payload.get("homeTeam")
    away = payload.get("awayTeam")
    if (game_id != game["nhl_game_id"] or season != game["season"]
            or game_type != game["game_type"] or game_type not in (2, 3)
            or not isinstance(home, dict) or not isinstance(away, dict)
            or _team_abbrev(home) != ("CBJ" if game["cbj_home"] else game["opponent_abbrev"])
            or _team_abbrev(away) != (game["opponent_abbrev"] if game["cbj_home"] else "CBJ")):
        raise NHLDataError("NHL landing response does not match the scheduled game")
    result = {"game_state": state, "goals_for": None, "goals_against": None,
              "shots_for": None, "shots_against": None, "result": None,
              "complete": False}
    if state != "OFF":
        return result
    cbj = home if game["cbj_home"] else away
    opponent = away if game["cbj_home"] else home
    result["goals_for"] = _nonnegative_int(cbj.get("score"))
    result["goals_against"] = _nonnegative_int(opponent.get("score"))
    result["shots_for"] = _nonnegative_int(cbj.get("sog"))
    result["shots_against"] = _nonnegative_int(opponent.get("sog"))
    if result["goals_for"] is None or result["goals_against"] is None:
        return result
    if result["goals_for"] > result["goals_against"]:
        result["result"] = "W"
    elif result["goals_for"] < result["goals_against"]:
        descriptor = payload.get("periodDescriptor")
        reg_periods = payload.get("regPeriods")
        # These nested keys are optional until real OT/SO payloads are validated.
        period_number = descriptor.get("number") if isinstance(descriptor, dict) else None
        period_type = descriptor.get("periodType") if isinstance(descriptor, dict) else None
        if period_type in ("OT", "SO") or (
            isinstance(period_number, int) and isinstance(reg_periods, int)
            and period_number > reg_periods
        ):
            result["result"] = "OTL"
        elif (isinstance(period_number, int) and isinstance(reg_periods, int)
              and period_number <= reg_periods):
            result["result"] = "L"
        elif period_type == "REG":
            result["result"] = "L"
    result["complete"] = result["result"] is not None
    return result

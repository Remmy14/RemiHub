"""User-owned attendance, shared attended-only detail cache, and aggregates."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging

from backend.database.database import get_db_conn, put_db_conn
from backend.services import nhl_client


logger = logging.getLogger("remihub.blue_jackets")


class GameNotFound(ValueError):
    pass


class AttendanceRequired(ValueError):
    pass


class GameConflict(ValueError):
    pass


def _rows(cur) -> list[dict]:
    names = [item[0] for item in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _one(cur) -> dict | None:
    rows = _rows(cur)
    return rows[0] if rows else None


def _json(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {key: value.isoformat() if isinstance(value, (date, datetime)) else value
            for key, value in row.items()}


def synchronize_schedule(season: int) -> dict:
    season = nhl_client.validate_season(season)
    games, duplicates = nhl_client.parse_schedule(nhl_client.get_schedule(season), season)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            for game in games:
                cur.execute("""
                    INSERT INTO public.blue_jackets_games
                      (nhl_game_id, season, game_type, game_date, start_time_utc,
                       opponent_abbrev, cbj_home, game_state)
                    VALUES (%(nhl_game_id)s, %(season)s, %(game_type)s, %(game_date)s,
                            %(start_time_utc)s, %(opponent_abbrev)s, %(cbj_home)s,
                            %(game_state)s)
                    ON CONFLICT (nhl_game_id) DO UPDATE SET
                      season = EXCLUDED.season, game_type = EXCLUDED.game_type,
                      game_date = EXCLUDED.game_date,
                      start_time_utc = EXCLUDED.start_time_utc,
                      opponent_abbrev = EXCLUDED.opponent_abbrev,
                      cbj_home = EXCLUDED.cbj_home,
                      game_state = EXCLUDED.game_state,
                      updated_at = CURRENT_TIMESTAMP
                """, game)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)
    if duplicates:
        logger.warning("NHL season %s contained duplicate game IDs: %s", season, duplicates)
    return {"season": season, "eligible_entries": len(games) + len(duplicates),
            "distinct_games": len(games), "duplicate_game_ids": duplicates}


def _get_game(cur, game_id: int) -> dict:
    cur.execute("SELECT * FROM public.blue_jackets_games WHERE nhl_game_id = %s", (game_id,))
    game = _one(cur)
    if game is None:
        raise GameNotFound("Eligible Blue Jackets game not found")
    return game


def list_games(*, user_id: str, season: int, date_from: date | None = None,
               date_to: date | None = None, opponent: str | None = None,
               attendance: str | None = None) -> list[dict]:
    nhl_client.validate_season(season)
    if date_from and date_to and date_from > date_to:
        raise ValueError("date_from must be on or before date_to")
    if opponent and (len(opponent) != 3 or not opponent.isalpha()):
        raise ValueError("opponent must be a three-letter abbreviation")
    if attendance not in (None, "none", "going", "attended"):
        raise ValueError("Invalid attendance filter")
    ensure_season_loaded(season)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT game.nhl_game_id, game.season, game.game_type, game.game_date,
                       game.start_time_utc, game.opponent_abbrev, game.cbj_home,
                       game.game_state, attendance.status AS attendance_status,
                       stats.game_state AS detail_state,
                       CASE WHEN stats.fetched_at IS NOT NULL THEN true ELSE false END AS stats_available
                FROM public.blue_jackets_games AS game
                LEFT JOIN public.blue_jackets_attendance AS attendance
                  ON attendance.nhl_game_id = game.nhl_game_id AND attendance.user_id = %s
                LEFT JOIN public.blue_jackets_game_stats AS stats
                  ON stats.nhl_game_id = game.nhl_game_id AND attendance.status = 'attended'
                WHERE game.season = %s
                  AND (%s::date IS NULL OR game.game_date >= %s::date)
                  AND (%s::date IS NULL OR game.game_date <= %s::date)
                  AND (%s::text IS NULL OR game.opponent_abbrev = %s::text)
                  AND (%s::text IS NULL OR COALESCE(attendance.status, 'none') = %s::text)
                ORDER BY game.game_date, game.start_time_utc, game.nhl_game_id
            """, (user_id, season, date_from, date_from, date_to, date_to,
                  opponent.upper() if opponent else None, opponent.upper() if opponent else None,
                  attendance, attendance))
            return [_json(row) for row in _rows(cur)]
    finally:
        put_db_conn(conn)


def ensure_season_loaded(season: int) -> None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM public.blue_jackets_games WHERE season = %s LIMIT 1", (season,))
            if cur.fetchone():
                return
    finally:
        put_db_conn(conn)
    synchronize_schedule(season)


def set_attendance(*, user_id: str, game_id: int, status: str) -> dict:
    if status not in ("going", "attended"):
        raise ValueError("status must be going or attended")
    ensure_game_loaded(game_id)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            game = _get_game(cur, game_id)
            cur.execute("""
                INSERT INTO public.blue_jackets_attendance (user_id, nhl_game_id, status)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, nhl_game_id) DO UPDATE
                SET status = EXCLUDED.status, updated_at = CURRENT_TIMESTAMP
                RETURNING user_id, nhl_game_id, status, created_at, updated_at
            """, (user_id, game_id, status))
            record = _one(cur)
            if status == "going":
                _delete_unowned_stats(cur, game_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)
    start = game["start_time_utc"]
    due_for_details = (start + timedelta(hours=3) <= datetime.now(timezone.utc)
                       if start else game["game_date"] < date.today())
    if status == "attended" and due_for_details:
        try:
            refresh_stats(user_id=user_id, game_id=game_id, force=False)
        except (nhl_client.NHLUnavailableError, nhl_client.NHLDataError) as exc:
            logger.warning("NHL detail retrieval deferred for game %s: %s", game_id, exc)
    return _json(record)


def ensure_game_loaded(game_id: int) -> None:
    if game_id <= 0:
        raise GameNotFound("Eligible Blue Jackets game not found")
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM public.blue_jackets_games WHERE nhl_game_id = %s", (game_id,))
            if cur.fetchone():
                return
    finally:
        put_db_conn(conn)
    # NHL game IDs begin with the season start year; schedule remains the
    # authority for eligibility and identity, never the ID alone.
    year = game_id // 1_000_000
    try:
        season = nhl_client.season_for_year(year)
    except nhl_client.NHLDataError as exc:
        raise GameNotFound("Eligible Blue Jackets game not found") from exc
    synchronize_schedule(season)


def _delete_unowned_stats(cur, game_id: int) -> None:
    cur.execute("""
        DELETE FROM public.blue_jackets_game_stats AS stats
        WHERE stats.nhl_game_id = %s
          AND NOT EXISTS (
            SELECT 1 FROM public.blue_jackets_attendance AS attendance
            WHERE attendance.nhl_game_id = stats.nhl_game_id AND attendance.status = 'attended'
          )
    """, (game_id,))


def remove_attendance(*, user_id: str, game_id: int) -> None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM public.blue_jackets_attendance
                WHERE user_id = %s AND nhl_game_id = %s
                RETURNING nhl_game_id
            """, (user_id, game_id))
            if cur.fetchone() is None:
                raise GameNotFound("Attendance record not found")
            _delete_unowned_stats(cur, game_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def _attended_game(cur, user_id: str, game_id: int, *, lock: bool = False) -> dict:
    cur.execute("""
        SELECT game.* FROM public.blue_jackets_games AS game
        JOIN public.blue_jackets_attendance AS attendance
          ON attendance.nhl_game_id = game.nhl_game_id
        WHERE game.nhl_game_id = %s AND attendance.user_id = %s
          AND attendance.status = 'attended'
    """ + (" FOR UPDATE OF attendance" if lock else ""), (game_id, user_id))
    game = _one(cur)
    if game is None:
        raise AttendanceRequired("Attended game not found")
    return game


def get_stats(*, user_id: str, game_id: int) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            _attended_game(cur, user_id, game_id)
            cur.execute("SELECT * FROM public.blue_jackets_game_stats WHERE nhl_game_id = %s", (game_id,))
            row = _one(cur)
            return _json(row) if row else {"nhl_game_id": game_id, "game_state": "pending",
                                           "goals_for": None, "goals_against": None,
                                           "shots_for": None, "shots_against": None,
                                           "result": None, "fetched_at": None,
                                           "next_retry_at": None, "attempt_count": 0,
                                           "last_error": None}
    finally:
        put_db_conn(conn)


def refresh_stats(*, user_id: str, game_id: int, force: bool = True) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            game = _attended_game(cur, user_id, game_id)
            cur.execute("SELECT fetched_at, next_retry_at, attempt_count FROM public.blue_jackets_game_stats WHERE nhl_game_id = %s", (game_id,))
            old = _one(cur)
    finally:
        put_db_conn(conn)
    now = datetime.now(timezone.utc)
    if not force and old and (old["fetched_at"] or (old["next_retry_at"] and old["next_retry_at"] > now)):
        return get_stats(user_id=user_id, game_id=game_id)
    try:
        parsed = nhl_client.parse_landing(nhl_client.get_landing(game_id), game)
        error = None
    except (nhl_client.NHLUnavailableError, nhl_client.NHLDataError) as exc:
        parsed = None
        error = str(exc)[:300]
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            # A user may remove attendance while the external request is in flight.
            _attended_game(cur, user_id, game_id, lock=True)
            attempts = (old["attempt_count"] if old else 0) + 1
            delay = min(30 * 2 ** min(attempts - 1, 6), 360)
            retry_at = None if parsed and parsed["complete"] else now + timedelta(minutes=delay)
            values = parsed or {"game_state": "error", "goals_for": None,
                                "goals_against": None, "shots_for": None,
                                "shots_against": None, "result": None}
            cur.execute("""
                INSERT INTO public.blue_jackets_game_stats
                  (nhl_game_id, game_state, goals_for, goals_against, shots_for,
                   shots_against, result, fetched_at, next_retry_at, attempt_count, last_error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (nhl_game_id) DO UPDATE SET
                  game_state = EXCLUDED.game_state,
                  goals_for = COALESCE(EXCLUDED.goals_for, blue_jackets_game_stats.goals_for),
                  goals_against = COALESCE(EXCLUDED.goals_against, blue_jackets_game_stats.goals_against),
                  shots_for = COALESCE(EXCLUDED.shots_for, blue_jackets_game_stats.shots_for),
                  shots_against = COALESCE(EXCLUDED.shots_against, blue_jackets_game_stats.shots_against),
                  result = COALESCE(EXCLUDED.result, blue_jackets_game_stats.result),
                  fetched_at = COALESCE(EXCLUDED.fetched_at, blue_jackets_game_stats.fetched_at),
                  next_retry_at = EXCLUDED.next_retry_at,
                  attempt_count = EXCLUDED.attempt_count,
                  last_error = EXCLUDED.last_error,
                  updated_at = CURRENT_TIMESTAMP
            """, (game_id, values["game_state"], values["goals_for"], values["goals_against"],
                  values["shots_for"], values["shots_against"], values["result"],
                  now if parsed and parsed["complete"] else None, retry_at, attempts, error))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)
    if error:
        raise nhl_client.NHLUnavailableError(error)
    return get_stats(user_id=user_id, game_id=game_id)


def due_attended_games(*, now: datetime | None = None, limit: int = 25) -> list[dict]:
    moment = now or datetime.now(timezone.utc)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (game.nhl_game_id) game.nhl_game_id,
                       attendance.user_id
                FROM public.blue_jackets_games AS game
                JOIN public.blue_jackets_attendance AS attendance
                  ON attendance.nhl_game_id = game.nhl_game_id
                 AND attendance.status = 'attended'
                LEFT JOIN public.blue_jackets_game_stats AS stats
                  ON stats.nhl_game_id = game.nhl_game_id
                WHERE (game.start_time_utc <= %s - interval '3 hours'
                       OR (game.start_time_utc IS NULL
                           AND game.game_date < (%s AT TIME ZONE 'UTC')::date))
                  AND stats.fetched_at IS NULL
                  AND (stats.next_retry_at IS NULL OR stats.next_retry_at <= %s)
                ORDER BY game.nhl_game_id, attendance.user_id
                LIMIT %s
            """, (moment, moment, moment, limit))
            return _rows(cur)
    finally:
        put_db_conn(conn)


def _summary(rows: list[dict]) -> dict:
    completed = [r for r in rows if r["result"] in ("W", "L", "OTL")]
    wins = sum(r["result"] == "W" for r in completed)
    losses = sum(r["result"] == "L" for r in completed)
    otl = sum(r["result"] == "OTL" for r in completed)
    def metric(name: str) -> tuple[int | None, int]:
        values = [r[name] for r in completed if r[name] is not None]
        return (sum(values) if values and len(values) == len(completed) else None,
                len(values))
    gf, gf_count = metric("goals_for")
    ga, ga_count = metric("goals_against")
    sf, sf_count = metric("shots_for")
    sa, sa_count = metric("shots_against")
    return {"games_attended": len(completed), "pending_attended_games": len(rows) - len(completed),
            "wins": wins, "regulation_losses": losses, "ot_shootout_losses": otl,
            "win_percentage": round(wins / len(completed) * 100, 2) if completed else None,
            "goals_for": gf, "goals_against": ga,
            "goal_differential": gf - ga if gf_count == ga_count == len(completed) and completed else None,
            "shots_for": sf, "shots_against": sa,
            "averages": {
                "goals_for": round(gf / gf_count, 2) if gf is not None else None,
                "goals_against": round(ga / ga_count, 2) if ga is not None else None,
                "shots_for": round(sf / sf_count, 2) if sf is not None else None,
                "shots_against": round(sa / sa_count, 2) if sa is not None else None,
            },
            "available_game_counts": {"goals_for": gf_count, "goals_against": ga_count,
                                      "shots_for": sf_count, "shots_against": sa_count}}


def attendance_summary(*, user_id: str, season: int | None = None) -> dict:
    if season is not None:
        nhl_client.validate_season(season)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT game.game_type, game.opponent_abbrev, game.season,
                       stats.result, stats.goals_for, stats.goals_against,
                       stats.shots_for, stats.shots_against
                FROM public.blue_jackets_attendance AS attendance
                JOIN public.blue_jackets_games AS game ON game.nhl_game_id = attendance.nhl_game_id
                LEFT JOIN public.blue_jackets_game_stats AS stats
                  ON stats.nhl_game_id = game.nhl_game_id AND stats.fetched_at IS NOT NULL
                WHERE attendance.user_id = %s AND attendance.status = 'attended'
                  AND (%s::integer IS NULL OR game.season = %s::integer)
            """, (user_id, season, season))
            rows = _rows(cur)
    finally:
        put_db_conn(conn)
    opponents = sorted({r["opponent_abbrev"] for r in rows})
    return {"season": season, **_summary(rows),
            "by_opponent": {o: _summary([r for r in rows if r["opponent_abbrev"] == o])
                            for o in opponents},
            "regular_season": _summary([r for r in rows if r["game_type"] == 2]),
            "playoffs": _summary([r for r in rows if r["game_type"] == 3])}

import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from psycopg2 import pool


class OfflinePool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise RuntimeError("database access is disabled during unit tests")

    def putconn(self, _conn):
        pass


pool.ThreadedConnectionPool = OfflinePool

from backend.core.auth import AuthenticatedPrincipal, require_current_principal
from backend.models.blue_jackets_models import AttendanceUpdate
from backend.routers import blue_jackets
from backend.services import blue_jackets_service as service, nhl_client
from backend.tasks import blue_jackets_worker


GAME = {"nhl_game_id": 2021020013, "season": 20212022, "game_type": 2,
        "cbj_home": True, "opponent_abbrev": "ARI"}
COMPLETED = {"id": 2021020013, "season": 20212022, "gameType": 2,
             "gameState": "OFF", "homeTeam": {"abbrev": "CBJ", "score": 8, "sog": 26},
             "awayTeam": {"abbrev": "ARI", "score": 2, "sog": 38},
             "periodDescriptor": {"number": 3}, "regPeriods": 3,
             "summary": {"scoring": []}}
FUTURE = {"id": 2026020258, "season": 20262027, "gameType": 2,
          "gameState": "FUT", "homeTeam": {"abbrev": "CBJ"},
          "awayTeam": {"abbrev": "DAL"}}
USER = AuthenticatedPrincipal(id="11111111-1111-4111-8111-111111111111",
                              firebase_uid="firebase-1", email="one@example.com",
                              display_name="One", role="member")


class NHLParserTests(unittest.TestCase):
    def test_verified_completed_home_game_and_missing_future_stats(self):
        result = nhl_client.parse_landing(COMPLETED, GAME)
        self.assertEqual((result["goals_for"], result["goals_against"]), (8, 2))
        self.assertEqual((result["shots_for"], result["shots_against"]), (26, 38))
        self.assertEqual(result["result"], "W")
        future_game = {"nhl_game_id": 2026020258, "season": 20262027,
                       "game_type": 2, "cbj_home": True, "opponent_abbrev": "DAL"}
        pending = nhl_client.parse_landing(FUTURE, future_game)
        self.assertFalse(pending["complete"])
        self.assertIsNone(pending["shots_for"])
        self.assertIsNone(pending["goals_for"])

    def test_schedule_excludes_preseason_and_deduplicates_by_id(self):
        def entry(game_id, game_type):
            return {"id": game_id, "season": 20262027, "gameType": game_type,
                    "gameDate": "2026-11-03", "gameState": "FUT",
                    "homeTeam": {"abbrev": "CBJ"}, "awayTeam": {"abbrev": "DAL"}}
        schedule = {"games": [entry(1, 1), entry(2, 2), entry(2, 2), entry(3, 3)]}
        games, duplicates = nhl_client.parse_schedule(schedule, 20262027)
        self.assertEqual([g["nhl_game_id"] for g in games], [2, 3])
        self.assertEqual(duplicates, [2])

    def test_away_regulation_loss_and_overtime_loss(self):
        away_game = {**GAME, "cbj_home": False, "opponent_abbrev": "ARI"}
        payload = {**COMPLETED, "homeTeam": {"abbrev": "ARI", "score": 3, "sog": 20},
                   "awayTeam": {"abbrev": "CBJ", "score": 2, "sog": 30}}
        regulation = nhl_client.parse_landing(payload, away_game)
        self.assertEqual(regulation["result"], "L")
        self.assertEqual(regulation["shots_for"], 30)
        overtime = nhl_client.parse_landing(
            {**payload, "periodDescriptor": {"number": 4}}, away_game)
        self.assertEqual(overtime["result"], "OTL")
        self.assertTrue(overtime["complete"])

    def test_final_is_not_off_and_missing_period_remains_unclassified(self):
        final = nhl_client.parse_landing({**COMPLETED, "gameState": "FINAL"}, GAME)
        self.assertFalse(final["complete"])
        self.assertIsNone(final["goals_for"])
        away_game = {**GAME, "cbj_home": False}
        loss = {**COMPLETED, "homeTeam": {"abbrev": "ARI", "score": 3},
                "awayTeam": {"abbrev": "CBJ", "score": 2}}
        unclassified = nhl_client.parse_landing({k: v for k, v in loss.items()
                                                  if k != "periodDescriptor"}, away_game)
        self.assertIsNone(unclassified["result"])
        self.assertFalse(unclassified["complete"])

    def test_rejects_preseason_and_mismatched_identity(self):
        with self.assertRaises(nhl_client.NHLDataError):
            nhl_client.parse_landing({**COMPLETED, "gameType": 1}, GAME)
        with self.assertRaises(nhl_client.NHLDataError):
            nhl_client.parse_landing({**COMPLETED, "id": 99}, GAME)


class SummaryTests(unittest.TestCase):
    @staticmethod
    def _row(opponent, game_type, result, *, season=20212022, shots_for=30):
        return {"opponent_abbrev": opponent, "game_type": game_type, "season": season,
                "result": result, "goals_for": 3 if result else None,
                "goals_against": 2 if result else None,
                "shots_for": shots_for if result else None,
                "shots_against": 25 if result else None}

    def _attendance_summary(self, rows, season=None):
        class Cursor:
            description = [(key,) for key in (rows[0] if rows else
                           ("game_type", "opponent_abbrev", "season", "result",
                            "goals_for", "goals_against", "shots_for", "shots_against"))]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def execute(self, sql, params):
                self.sql = sql
                self.params = params

            def fetchall(self):
                return [tuple(row[key[0]] for key in self.description) for row in rows]

        class Connection:
            def __init__(self):
                self.test_cursor = Cursor()

            def cursor(self):
                return self.test_cursor

        conn = Connection()
        with patch.object(service, "get_db_conn", return_value=conn), \
             patch.object(service, "put_db_conn") as release:
            result = service.attendance_summary(user_id=USER.id, season=season)
        self.assertIn("attendance.user_id = %s AND attendance.status = 'attended'",
                      conn.test_cursor.sql)
        self.assertIn("game.season = %s::integer", conn.test_cursor.sql)
        self.assertEqual(conn.test_cursor.params, (USER.id, season, season))
        release.assert_called_once_with(conn)
        return result

    def test_attended_only_completed_totals_and_missing_shots(self):
        rows = [
            {"result": "W", "goals_for": 8, "goals_against": 2,
             "shots_for": 26, "shots_against": 38},
            {"result": "OTL", "goals_for": 2, "goals_against": 3,
             "shots_for": None, "shots_against": None},
            {"result": None, "goals_for": None, "goals_against": None,
             "shots_for": None, "shots_against": None},
        ]
        result = service._summary(rows)
        self.assertEqual(result["games_attended"], 2)
        self.assertEqual(result["pending_attended_games"], 1)
        self.assertEqual(result["wins"], 1)
        self.assertEqual(result["ot_shootout_losses"], 1)
        self.assertEqual(result["win_percentage"], 50)
        self.assertEqual(result["goal_differential"], 5)
        self.assertIsNone(result["shots_for"])
        self.assertEqual(result["available_game_counts"]["shots_for"], 1)
        self.assertIsNone(service._summary(rows[2:])["shots_for"])
        self.assertEqual(service._summary(rows[:1])["games_attended"], 1)

    def test_opponents_split_by_competition_without_changing_combined_summary(self):
        regular_win = self._row("NYR", 2, "W")
        regular_loss = self._row("BOS", 2, "L")
        playoff_otl = self._row("NYR", 3, "OTL", shots_for=None)
        playoff_pending = self._row("CAR", 3, None)
        rows = [regular_win, regular_loss, playoff_otl, playoff_pending]
        result = self._attendance_summary(rows)

        self.assertEqual(set(result), {"season", *service._summary(rows),
                                       "by_opponent", "by_opponent_regular_season",
                                       "by_opponent_playoffs", "regular_season", "playoffs"})
        self.assertEqual(result["by_opponent"], {
            "BOS": service._summary([regular_loss]),
            "CAR": service._summary([playoff_pending]),
            "NYR": service._summary([regular_win, playoff_otl]),
        })
        self.assertEqual(result["by_opponent_regular_season"], {
            "BOS": service._summary([regular_loss]),
            "NYR": service._summary([regular_win]),
        })
        self.assertEqual(result["by_opponent_playoffs"], {
            "CAR": service._summary([playoff_pending]),
            "NYR": service._summary([playoff_otl]),
        })
        self.assertEqual((result["by_opponent"]["NYR"]["wins"],
                          result["by_opponent"]["NYR"]["ot_shootout_losses"]), (1, 1))
        self.assertEqual(result["by_opponent_playoffs"]["CAR"]["pending_attended_games"], 1)
        self.assertIsNone(result["by_opponent_playoffs"]["NYR"]["shots_for"])
        self.assertEqual(result["regular_season"], service._summary(rows[:2]))
        self.assertEqual(result["playoffs"], service._summary(rows[2:]))
        self.assertEqual(result["games_attended"], 3)
        self.assertEqual(result["pending_attended_games"], 1)

    def test_season_filter_and_absent_playoff_attendance(self):
        regular = self._row("NYR", 2, "W")
        result = self._attendance_summary([regular], season=20212022)
        self.assertEqual(result["season"], 20212022)
        self.assertEqual(result["by_opponent"], {"NYR": service._summary([regular])})
        self.assertEqual(result["by_opponent_regular_season"], result["by_opponent"])
        self.assertEqual(result["by_opponent_playoffs"], {})
        self.assertEqual(result["playoffs"], service._summary([]))

    def test_no_attendance_returns_empty_opponent_maps(self):
        result = self._attendance_summary([], season=20212022)
        self.assertEqual(result["by_opponent"], {})
        self.assertEqual(result["by_opponent_regular_season"], {})
        self.assertEqual(result["by_opponent_playoffs"], {})
        self.assertEqual(result["games_attended"], 0)
        self.assertEqual(result["regular_season"], service._summary([]))
        self.assertEqual(result["playoffs"], service._summary([]))


class AttendanceServiceTests(unittest.TestCase):
    def test_going_does_not_fetch_details_and_historical_attended_does(self):
        historical = {**GAME, "game_date": date(2021, 10, 14),
                      "start_time_utc": datetime(2021, 10, 14, tzinfo=timezone.utc)}

        class Cursor:
            description = []
            data = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def execute(self, sql, params):
                if "SELECT * FROM public.blue_jackets_games" in sql:
                    row = historical
                elif "RETURNING user_id" in sql:
                    row = {"user_id": USER.id, "nhl_game_id": GAME["nhl_game_id"],
                           "status": params[2]}
                else:
                    self.description = []
                    self.data = []
                    return
                self.description = [(key,) for key in row]
                self.data = [tuple(row.values())]

            def fetchall(self):
                return self.data

        class Connection:
            def cursor(self):
                return Cursor()

            def commit(self):
                pass

            def rollback(self):
                pass

        with patch.object(service, "ensure_game_loaded"), \
             patch.object(service, "get_db_conn", return_value=Connection()), \
             patch.object(service, "put_db_conn"), \
             patch.object(service, "refresh_stats") as refresh:
            service.set_attendance(user_id=USER.id, game_id=GAME["nhl_game_id"], status="going")
            refresh.assert_not_called()
            service.set_attendance(user_id=USER.id, game_id=GAME["nhl_game_id"], status="attended")
            refresh.assert_called_once_with(user_id=USER.id, game_id=GAME["nhl_game_id"], force=False)


class RouteAndWorkerTests(unittest.TestCase):
    def test_summary_route_preserves_envelope_and_principal(self):
        summary = {"by_opponent": {}, "by_opponent_regular_season": {},
                   "by_opponent_playoffs": {}}
        with patch.object(blue_jackets.service, "attendance_summary",
                          return_value=summary) as aggregate:
            response = blue_jackets.get_summary(season=20212022, principal=USER)
        self.assertEqual(response, {"success": True, "data": summary})
        aggregate.assert_called_once_with(user_id=USER.id, season=20212022)

    def test_every_route_requires_strict_principal(self):
        for route in blue_jackets.router.routes:
            dependencies = [dep.call for dep in route.dependant.dependencies]
            self.assertIn(require_current_principal, dependencies, route.path)

    def test_attendance_and_stats_use_principal_identity(self):
        with patch.object(blue_jackets.service, "set_attendance", return_value={"status": "attended"}) as set_record:
            blue_jackets.set_attendance(2021020013, AttendanceUpdate(status="attended"), USER)
            self.assertEqual(set_record.call_args.kwargs["user_id"], USER.id)
        with patch.object(blue_jackets.service, "get_stats", return_value={}) as get_stats:
            blue_jackets.get_game_stats(2021020013, USER)
            self.assertEqual(get_stats.call_args.kwargs["user_id"], USER.id)

    def test_worker_fetches_only_due_attended_game_ids(self):
        with patch.object(blue_jackets_worker.service, "due_attended_games",
                          return_value=[{"nhl_game_id": 2021020013, "user_id": USER.id}]), \
             patch.object(blue_jackets_worker.service, "refresh_stats") as refresh:
            self.assertEqual(blue_jackets_worker.synchronize_attended_once(), 1)
            refresh.assert_called_once_with(user_id=USER.id, game_id=2021020013, force=False)

    def test_worker_covers_six_observed_seasons(self):
        with patch.object(blue_jackets_worker.service, "synchronize_schedule",
                          side_effect=lambda season: {"season": season,
                                                      "eligible_entries": 82,
                                                      "distinct_games": 82,
                                                      "duplicate_game_ids": []}) as sync:
            result = blue_jackets_worker.synchronize_schedules_once(date(2026, 11, 3))
        self.assertEqual(len(result), 6)
        self.assertEqual(sync.call_args_list[-1].args[0], 20262027)


if __name__ == "__main__":
    unittest.main()

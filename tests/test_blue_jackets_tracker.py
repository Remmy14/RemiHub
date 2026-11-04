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

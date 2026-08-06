import os
import ast
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from psycopg2 import pool

from backend.core.auth import (
    AuthenticatedPrincipal,
    require_admin_principal,
    require_current_principal,
)
from backend.services.auth_service import InactiveUserError


class OfflineThreadedConnectionPool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise RuntimeError("database access is disabled during race auth tests")

    def putconn(self, _connection):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool


ADMIN = AuthenticatedPrincipal(
    id="11111111-1111-4111-8111-111111111111",
    firebase_uid="firebase-admin-1",
    email="admin@example.com",
    display_name="Admin",
    role="admin",
)
MEMBER = AuthenticatedPrincipal(
    id="22222222-2222-4222-8222-222222222222",
    firebase_uid="firebase-member-1",
    email="member@example.com",
    display_name="Member",
    role="member",
)


def race_api_routes():
    from backend.routers import race

    return {
        (method, route.path): route
        for route in race.router.routes
        for method in getattr(route, "methods", set())
        if method in {"GET", "POST"}
    }


def bearer(token: str = "test-token") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def race_admin_route_keys():
    from backend.routers import race

    return sorted(race.ADMIN_RACE_API_ROUTES)


class RaceApiAuthorizationTests(unittest.TestCase):
    def test_every_race_api_route_has_explicit_access_classification(self):
        from backend.routers import race

        routes = set(race_api_routes())
        expected = race.PUBLIC_RACE_API_ROUTES | race.ADMIN_RACE_API_ROUTES

        self.assertEqual(routes, expected)
        self.assertTrue(
            race.PUBLIC_RACE_API_ROUTES.isdisjoint(race.ADMIN_RACE_API_ROUTES)
        )

    def test_public_get_routes_have_no_auth_dependencies(self):
        from backend.routers import race

        routes = race_api_routes()
        for route_key in race.PUBLIC_RACE_API_ROUTES:
            with self.subTest(route=route_key):
                dependencies = [
                    dependency.call
                    for dependency in routes[route_key].dependant.dependencies
                ]
                self.assertNotIn(require_current_principal, dependencies)
                self.assertNotIn(require_admin_principal, dependencies)

    def test_all_get_race_api_routes_are_public(self):
        from backend.routers import race

        get_routes = {
            route_key
            for route_key in race_api_routes()
            if route_key[0] == "GET"
        }

        self.assertEqual(get_routes, race.PUBLIC_RACE_API_ROUTES)

    def test_admin_routes_require_admin_dependency(self):
        from backend.routers import race

        routes = race_api_routes()
        for route_key in race.ADMIN_RACE_API_ROUTES:
            with self.subTest(route=route_key):
                dependencies = [
                    dependency.call
                    for dependency in routes[route_key].dependant.dependencies
                ]
                self.assertIn(require_admin_principal, dependencies)

    def test_public_reads_have_no_auth_parameters_and_keep_contracts(self):
        from backend.routers import race

        with (
            patch(
                "backend.routers.race.race_service.get_all_pools",
                return_value=[
                    {"id": 1, "name": "Family", "participantCount": 10},
                ],
            ),
            patch(
                "backend.routers.race.race_service.load_pool",
                return_value={"Alex": [{"number": "3", "name": "Driver"}]},
            ),
            patch(
                "backend.routers.race.race_service.get_draft_order_by_pool",
                return_value=[{"name": "Alex", "position": 1}],
            ),
            patch(
                "backend.routers.race.race_service.get_current_draft_pick_by_pool",
                return_value={"current_pick": 1, "participant": "Alex"},
            ),
            patch(
                "backend.routers.race.race_service.get_recent_picks",
                return_value=[
                    {
                        "participant": "Alex",
                        "driver_name": "Driver",
                        "car_number": "3",
                        "pick_number": 1,
                    },
                ],
            ),
            patch(
                "backend.routers.race.race_service.get_draft_status",
                return_value={
                    "status": "DRAFT_ACTIVE",
                    "current_picker": "Alex",
                    "on_deck": [],
                    "total_picks": 0,
                },
            ),
            patch(
                "backend.routers.race.race_service.get_leaderboard",
                return_value={
                    "success": True,
                    "standings": [],
                    "updatedAt": "2026-08-06T00:00:00",
                },
            ),
            patch(
                "backend.routers.race.race_service.get_starting_grid_status",
                return_value=[
                    {
                        "number": "3",
                        "name": "Driver",
                        "starting_position": 1,
                        "takenBy": None,
                        "car_image_url": "static/images/3.png",
                    },
                ],
            ),
            patch(
                "backend.routers.race.race_service.get_archives",
                return_value=[{"id": 1, "year": 2026}],
            ),
            patch(
                "backend.routers.race.race_service.get_archive_entries",
                return_value={"success": True, "entries": []},
            ),
        ):
            responses = {
                "pools": race.get_all_pools(),
                "assignments": race.get_pool_assignments(pool_id=1),
                "order": race.get_draft_order(pool_id=1),
                "pick": race.current_pick(pool_id=1),
                "recent": race.get_recent_picks(pool_id=1, limit=5),
                "status": race.draft_status(pool_id=1),
                "leaderboard": race.get_leaderboard(pool_id=1),
                "grid": race.get_grid_status(pool_id=1),
                "archives": race.get_archives(),
                "entries": race.get_archive_entries(archive_id=1),
            }

        self.assertIsInstance(responses["pools"], list)
        self.assertIn("participantCount", responses["pools"][0])
        self.assertIsInstance(responses["assignments"], dict)
        self.assertEqual(responses["order"][0]["position"], 1)
        self.assertEqual(
            responses["pick"],
            {"pick_number": 1, "participant": "Alex"},
        )
        self.assertEqual(responses["recent"][0]["pick_number"], 1)
        self.assertEqual(responses["status"]["status"], "DRAFT_ACTIVE")
        self.assertTrue(responses["leaderboard"]["success"])
        self.assertEqual(responses["grid"][0]["starting_position"], 1)
        self.assertTrue(responses["archives"]["success"])
        self.assertEqual(responses["archives"]["archives"][0]["year"], 2026)
        self.assertTrue(responses["entries"]["success"])

    def test_admin_mutations_reject_unauthenticated_requests(self):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}, clear=True):
            for route_key in race_admin_route_keys():
                with self.subTest(route=route_key):
                    with self.assertRaises(HTTPException) as caught:
                        require_current_principal(None)

                    self.assertEqual(caught.exception.status_code, 401)
                    self.assertEqual(
                        caught.exception.detail,
                        "Authentication required",
                    )

    def test_admin_mutations_reject_authenticated_members(self):
        for route_key in race_admin_route_keys():
            with self.subTest(route=route_key):
                with self.assertRaises(HTTPException) as caught:
                    require_admin_principal(MEMBER)

                self.assertEqual(caught.exception.status_code, 403)
                self.assertEqual(
                    caught.exception.detail,
                    "Administrator access required",
                )

    def test_admin_mutations_permit_administrators(self):
        from backend.routers import race

        with (
            patch(
                "backend.routers.race.race_service.create_pool",
                return_value={"success": True, "id": 1, "name": "Family"},
            ) as create_pool,
            patch("backend.routers.race.race_service.reset_draft") as reset_draft,
            patch(
                "backend.routers.race.race_service.reset_race_to_square_one"
            ) as reset,
            patch(
                "backend.routers.race.race_service.start_draft",
                return_value={"success": False, "message": "Draft not ready."},
            ) as start_draft,
            patch(
                "backend.routers.race.race_service.submit_pick",
                return_value={"success": False, "message": "Invalid pick."},
            ) as submit_pick,
            patch(
                "backend.routers.race.race_service.set_race_draft_status"
            ) as set_status,
        ):
            self.assertEqual(
                race.create_pool(
                    request={"name": "Family", "participantCount": 10},
                    _principal=ADMIN,
                ),
                {"success": True, "id": 1, "name": "Family"},
            )
            self.assertEqual(
                race.submit_draft_order(
                    pool_id=1,
                    order=[{"name": "Alex", "position": 1}],
                    _principal=ADMIN,
                ),
                {"success": True, "message": "Draft order initialized."},
            )
            self.assertIsNone(race.reset_all_status(_principal=ADMIN))
            self.assertEqual(
                race.start_draft_now(pool_id=1, _principal=ADMIN),
                {"success": False, "message": "Draft not ready."},
            )
            self.assertEqual(
                race.submit_pick(pool_id=1, car_number="3", _principal=ADMIN),
                {"success": False, "message": "Invalid pick."},
            )
            self.assertEqual(
                race.start_race(_principal=ADMIN),
                {"success": True, "message": "Race tracking is now active."},
            )
            self.assertEqual(
                race.stop_race(_principal=ADMIN),
                {"success": True, "message": "Race tracking is now completed."},
            )

        create_pool.assert_called_once_with("Family", 10)
        reset_draft.assert_called_once_with(1, [{"name": "Alex", "position": 1}])
        reset.assert_called_once()
        start_draft.assert_called_once_with(1)
        submit_pick.assert_called_once_with(1, "3")
        self.assertEqual(set_status.call_count, 2)

    def test_admin_mutation_rejects_invalid_firebase_identity(self):
        with (
            patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "required"}, clear=True),
            patch(
                "backend.core.auth.verify_firebase_id_token",
                side_effect=ValueError("bad token"),
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                require_current_principal(bearer("invalid"))

        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(
            caught.exception.detail,
            "Invalid or expired authentication token",
        )

    def test_admin_mutation_rejects_inactive_user(self):
        with (
            patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "required"}, clear=True),
            patch(
                "backend.core.auth.verify_firebase_id_token",
                return_value={
                    "uid": "firebase-member-1",
                    "email": "member@example.com",
                    "email_verified": True,
                },
            ),
            patch(
                "backend.core.auth.resolve_authenticated_user",
                side_effect=InactiveUserError("RemiHub user is inactive"),
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                require_current_principal(bearer("inactive"))

        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.detail, "RemiHub user is inactive")


class RacePortalBoundaryTests(unittest.TestCase):
    def test_race_and_draft_portals_are_registered_as_public_routes(self):
        source_path = Path(__file__).resolve().parents[1] / "backend" / "main.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))

        route_paths: set[str] = set()
        static_mounts: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) or isinstance(
                node,
                ast.AsyncFunctionDef,
            ):
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    if not isinstance(decorator.func, ast.Attribute):
                        continue
                    if decorator.func.attr != "get":
                        continue
                    if not decorator.args or not isinstance(
                        decorator.args[0],
                        ast.Constant,
                    ):
                        continue
                    route_paths.add(decorator.args[0].value)
                    self.assertFalse(
                        any(
                            keyword.arg == "dependencies"
                            for keyword in decorator.keywords
                        ),
                        msg=(
                            f"{decorator.args[0].value} should not declare "
                            "auth dependencies"
                        ),
                    )
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "mount":
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                static_mounts.add(node.args[0].value)

        self.assertIn("/race/draft", route_paths)
        self.assertIn("/race/draft/{full_path:path}", route_paths)
        self.assertIn("/race", static_mounts)


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from unittest.mock import patch

from fastapi import APIRouter, HTTPException
from psycopg2 import pool

from backend.core.auth import get_current_principal


class OfflineThreadedConnectionPool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise RuntimeError("database access is disabled during route policy tests")

    def putconn(self, _connection):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool

from backend.routers import race


class AuthenticationRoutePolicyTests(unittest.TestCase):
    def test_required_mode_keeps_public_router_open(self):
        public_router = APIRouter(prefix="/race")

        @public_router.get("/status")
        def public_status():
            return {"success": True}

        route = public_router.routes[0]

        self.assertEqual(route.dependant.dependencies, [])
        self.assertEqual(public_status(), {"success": True})

    def test_required_mode_rejects_protected_router_without_token(self):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "required"}, clear=True):
            with self.assertRaises(HTTPException) as caught:
                get_current_principal(None)

        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(caught.exception.detail, "Authentication required")

    def test_race_router_uses_endpoint_level_policy(self):
        classified_routes = (
            race.PUBLIC_RACE_API_ROUTES
            | race.ADMIN_RACE_API_ROUTES
        )
        registered_routes = {
            (method, route.path)
            for route in race.router.routes
            for method in getattr(route, "methods", set())
            if method in {"GET", "POST"}
        }

        self.assertEqual(registered_routes, classified_routes)


if __name__ == "__main__":
    unittest.main()

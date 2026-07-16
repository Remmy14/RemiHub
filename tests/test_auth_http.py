import os
import unittest
from unittest.mock import patch

from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from backend.core.auth import get_current_principal
from backend.routers import auth


test_router = APIRouter()


@test_router.get("/protected-test-route")
def protected_test_route():
    return {"success": True}


app = FastAPI()
app.include_router(auth.router)
app.include_router(
    test_router,
    dependencies=[Depends(get_current_principal)],
)
client = TestClient(app)


class AuthenticationHttpBoundaryTests(unittest.TestCase):
    def test_transition_mode_keeps_legacy_request_working(self):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}, clear=True):
            response = client.get("/protected-test-route")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True})

    def test_required_mode_rejects_legacy_request(self):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "required"}, clear=True):
            response = client.get("/protected-test-route")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Authentication required")
        self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_auth_me_remains_strict_during_transition(self):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}, clear=True):
            response = client.get("/auth/me")

        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()

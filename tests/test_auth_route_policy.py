import ast
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from backend.core.auth import get_current_principal


class AuthenticationRoutePolicyTests(unittest.TestCase):
    def test_required_mode_keeps_public_router_open(self):
        public_router = APIRouter(prefix="/race")

        @public_router.get("/status")
        def public_status():
            return {"success": True}

        app = FastAPI()
        app.include_router(public_router)
        client = TestClient(app)

        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "required"}, clear=True):
            response = client.get("/race/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True})

    def test_required_mode_rejects_protected_router_without_token(self):
        protected_router = APIRouter(prefix="/finance")

        @protected_router.get("/status")
        def protected_status():
            return {"success": True}

        app = FastAPI()
        app.include_router(
            protected_router,
            dependencies=[Depends(get_current_principal)],
        )
        client = TestClient(app)

        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "required"}, clear=True):
            response = client.get("/finance/status")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Authentication required")

    def test_production_policy_lists_only_race_as_public(self):
        source_path = Path(__file__).resolve().parents[1] / "backend" / "main.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))

        assignments: dict[str, list[str]] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            if target.id not in {"public_routers", "protected_routers"}:
                continue
            self.assertIsInstance(node.value, ast.List)
            assignments[target.id] = [
                element.value.id
                for element in node.value.elts
                if isinstance(element, ast.Attribute)
                and element.attr == "router"
                and isinstance(element.value, ast.Name)
            ]

        self.assertEqual(assignments["public_routers"], ["race"])
        self.assertNotIn("race", assignments["protected_routers"])
        self.assertEqual(
            set(assignments["protected_routers"]),
            {
                "pool",
                "plex",
                "fieldwatch",
                "auto_logins",
                "notifications",
                "app_update",
                "speedtest",
                "autographs",
                "weather",
                "rh_storage",
                "finance",
                "kids_investing",
                "weightlifting",
                "spotify",
            },
        )


if __name__ == "__main__":
    unittest.main()

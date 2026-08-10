import json
import unittest
from pathlib import Path


class FrontendWebPortalTests(unittest.TestCase):
    def test_firebase_browser_config_is_source_controlled_and_complete(self):
        config_path = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "firebase-browser"
            / "firebaseBrowserConfig.json"
        )

        config = json.loads(config_path.read_text(encoding="utf-8"))
        missing = [
            key
            for key in (
                "apiKey",
                "authDomain",
                "projectId",
                "storageBucket",
                "messagingSenderId",
                "appId",
            )
            if not isinstance(config.get(key), str) or not config[key].strip()
        ]

        self.assertEqual(missing, [])
        self.assertEqual(config["projectId"], "remihub-33a90")

    def test_frontend_uses_same_origin_public_race_api(self):
        frontend_root = Path(__file__).resolve().parents[1] / "frontend-web"

        for relative_path in (
            "src/RaceScreen.tsx",
            "src/DraftCompanionScreen.tsx",
        ):
            with self.subTest(path=relative_path):
                source = (frontend_root / relative_path).read_text(
                    encoding="utf-8",
                )
                self.assertNotIn("https://remillard.duckdns.org", source)

    def test_frontend_has_authenticated_api_helper_without_legacy_key(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "api"
            / "authenticatedApi.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("Authorization", source)
        self.assertIn("Bearer", source)
        self.assertNotIn("X-RemiHub-Key", source)

    def test_frontend_uses_firebase_javascript_sdk(self):
        auth_source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "auth"
            / "firebaseAuth.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("initializeApp", auth_source)
        self.assertIn("getAuth", auth_source)
        self.assertIn("onAuthStateChanged", auth_source)
        self.assertIn("signInWithEmailAndPassword", auth_source)
        self.assertIn("getIdToken", auth_source)
        self.assertNotIn("identitytoolkit.googleapis.com", auth_source)
        self.assertNotIn("refresh_token", auth_source)


    def test_frontend_authentication_is_sign_in_only(self):
        frontend_root = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
        )

        auth_source = (
            frontend_root
            / "src"
            / "auth"
            / "firebaseAuth.ts"
        ).read_text(encoding="utf-8")

        app_source = (
            frontend_root
            / "src"
            / "App.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "signInWithEmailAndPassword",
            auth_source,
        )

        for forbidden_api in (
            "createUserWithEmailAndPassword",
            "deleteUser(",
        ):
            with self.subTest(forbidden_api=forbidden_api):
                self.assertNotIn(
                    forbidden_api,
                    auth_source,
                )

        for forbidden_ui in (
            "Sign up",
            "Create account",
            "Register account",
        ):
            with self.subTest(forbidden_ui=forbidden_ui):
                self.assertNotIn(
                    forbidden_ui,
                    app_source,
                )

    def test_agent_portal_route_stays_inside_authenticated_shell(self):
        app_source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "App.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn('import AgentScreen from "./AgentScreen";', app_source)
        self.assertIn('path.startsWith("/agent")', app_source)
        self.assertIn("<AuthenticatedRoute>", app_source)
        self.assertLess(
            app_source.index('path.startsWith("/race")'),
            app_source.index("<AuthenticatedRoute>"),
        )
        self.assertGreater(
            app_source.index('path.startsWith("/agent")'),
            app_source.index("function PrivateApp()"),
        )

    def test_agent_frontend_uses_authenticated_api_only(self):
        agent_api_source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "api"
            / "agentApi.ts"
        ).read_text(encoding="utf-8")

        self.assertIn('from "./authenticatedApi"', agent_api_source)
        self.assertIn("apiRequest<", agent_api_source)
        self.assertNotIn("authenticated: false", agent_api_source)
        self.assertNotIn("X-RemiHub-Key", agent_api_source)
        self.assertNotIn("api_key", agent_api_source)
        self.assertNotIn("token=", agent_api_source)

    def test_agent_frontend_does_not_invent_lifecycle_routes(self):
        agent_api_source = (
            Path(__file__).resolve().parents[1]
            / "frontend-web"
            / "src"
            / "api"
            / "agentApi.ts"
        ).read_text(encoding="utf-8")

        expected_routes = (
            "/agent/cards",
            "/approve-implementation",
            "/approve-deployment",
            "/retry",
            "/retry-github-sync",
            "/cancel",
            "/close",
            "/messages",
        )
        for route in expected_routes:
            with self.subTest(route=route):
                self.assertIn(route, agent_api_source)

        self.assertNotIn("review-stage", agent_api_source)
        self.assertNotIn("review_stage", agent_api_source)

    def test_agent_backend_router_remains_admin_only(self):
        router_source = (
            Path(__file__).resolve().parents[1]
            / "backend"
            / "routers"
            / "agent.py"
        ).read_text(encoding="utf-8")

        self.assertIn("require_admin_principal", router_source)
        self.assertIn("dependencies=[Depends(require_admin_principal)]", router_source)
        self.assertNotIn("get_current_principal", router_source)

    def test_race_public_exception_is_not_broadened_for_agent(self):
        main_source = (
            Path(__file__).resolve().parents[1]
            / "backend"
            / "main.py"
        ).read_text(encoding="utf-8")

        self.assertIn("app.include_router(agent.router)", main_source)
        self.assertIn("app.include_router(race.router)", main_source)
        self.assertIn("protected_routers = [", main_source)
        self.assertLess(
            main_source.index("app.include_router(agent.router)"),
            main_source.index("protected_routers = ["),
        )
        self.assertLess(
            main_source.index("app.include_router(race.router)"),
            main_source.index("protected_routers = ["),
        )


if __name__ == "__main__":
    unittest.main()

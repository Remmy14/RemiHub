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


if __name__ == "__main__":
    unittest.main()

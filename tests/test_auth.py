import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from backend.core.auth import (
    AuthMode,
    AuthenticatedPrincipal,
    get_auth_mode,
    get_current_principal,
    require_admin_principal,
    require_current_principal,
)
from backend.core.firebase_auth import (
    FirebaseConfigurationError,
    get_service_account_path,
)
from backend.services.auth_service import (
    InactiveUserError,
    UserNotAuthorizedError,
    _admin_email_allowlist,
    _identity_from_token,
    resolve_authenticated_user,
)


def bearer(token: str = "test-token") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def user_record() -> dict:
    return {
        "id": "c346f3f4-3867-4ddb-83ea-7d24db8817bc",
        "firebase_uid": "firebase-user-1",
        "email": "alex@example.com",
        "display_name": "Alex",
        "role": "admin",
    }


class AuthModeTests(unittest.TestCase):
    def test_default_mode_is_transition(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_auth_mode(), AuthMode.TRANSITION)

    def test_mode_is_case_insensitive(self):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": " REQUIRED "}, clear=True):
            self.assertEqual(get_auth_mode(), AuthMode.REQUIRED)

    def test_invalid_mode_fails_closed(self):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "sometimes"}, clear=True):
            with self.assertRaises(RuntimeError):
                get_auth_mode()


class FirebaseConfigurationTests(unittest.TestCase):
    def test_uses_configured_service_account_path(self):
        configured_path = Path("/secure/firebase-service-account.json")

        with patch.dict(
            os.environ,
            {"FIREBASE_SERVICE_ACCOUNT_FILE": str(configured_path)},
            clear=True,
        ):
            self.assertEqual(get_service_account_path(), configured_path)

    def test_empty_configuration_uses_repository_default(self):
        with patch.dict(
            os.environ,
            {"FIREBASE_SERVICE_ACCOUNT_FILE": ""},
            clear=True,
        ):
            resolved = get_service_account_path()

        self.assertEqual(resolved.name, "firebase-service-account.json")
        self.assertEqual(resolved.parent.name, "config")


class AuthenticationDependencyTests(unittest.TestCase):
    def test_disabled_mode_ignores_missing_credentials(self):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "disabled"}, clear=True):
            self.assertIsNone(get_current_principal(None))

    def test_transition_mode_permits_missing_credentials(self):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}, clear=True):
            self.assertIsNone(get_current_principal(None))

    def test_required_mode_rejects_missing_credentials(self):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "required"}, clear=True):
            with self.assertRaises(HTTPException) as caught:
                get_current_principal(None)
        self.assertEqual(caught.exception.status_code, 401)

    def test_strict_dependency_rejects_missing_credentials_in_transition(self):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}, clear=True):
            with self.assertRaises(HTTPException) as caught:
                require_current_principal(None)
        self.assertEqual(caught.exception.status_code, 401)

    def test_admin_dependency_accepts_administrator(self):
        principal = AuthenticatedPrincipal(
            id="c346f3f4-3867-4ddb-83ea-7d24db8817bc",
            firebase_uid="firebase-user-1",
            email="alex@example.com",
            display_name="Alex",
            role="admin",
        )

        self.assertIs(require_admin_principal(principal), principal)

    def test_admin_dependency_rejects_member(self):
        principal = AuthenticatedPrincipal(
            id="c346f3f4-3867-4ddb-83ea-7d24db8817bc",
            firebase_uid="firebase-user-1",
            email="alex@example.com",
            display_name="Alex",
            role="member",
        )

        with self.assertRaises(HTTPException) as caught:
            require_admin_principal(principal)

        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(
            caught.exception.detail,
            "Administrator access required",
        )

    @patch("backend.core.auth.resolve_authenticated_user", return_value=user_record())
    @patch(
        "backend.core.auth.verify_firebase_id_token",
        return_value={
            "uid": "firebase-user-1",
            "email": "alex@example.com",
            "email_verified": True,
        },
    )
    def test_valid_token_returns_principal(self, _verify_token, _resolve_user):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}, clear=True):
            principal = get_current_principal(bearer())

        self.assertIsNotNone(principal)
        self.assertEqual(principal.email, "alex@example.com")
        self.assertEqual(principal.role, "admin")

    @patch("backend.core.auth.verify_firebase_id_token", side_effect=ValueError("bad"))
    def test_invalid_token_is_unauthorized(self, _verify_token):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}, clear=True):
            with self.assertRaises(HTTPException) as caught:
                get_current_principal(bearer())
        self.assertEqual(caught.exception.status_code, 401)

    @patch(
        "backend.core.auth.verify_firebase_id_token",
        side_effect=FirebaseConfigurationError("not configured"),
    )
    def test_missing_firebase_configuration_is_unavailable(self, _verify_token):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}, clear=True):
            with self.assertRaises(HTTPException) as caught:
                get_current_principal(bearer())
        self.assertEqual(caught.exception.status_code, 503)

    @patch(
        "backend.core.auth.verify_firebase_id_token",
        return_value={"uid": "firebase-user-1"},
    )
    @patch(
        "backend.core.auth.resolve_authenticated_user",
        side_effect=UserNotAuthorizedError("not enrolled"),
    )
    def test_unenrolled_user_is_forbidden(self, _resolve_user, _verify_token):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}, clear=True):
            with self.assertRaises(HTTPException) as caught:
                get_current_principal(bearer())
        self.assertEqual(caught.exception.status_code, 403)

    @patch(
        "backend.core.auth.verify_firebase_id_token",
        return_value={"uid": "firebase-user-1"},
    )
    @patch(
        "backend.core.auth.resolve_authenticated_user",
        side_effect=InactiveUserError("inactive"),
    )
    def test_inactive_user_is_forbidden(self, _resolve_user, _verify_token):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}, clear=True):
            with self.assertRaises(HTTPException) as caught:
                get_current_principal(bearer())
        self.assertEqual(caught.exception.status_code, 403)


class FirebaseIdentityTests(unittest.TestCase):
    def test_identity_requires_verified_email(self):
        with self.assertRaises(UserNotAuthorizedError):
            _identity_from_token(
                {
                    "uid": "firebase-user-1",
                    "email": "alex@example.com",
                    "email_verified": False,
                }
            )

    def test_identity_normalizes_email(self):
        identity = _identity_from_token(
            {
                "uid": "firebase-user-1",
                "email": " Alex@Example.COM ",
                "email_verified": True,
                "name": "Alex",
            }
        )
        self.assertEqual(identity, ("firebase-user-1", "alex@example.com", "Alex"))

    def test_identity_normalizes_blank_display_name_to_none(self):
        identity = _identity_from_token(
            {
                "uid": "firebase-user-1",
                "email": "alex@example.com",
                "email_verified": True,
                "name": "   ",
            }
        )
        self.assertIsNone(identity[2])

    def test_admin_allowlist_is_normalized(self):
        with patch.dict(
            os.environ,
            {"REMIHUB_ADMIN_EMAILS": " Alex@Example.com, second@example.com "},
            clear=True,
        ):
            self.assertEqual(
                _admin_email_allowlist(),
                {"alex@example.com", "second@example.com"},
            )


class AuthenticatedUserResolutionTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.cursor = self.conn.cursor.return_value.__enter__.return_value
        self.token = {
            "uid": "firebase-user-1",
            "email": "alex@example.com",
            "email_verified": True,
            "name": "Alex",
        }

    def test_allowlisted_identity_is_enrolled_as_admin(self):
        created_row = (
            "c346f3f4-3867-4ddb-83ea-7d24db8817bc",
            "firebase-user-1",
            "alex@example.com",
            "Alex",
            "admin",
            True,
            None,
            None,
            None,
        )
        self.cursor.fetchone.side_effect = [None, None, created_row]

        with patch.dict(
            os.environ,
            {"REMIHUB_ADMIN_EMAILS": "alex@example.com"},
            clear=True,
        ):
            user = resolve_authenticated_user(self.token, conn=self.conn)

        self.assertEqual(user["role"], "admin")
        self.conn.commit.assert_called_once_with()
        self.conn.rollback.assert_not_called()

    def test_unenrolled_identity_outside_allowlist_is_rejected(self):
        self.cursor.fetchone.return_value = None

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(UserNotAuthorizedError):
                resolve_authenticated_user(self.token, conn=self.conn)

        self.conn.rollback.assert_called_once_with()
        self.conn.commit.assert_not_called()

    def test_inactive_existing_user_is_rejected(self):
        inactive_row = (
            "c346f3f4-3867-4ddb-83ea-7d24db8817bc",
            "firebase-user-1",
            "alex@example.com",
            "Alex",
            "admin",
            False,
            None,
            None,
            None,
        )
        self.cursor.fetchone.return_value = inactive_row

        with self.assertRaises(InactiveUserError):
            resolve_authenticated_user(self.token, conn=self.conn)

        self.conn.rollback.assert_called_once_with()
        self.conn.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()

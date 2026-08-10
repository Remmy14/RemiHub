import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from backend.core.auth import AuthenticatedPrincipal, require_admin_principal, require_current_principal
from backend.models.health_models import (
    HealthComponent,
    HealthComponentGroup,
    HealthComponentKind,
    HealthStatus,
    ServiceHealthSnapshotResponse,
)
from backend.routers import health


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


def bearer(token: str = "test-token", scheme: str = "Bearer") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme=scheme, credentials=token)


def snapshot() -> ServiceHealthSnapshotResponse:
    checked_at = datetime.now(timezone.utc)
    return ServiceHealthSnapshotResponse(
        success=True,
        checked_at=checked_at,
        overall=HealthStatus.HEALTHY,
        components=[
            HealthComponent(
                id="remihub",
                name="RemiHub Backend",
                group=HealthComponentGroup.CORE,
                kind=HealthComponentKind.SYSTEMD_UNIT,
                status=HealthStatus.HEALTHY,
                message="ok",
                checked_at=checked_at,
            )
        ],
    )


class HealthHttpBoundaryTests(unittest.TestCase):
    def test_router_is_admin_only_and_has_no_arbitrary_systemd_route(self):
        paths = {route.path for route in health.router.routes}

        self.assertIn("/health/services", paths)
        self.assertNotIn("/health/systemd/{unit}", paths)
        for route in health.router.routes:
            dependencies = [dependency.call for dependency in route.dependant.dependencies]
            self.assertIn(require_admin_principal, dependencies)
            self.assertNotIn(require_current_principal, dependencies)

    def test_missing_bearer_is_rejected_by_strict_dependency(self):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}, clear=True):
            with self.assertRaises(HTTPException) as caught:
                require_current_principal(None)

        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(caught.exception.detail, "Authentication required")

    def test_malformed_bearer_is_rejected(self):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}, clear=True):
            with self.assertRaises(HTTPException) as caught:
                require_current_principal(bearer("abc", scheme="Basic"))

        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(
            caught.exception.detail,
            "Invalid or expired authentication token",
        )

    @patch("backend.core.auth.verify_firebase_id_token", side_effect=ValueError("expired"))
    def test_invalid_or_expired_bearer_is_rejected(self, _verify):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}, clear=True):
            with self.assertRaises(HTTPException) as caught:
                require_current_principal(bearer("expired-token"))

        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(
            caught.exception.detail,
            "Invalid or expired authentication token",
        )

    def test_non_admin_principal_is_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            require_admin_principal(MEMBER)

        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(
            caught.exception.detail,
            "Administrator access required",
        )

    @patch("backend.routers.health.service_health_service.get_service_health_snapshot")
    def test_admin_route_function_returns_typed_snapshot(self, get_snapshot):
        get_snapshot.return_value = snapshot()

        response = health.get_service_health_snapshot()

        self.assertTrue(response.success)
        self.assertEqual(response.overall, HealthStatus.HEALTHY)
        self.assertEqual(response.components[0].id, "remihub")

    def test_response_model_contains_no_secret_or_environment_fields(self):
        fields = ServiceHealthSnapshotResponse.model_fields
        component_fields = HealthComponent.model_fields

        self.assertNotIn("environment", fields)
        self.assertNotIn("secret", fields)
        self.assertNotIn("stderr", component_fields)
        self.assertNotIn("journal", component_fields)


if __name__ == "__main__":
    unittest.main()

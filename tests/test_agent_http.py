import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core.auth import AuthenticatedPrincipal, require_admin_principal
from backend.routers import agent
from backend.services.agent_service import (
    AgentCardNotFoundError,
    AgentConflictError,
)


CARD_ID = "3d8549c4-a965-4d2e-aacf-9df7e6ccdbb4"
USER_ID = "c346f3f4-3867-4ddb-83ea-7d24db8817bc"
NOW = "2026-07-29T15:00:00+00:00"
ADMIN = AuthenticatedPrincipal(
    id=USER_ID,
    firebase_uid="firebase-user-1",
    email="alex@example.com",
    display_name="Alex",
    role="admin",
)


def card_detail(*, status="planning_queued", allowed_actions=None):
    return {
        "id": CARD_ID,
        "title": "Medication tracking",
        "description": "Plan a medication tracking module.",
        "status": status,
        "repository_scope": "backend",
        "revision": 1,
        "base_branch": "main",
        "feature_branch": None,
        "worktree_path": None,
        "codex_thread_id": None,
        "resume_status": None,
        "blocked_reason": None,
        "blocked_until": None,
        "created_by": USER_ID,
        "closed_at": None,
        "created_at": NOW,
        "updated_at": NOW,
        "latest_run": None,
        "allowed_actions": allowed_actions or [],
        "messages": [],
        "runs": [],
        "approvals": [],
        "events": [],
    }


strict_app = FastAPI()
strict_app.include_router(agent.router)
strict_client = TestClient(strict_app)

admin_app = FastAPI()
admin_app.include_router(agent.router)
admin_app.dependency_overrides[require_admin_principal] = lambda: ADMIN
admin_client = TestClient(admin_app)


class AgentHttpBoundaryTests(unittest.TestCase):
    def test_agent_routes_remain_strict_during_transition(self):
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}, clear=True):
            response = strict_client.get("/agent/cards")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Authentication required")

    @patch("backend.routers.agent.agent_service.create_card")
    def test_create_card_uses_authenticated_administrator(self, create_card):
        create_card.return_value = card_detail()

        response = admin_client.post(
            "/agent/cards",
            json={
                "title": "Medication tracking",
                "description": "Plan a medication tracking module.",
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["data"]["status"], "planning_queued")
        create_card.assert_called_once_with(
            title="Medication tracking",
            description="Plan a medication tracking module.",
            created_by=USER_ID,
            client_message_id=None,
        )

    @patch("backend.routers.agent.agent_service.create_card")
    def test_second_open_card_is_reported_as_conflict(self, create_card):
        create_card.side_effect = AgentConflictError(
            "Another agent card is already open"
        )

        response = admin_client.post(
            "/agent/cards",
            json={"title": "Second card", "description": "Do another thing"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"],
            "Another agent card is already open",
        )

    @patch("backend.routers.agent.agent_service.list_cards")
    def test_closed_cards_can_be_requested(self, list_cards):
        list_cards.return_value = []

        response = admin_client.get("/agent/cards?include_closed=true")

        self.assertEqual(response.status_code, 200)
        list_cards.assert_called_once_with(include_closed=True)

    @patch("backend.routers.agent.agent_service.get_card")
    def test_missing_card_is_not_found(self, get_card):
        get_card.side_effect = AgentCardNotFoundError(
            f"Agent card not found: {CARD_ID}"
        )

        response = admin_client.get(f"/agent/cards/{CARD_ID}")

        self.assertEqual(response.status_code, 404)

    @patch("backend.routers.agent.agent_service.add_follow_up")
    def test_follow_up_is_attributed_to_administrator(self, add_follow_up):
        add_follow_up.return_value = card_detail(
            status="implementation_queued",
            allowed_actions=["cancel"],
        )

        response = admin_client.post(
            f"/agent/cards/{CARD_ID}/messages",
            json={"content": "Move history below current medications."},
        )

        self.assertEqual(response.status_code, 200)
        add_follow_up.assert_called_once_with(
            card_id=CARD_ID,
            content="Move history below current medications.",
            created_by=USER_ID,
            client_message_id=None,
        )

    @patch("backend.routers.agent.agent_service.approve_implementation")
    def test_implementation_approval_is_explicit(self, approve_implementation):
        approve_implementation.return_value = card_detail(
            status="implementation_queued",
            allowed_actions=["cancel"],
        )

        response = admin_client.post(
            f"/agent/cards/{CARD_ID}/approve-implementation",
            json={"notes": "Plan approved"},
        )

        self.assertEqual(response.status_code, 200)
        approve_implementation.assert_called_once_with(
            card_id=CARD_ID,
            approved_by=USER_ID,
            notes="Plan approved",
        )

    @patch("backend.routers.agent.agent_service.retry_card")
    def test_failed_card_retry_is_explicit(self, retry_card):
        retry_card.return_value = card_detail(
            status="deployment_queued",
            allowed_actions=["cancel"],
        )

        response = admin_client.post(
            f"/agent/cards/{CARD_ID}/retry",
            json={"notes": "Retry the approved candidate"},
        )

        self.assertEqual(response.status_code, 200)
        retry_card.assert_called_once_with(
            card_id=CARD_ID,
            requested_by=USER_ID,
            notes="Retry the approved candidate",
        )

    def test_blank_card_description_is_rejected(self):
        response = admin_client.post(
            "/agent/cards",
            json={"title": "Blank request", "description": "   "},
        )

        self.assertEqual(response.status_code, 422)

    def test_openapi_has_typed_success_and_error_contracts(self):
        schema = admin_app.openapi()
        paths = schema["paths"]

        self.assertEqual(len([path for path in paths if path.startswith("/agent/")]), 8)
        self.assertEqual(
            paths["/agent/cards"]["get"]["responses"]["200"]["content"]
            ["application/json"]["schema"]["$ref"],
            "#/components/schemas/AgentCardListResponse",
        )
        self.assertEqual(
            paths["/agent/cards/{card_id}"]["get"]["responses"]["200"]
            ["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AgentCardResponse",
        )
        retry = paths["/agent/cards/{card_id}/retry"]["post"]
        self.assertEqual(
            retry["responses"]["409"]["content"]["application/json"]
            ["schema"]["$ref"],
            "#/components/schemas/AgentErrorResponse",
        )
        self.assertIn("AgentCardDetail", schema["components"]["schemas"])
        self.assertIn("AgentCardAction", schema["components"]["schemas"])


if __name__ == "__main__":
    unittest.main()

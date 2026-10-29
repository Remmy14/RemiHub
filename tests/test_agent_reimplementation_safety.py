import unittest

from backend.services.agent_service import (
    _safe_reimplementation_failure,
)
from backend.services.agent_worker_service import (
    _failure_metadata,
)


class ReimplementationSafetyTests(unittest.TestCase):

    def setUp(self):
        self.card = {
            "status": "failed",
            "repository_scope": "backend",
            "revision": 1,
        }

        self.run = {
            "id": "70142ab9-5133-4402-bfe0-c2b920cfb67d",
            "phase": "deployment",
            "status": "failed",
            "card_revision": 1,
            "error_message": (
                "AgentDeploymentError: Deployment target "
                "advanced before candidate creation"
            ),
            "result_metadata": {},
        }

    def eligible(self, **changes):
        return _safe_reimplementation_failure(
            self.card,
            {**self.run, **changes},
        )

    def test_qa_baseline_mismatch(self):
        self.assertTrue(self.eligible())

        self.assertTrue(
            self.eligible(
                result_metadata={"environment": "qa"}
            )
        )

    def test_production_handoff_rejected(self):
        metadata = {
            "deployment_pipeline": {
                "stage": "qa_succeeded",
            }
        }

        self.assertEqual(
            _failure_metadata(metadata),
            metadata,
        )

        self.assertFalse(
            self.eligible(
                result_metadata=_failure_metadata(metadata)
            )
        )

        self.assertFalse(
            self.eligible(
                result_metadata={"environment": "production"}
            )
        )

        self.assertFalse(
            self.eligible(
                result_metadata={
                    "deployment_recovery": {
                        "production_deployed": True
                    }
                }
            )
        )

    def test_other_errors_rejected(self):
        self.assertFalse(
            self.eligible(
                error_message=(
                    "AgentDeploymentError: different failure"
                )
            )
        )

        self.assertFalse(
            self.eligible(
                error_message=(
                    self.run["error_message"]
                    + "; rollback was incomplete"
                )
            )
        )

    def test_existing_allowlist_preserved(self):
        self.assertTrue(
            self.eligible(
                error_message=(
                    "Unable to apply the approved "
                    "implementation patch"
                )
            )
        )

    def test_revision_scope_and_status_boundaries(self):
        self.assertFalse(
            self.eligible(card_revision=2)
        )

        self.assertFalse(
            _safe_reimplementation_failure(
                {
                    **self.card,
                    "repository_scope": "android",
                },
                self.run,
            )
        )

        self.assertFalse(
            _safe_reimplementation_failure(
                {
                    **self.card,
                    "status": "review_ready",
                },
                self.run,
            )
        )


if __name__ == "__main__":
    unittest.main()

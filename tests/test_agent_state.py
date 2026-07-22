import unittest

from backend.core.agent_state import (
    ALLOWED_CARD_TRANSITIONS,
    TERMINAL_CARD_STATUSES,
    CardStatus,
    InvalidCardTransitionError,
    InvalidRunCompletionError,
    RepositoryScope,
    RunPhase,
    active_card_status_for_phase,
    require_backend_repository_scope,
    require_deployment_repository_scope,
    follow_up_target,
    queued_card_status_for_phase,
    require_card_transition,
    require_resolved_repository_scope,
    require_run_completion_status,
)


class AgentCardStateTests(unittest.TestCase):
    def test_every_card_status_has_a_transition_definition(self):
        self.assertEqual(set(ALLOWED_CARD_TRANSITIONS), set(CardStatus))

    def test_planning_can_stop_for_implementation_approval(self):
        current, target = require_card_transition(
            CardStatus.PLANNING,
            CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
        )

        self.assertEqual(current, CardStatus.PLANNING)
        self.assertEqual(
            target,
            CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
        )

    def test_implementation_requires_approval_state(self):
        with self.assertRaisesRegex(
            InvalidCardTransitionError,
            "planning_queued to implementation_queued",
        ):
            require_card_transition(
                CardStatus.PLANNING_QUEUED,
                CardStatus.IMPLEMENTATION_QUEUED,
            )

    def test_deployment_cannot_skip_review(self):
        with self.assertRaises(InvalidCardTransitionError):
            require_card_transition(
                CardStatus.IMPLEMENTING,
                CardStatus.DEPLOYMENT_QUEUED,
            )

    def test_planning_follow_up_queues_another_planning_run(self):
        status, phase = follow_up_target(
            CardStatus.AWAITING_IMPLEMENTATION_APPROVAL
        )

        self.assertEqual(status, CardStatus.PLANNING_QUEUED)
        self.assertEqual(phase, RunPhase.PLANNING)

    def test_review_follow_up_queues_another_implementation_run(self):
        status, phase = follow_up_target(CardStatus.REVIEW_READY)

        self.assertEqual(status, CardStatus.IMPLEMENTATION_QUEUED)
        self.assertEqual(phase, RunPhase.IMPLEMENTATION)

    def test_running_card_does_not_accept_follow_up(self):
        with self.assertRaisesRegex(
            InvalidCardTransitionError,
            "does not accept follow-up",
        ):
            follow_up_target(CardStatus.IMPLEMENTING)

    def test_active_work_can_be_temporarily_blocked_and_resumed(self):
        require_card_transition(CardStatus.PLANNING, CardStatus.BLOCKED)
        require_card_transition(CardStatus.BLOCKED, CardStatus.PLANNING)

    def test_blocked_card_can_be_cancelled_but_not_closed_directly(self):
        require_card_transition(CardStatus.BLOCKED, CardStatus.CANCELLED)

        with self.assertRaises(InvalidCardTransitionError):
            require_card_transition(CardStatus.BLOCKED, CardStatus.CLOSED)

    def test_phase_maps_to_queued_and_active_card_states(self):
        self.assertEqual(
            queued_card_status_for_phase(RunPhase.IMPLEMENTATION),
            CardStatus.IMPLEMENTATION_QUEUED,
        )
        self.assertEqual(
            active_card_status_for_phase(RunPhase.IMPLEMENTATION),
            CardStatus.IMPLEMENTING,
        )

    def test_run_completion_status_is_limited_by_phase(self):
        require_run_completion_status(
            RunPhase.PLANNING,
            CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
        )

        with self.assertRaises(InvalidRunCompletionError):
            require_run_completion_status(
                RunPhase.PLANNING,
                CardStatus.REVIEW_READY,
            )

    def test_only_completed_cancelled_and_closed_release_the_card_slot(self):
        self.assertEqual(
            TERMINAL_CARD_STATUSES,
            {
                CardStatus.COMPLETED,
                CardStatus.CANCELLED,
                CardStatus.CLOSED,
            },
        )

    def test_repository_scope_must_be_resolved_before_implementation(self):
        self.assertEqual(
            require_resolved_repository_scope("android"),
            RepositoryScope.ANDROID,
        )

        with self.assertRaisesRegex(InvalidCardTransitionError, "resolved"):
            require_resolved_repository_scope("auto")

    def test_backend_scope_gate_remains_exact(self):
        self.assertEqual(
            require_backend_repository_scope(
                "backend",
                action="Backend deployment",
            ),
            RepositoryScope.BACKEND,
        )

        with self.assertRaisesRegex(
            InvalidCardTransitionError,
            "requires repository_scope=backend",
        ):
            require_backend_repository_scope(
                "android",
                action="Backend deployment",
            )

    def test_deployment_scope_allows_backend_and_android_only(self):
        self.assertEqual(
            require_deployment_repository_scope(
                "backend",
                action="Deployment approval",
            ),
            RepositoryScope.BACKEND,
        )
        self.assertEqual(
            require_deployment_repository_scope(
                "android",
                action="Deployment approval",
            ),
            RepositoryScope.ANDROID,
        )
        with self.assertRaisesRegex(
            InvalidCardTransitionError,
            "combined backend-and-Android",
        ):
            require_deployment_repository_scope(
                "backend_and_android",
                action="Deployment approval",
            )


if __name__ == "__main__":
    unittest.main()

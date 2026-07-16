import unittest

from backend.core.agent_state import (
    ALLOWED_CARD_TRANSITIONS,
    TERMINAL_CARD_STATUSES,
    CardStatus,
    InvalidCardTransitionError,
    RunPhase,
    follow_up_target,
    require_card_transition,
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

    def test_only_completed_cancelled_and_closed_release_the_card_slot(self):
        self.assertEqual(
            TERMINAL_CARD_STATUSES,
            {
                CardStatus.COMPLETED,
                CardStatus.CANCELLED,
                CardStatus.CLOSED,
            },
        )


if __name__ == "__main__":
    unittest.main()

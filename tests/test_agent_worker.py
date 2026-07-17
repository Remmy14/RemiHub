import os
import threading
import unittest
from unittest.mock import MagicMock, patch

from backend.agent_worker import AgentWorkerSettings, build_executor
from backend.core.agent_state import CardStatus, RunPhase
from backend.core.agent_worker import (
    AgentLeaseLostError,
    AgentTemporarilyBlockedError,
    AgentWorker,
    AgentWorkerConfigurationError,
    ClaimedRun,
    ExecutionResult,
    FakeAgentExecutor,
)


def _wait_until(predicate, *, timeout: float) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def claimed_run(
    *,
    phase: RunPhase = RunPhase.PLANNING,
    attempt_count: int = 1,
) -> ClaimedRun:
    active_status = {
        RunPhase.PLANNING: CardStatus.PLANNING,
        RunPhase.IMPLEMENTATION: CardStatus.IMPLEMENTING,
        RunPhase.DEPLOYMENT: CardStatus.DEPLOYING,
    }[phase]
    return ClaimedRun(
        id="4c0056d9-cfab-4a7e-b8a8-369ea90efee8",
        card_id="3d8549c4-a965-4d2e-aacf-9df7e6ccdbb4",
        phase=phase,
        card_status=active_status,
        card_revision=1,
        attempt_count=attempt_count,
        lease_token="a65bce12-7ab7-47a9-9e93-cb0a58fd49ea",
        worker_id="qa-worker",
        title="Medication tracking",
        description="Plan a medication tracking module.",
        messages=(),
    )


class AgentWorkerOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.queue = MagicMock()
        self.executor = MagicMock()
        self.executor.allowed_phases = frozenset({RunPhase.PLANNING})
        self.worker = AgentWorker(
            queue=self.queue,
            executor=self.executor,
            worker_id="qa-worker",
            lease_seconds=120,
            heartbeat_seconds=30,
            max_attempts=3,
        )

    def test_empty_queue_returns_false(self):
        self.queue.claim_next_run.return_value = None

        self.assertFalse(self.worker.process_once())
        self.queue.claim_next_run.assert_called_once_with(
            worker_id="qa-worker",
            lease_seconds=120,
            allowed_phases=frozenset({RunPhase.PLANNING}),
        )
        self.queue.start_run.assert_not_called()

    def test_successful_execution_completes_run(self):
        claim = claimed_run()
        result = ExecutionResult(
            message="Plan ready",
            card_status=CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
        )
        self.queue.claim_next_run.return_value = claim
        self.executor.execute.return_value = result

        self.assertTrue(self.worker.process_once())

        self.queue.start_run.assert_called_once_with(
            claim,
            lease_seconds=120,
        )
        self.executor.execute.assert_called_once_with(claim)
        self.queue.complete_run.assert_called_once_with(claim, result)
        self.queue.fail_run.assert_not_called()

    def test_long_execution_renews_lease(self):
        claim = claimed_run()
        result = ExecutionResult(
            message="Plan ready",
            card_status=CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
        )
        execution_started = threading.Event()
        execution_release = threading.Event()

        def execute(_claim):
            execution_started.set()
            self.assertTrue(execution_release.wait(timeout=2))
            return result

        self.worker.heartbeat_seconds = 0.01
        self.queue.claim_next_run.return_value = claim
        self.executor.execute.side_effect = execute

        worker_thread = threading.Thread(target=self.worker.process_once)
        worker_thread.start()
        self.assertTrue(execution_started.wait(timeout=1))
        self.assertTrue(
            _wait_until(
                lambda: self.queue.heartbeat_run.call_count >= 1,
                timeout=1,
            )
        )
        execution_release.set()
        worker_thread.join(timeout=2)

        self.assertFalse(worker_thread.is_alive())
        self.queue.complete_run.assert_called_once_with(claim, result)

    def test_lease_loss_during_execution_fences_completion(self):
        claim = claimed_run()
        execution_started = threading.Event()
        execution_release = threading.Event()

        def execute(_claim):
            execution_started.set()
            self.assertTrue(execution_release.wait(timeout=2))
            return ExecutionResult(
                message="Plan ready",
                card_status=CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
            )

        self.worker.heartbeat_seconds = 0.01
        self.queue.claim_next_run.return_value = claim
        self.queue.heartbeat_run.side_effect = AgentLeaseLostError("reclaimed")
        self.executor.execute.side_effect = execute

        with self.assertLogs("remihub.agent_worker", level="WARNING"):
            worker_thread = threading.Thread(target=self.worker.process_once)
            worker_thread.start()
            self.assertTrue(execution_started.wait(timeout=1))
            self.assertTrue(
                _wait_until(
                    lambda: self.queue.heartbeat_run.call_count >= 1,
                    timeout=1,
                )
            )
            execution_release.set()
            worker_thread.join(timeout=2)

        self.queue.complete_run.assert_not_called()
        self.queue.fail_run.assert_not_called()

    def test_temporary_limit_blocks_run_for_retry(self):
        claim = claimed_run()
        self.queue.claim_next_run.return_value = claim
        self.executor.execute.side_effect = AgentTemporarilyBlockedError(
            "Usage limit reached",
            retry_after_seconds=900,
        )

        self.assertTrue(self.worker.process_once())

        self.queue.block_run.assert_called_once_with(
            claim,
            reason="Usage limit reached",
            retry_after_seconds=900,
        )
        self.queue.fail_run.assert_not_called()

    def test_executor_error_marks_run_failed(self):
        claim = claimed_run()
        self.queue.claim_next_run.return_value = claim
        self.executor.execute.side_effect = RuntimeError("executor exploded")

        with self.assertLogs("remihub.agent_worker", level="ERROR"):
            self.assertTrue(self.worker.process_once())

        self.queue.fail_run.assert_called_once_with(
            claim,
            error_message="RuntimeError: executor exploded",
        )

    def test_executor_failure_cannot_overwrite_reclaimed_run(self):
        claim = claimed_run()
        self.queue.claim_next_run.return_value = claim
        self.executor.execute.side_effect = RuntimeError("executor exploded")
        self.queue.fail_run.side_effect = AgentLeaseLostError("reclaimed")

        with self.assertLogs("remihub.agent_worker", level="WARNING"):
            self.assertTrue(self.worker.process_once())

        self.queue.fail_run.assert_called_once()

    def test_completion_database_error_is_not_reclassified(self):
        claim = claimed_run()
        result = ExecutionResult(
            message="Plan ready",
            card_status=CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
        )
        self.queue.claim_next_run.return_value = claim
        self.executor.execute.return_value = result
        self.queue.complete_run.side_effect = RuntimeError("database unavailable")

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            self.worker.process_once()

        self.queue.fail_run.assert_not_called()

    def test_stale_worker_does_not_fail_reclaimed_run(self):
        claim = claimed_run()
        self.queue.claim_next_run.return_value = claim
        self.queue.start_run.side_effect = AgentLeaseLostError("reclaimed")

        with self.assertLogs("remihub.agent_worker", level="WARNING"):
            self.assertTrue(self.worker.process_once())

        self.executor.execute.assert_not_called()
        self.queue.fail_run.assert_not_called()

    def test_maximum_attempts_fails_without_execution(self):
        claim = claimed_run(attempt_count=4)
        self.queue.claim_next_run.return_value = claim

        self.assertTrue(self.worker.process_once())

        self.executor.execute.assert_not_called()
        self.queue.fail_run.assert_called_once_with(
            claim,
            error_message="Maximum worker attempts exceeded (3)",
        )


class FakeAgentExecutorTests(unittest.TestCase):
    def test_fake_executor_returns_phase_appropriate_states(self):
        executor = FakeAgentExecutor()

        self.assertEqual(
            executor.execute(claimed_run(phase=RunPhase.PLANNING)).card_status,
            CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
        )
        self.assertEqual(
            executor.execute(claimed_run(phase=RunPhase.IMPLEMENTATION)).card_status,
            CardStatus.REVIEW_READY,
        )
        self.assertEqual(
            executor.execute(claimed_run(phase=RunPhase.DEPLOYMENT)).card_status,
            CardStatus.COMPLETED,
        )


class AgentWorkerSettingsTests(unittest.TestCase):
    def test_worker_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = AgentWorkerSettings.from_environment()

        self.assertEqual(settings.environment, "production")
        self.assertEqual(settings.executor_name, "disabled")
        with self.assertRaises(AgentWorkerConfigurationError):
            build_executor(settings)

    def test_fake_executor_requires_qa_and_explicit_gate(self):
        with patch.dict(
            os.environ,
            {
                "REMIHUB_AGENT_ENVIRONMENT": "qa",
                "REMIHUB_AGENT_EXECUTOR": "fake",
                "REMIHUB_AGENT_ALLOW_FAKE_EXECUTOR": "true",
            },
            clear=True,
        ):
            settings = AgentWorkerSettings.from_environment()

        self.assertIsInstance(build_executor(settings), FakeAgentExecutor)

    def test_fake_executor_is_rejected_in_production(self):
        with patch.dict(
            os.environ,
            {
                "REMIHUB_AGENT_ENVIRONMENT": "production",
                "REMIHUB_AGENT_EXECUTOR": "fake",
                "REMIHUB_AGENT_ALLOW_FAKE_EXECUTOR": "true",
            },
            clear=True,
        ):
            settings = AgentWorkerSettings.from_environment()

        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "restricted to QA",
        ):
            build_executor(settings)


if __name__ == "__main__":
    unittest.main()

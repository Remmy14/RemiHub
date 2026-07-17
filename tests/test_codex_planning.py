import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.core.agent_state import CardStatus, RunPhase
from backend.core.agent_worker import (
    AgentTemporarilyBlockedError,
    AgentWorkerConfigurationError,
)
from backend.core.codex_planning import (
    CodexPlanningExecutor,
    CodexPlanningTurn,
    CodexTemporaryFailure,
    OpenAICodexPlanningGateway,
    PLANNING_OUTPUT_SCHEMA,
    _parse_planning_response,
)
from tests.test_agent_worker import claimed_run


class RecordingGateway:
    def __init__(self, *, response: dict | None = None, error: Exception | None = None):
        self.response = response or {
            "response_markdown": "## Plan\n\n1. Add the module.",
            "ready_for_implementation": True,
        }
        self.error = error
        self.calls = []

    def run_turn(self, **arguments):
        self.calls.append(arguments)
        if self.error:
            raise self.error
        thread_id = arguments["existing_thread_id"] or "thr_new"
        if arguments["existing_thread_id"] is None:
            arguments["on_thread_created"](thread_id)
        return CodexPlanningTurn(
            thread_id=thread_id,
            turn_id="turn_123",
            final_response=json.dumps(self.response),
            duration_ms=1234,
            sdk_version="0.1.0b3",
            usage={
                "last": {
                    "input_tokens": 100,
                    "cached_input_tokens": 25,
                    "output_tokens": 50,
                    "reasoning_output_tokens": 10,
                    "total_tokens": 150,
                },
                "total": {
                    "input_tokens": 100,
                    "cached_input_tokens": 25,
                    "output_tokens": 50,
                    "reasoning_output_tokens": 10,
                    "total_tokens": 150,
                },
                "model_context_window": 200000,
            },
        )


class CodexPlanningExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repository = Path(self.temporary_directory.name)
        (self.repository / ".git").write_text("gitdir: /tmp/example\n")
        self.thread_store = MagicMock()

    def executor(self, gateway):
        return CodexPlanningExecutor(
            repository_path=self.repository,
            thread_store=self.thread_store,
            model="gpt-test",
            retry_after_seconds=600,
            gateway=gateway,
        )

    def test_new_planning_thread_is_persisted_before_completion(self):
        gateway = RecordingGateway()

        result = self.executor(gateway).execute(claimed_run())

        self.assertEqual(
            result.card_status,
            CardStatus.AWAITING_IMPLEMENTATION_APPROVAL,
        )
        self.assertIn("Add the module", result.message)
        self.assertEqual(result.metadata["sandbox"], "read-only")
        self.assertEqual(result.metadata["thread_id"], "thr_new")
        self.assertEqual(
            result.metadata["usage"]["last"]["total_tokens"],
            150,
        )
        self.thread_store.persist_codex_thread_id.assert_called_once()
        call = gateway.calls[0]
        self.assertIsNone(call["existing_thread_id"])
        self.assertEqual(call["repository_path"], self.repository.resolve())
        self.assertEqual(call["model"], "gpt-test")

    def test_existing_thread_is_resumed_with_latest_user_message(self):
        gateway = RecordingGateway(
            response={
                "response_markdown": "I still need one answer.",
                "ready_for_implementation": False,
            }
        )
        claim = claimed_run()
        claim = type(claim)(
            **{
                **claim.__dict__,
                "codex_thread_id": "thr_existing",
                "messages": (
                    {"author_type": "user", "content": "Original request"},
                    {"author_type": "agent", "content": "Which database?"},
                    {"author_type": "user", "content": "Use PostgreSQL."},
                ),
            }
        )

        result = self.executor(gateway).execute(claim)

        self.assertEqual(result.card_status, CardStatus.AWAITING_FEEDBACK)
        self.assertEqual(
            gateway.calls[0]["existing_thread_id"],
            "thr_existing",
        )
        self.assertIn("Use PostgreSQL.", gateway.calls[0]["prompt"])
        self.thread_store.persist_codex_thread_id.assert_not_called()

    def test_temporary_sdk_failure_blocks_instead_of_failing(self):
        gateway = RecordingGateway(
            error=CodexTemporaryFailure("usage limit reached")
        )

        with self.assertRaises(AgentTemporarilyBlockedError) as raised:
            self.executor(gateway).execute(claimed_run())

        self.assertEqual(raised.exception.retry_after_seconds, 600)

    def test_implementation_phase_is_rejected(self):
        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "cannot run implementation",
        ):
            self.executor(RecordingGateway()).execute(
                claimed_run(phase=RunPhase.IMPLEMENTATION)
            )

    def test_executor_advertises_only_planning(self):
        self.assertEqual(
            self.executor(RecordingGateway()).allowed_phases,
            frozenset({RunPhase.PLANNING}),
        )


class PlanningResponseTests(unittest.TestCase):
    def test_invalid_json_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "invalid structured"):
            _parse_planning_response("not json")

    def test_schema_requires_readiness_decision(self):
        with self.assertRaisesRegex(RuntimeError, "ready_for_implementation"):
            _parse_planning_response(
                json.dumps({"response_markdown": "A plan"})
            )


class OpenAICodexGatewayTests(unittest.TestCase):
    def test_gateway_denies_approvals_and_reasserts_read_only_sandbox(self):
        calls = {}

        class ApprovalMode:
            deny_all = "deny-all"

        class Sandbox:
            read_only = "read-only"

        class Result:
            id = "turn_456"
            final_response = json.dumps(
                {
                    "response_markdown": "Plan ready",
                    "ready_for_implementation": True,
                }
            )
            duration_ms = 50
            usage = None

        class Thread:
            id = "thr_456"

            def run(self, prompt, **arguments):
                calls["run"] = (prompt, arguments)
                return Result()

        class Codex:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def thread_start(self, **arguments):
                calls["start"] = arguments
                return Thread()

        fake_sdk = types.ModuleType("openai_codex")
        fake_sdk.__version__ = "test-sdk"
        fake_sdk.ApprovalMode = ApprovalMode
        fake_sdk.Codex = Codex
        fake_sdk.Sandbox = Sandbox
        fake_sdk.is_retryable_error = lambda _exc: False

        with patch.dict(sys.modules, {"openai_codex": fake_sdk}):
            created = []
            result = OpenAICodexPlanningGateway().run_turn(
                existing_thread_id=None,
                repository_path=Path("/tmp/repository"),
                prompt="Plan this",
                model=None,
                on_thread_created=created.append,
            )

        self.assertEqual(created, ["thr_456"])
        self.assertEqual(calls["start"]["approval_mode"], ApprovalMode.deny_all)
        self.assertEqual(calls["start"]["sandbox"], Sandbox.read_only)
        self.assertFalse(calls["start"]["ephemeral"])
        self.assertEqual(calls["run"][1]["approval_mode"], ApprovalMode.deny_all)
        self.assertEqual(calls["run"][1]["sandbox"], Sandbox.read_only)
        self.assertEqual(calls["run"][1]["output_schema"], PLANNING_OUTPUT_SCHEMA)
        self.assertEqual(result.thread_id, "thr_456")


if __name__ == "__main__":
    unittest.main()

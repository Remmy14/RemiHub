import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.core.agent_state import CardStatus, RepositoryScope, RunPhase
from backend.core.agent_worker import (
    AgentTemporarilyBlockedError,
    AgentWorkerConfigurationError,
)
from backend.core.codex_planning import (
    CodexPlanningExecutor,
    CodexPlanningTurn,
    CodexRemoteCompactNotFound,
    CodexTemporaryFailure,
    OpenAICodexPlanningGateway,
    PLANNING_OUTPUT_SCHEMA,
    _is_remote_compact_not_found,
    _parse_planning_response,
)
from tests.test_agent_worker import claimed_run


class RecordingGateway:
    def __init__(self, *, response: dict | None = None, error: Exception | None = None):
        self.response = response or {
            "response_markdown": "## Plan\n\n1. Add the module.",
            "ready_for_implementation": True,
            "repository_scope": "backend",
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
        self.assertEqual(result.metadata["repository_scope"], "backend")
        self.assertEqual(result.repository_scope, RepositoryScope.BACKEND)
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
                "repository_scope": "backend_and_android",
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
            result.repository_scope,
            RepositoryScope.BACKEND_AND_ANDROID,
        )
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

    def test_remote_compact_404_rolls_thread_and_continues_planning(self):
        class RolloverGateway(RecordingGateway):
            def __init__(self):
                super().__init__()
                self.first = True

            def run_turn(self, **arguments):
                if self.first:
                    self.first = False
                    self.calls.append(arguments)
                    raise CodexRemoteCompactNotFound(
                        "Error running remote compact task: unexpected status "
                        "404 Not Found, url: https://chatgpt.com/backend-api/"
                        "codex/responses/compact"
                    )
                return super().run_turn(**arguments)

        gateway = RolloverGateway()
        claim = claimed_run()
        claim = type(claim)(
            **{
                **claim.__dict__,
                "codex_thread_id": "thr_existing",
                "messages": (
                    {"author_type": "user", "content": "Original request"},
                    {"author_type": "agent", "content": "Earlier plan"},
                    {"author_type": "user", "content": "Continue with this"},
                ),
            }
        )

        result = self.executor(gateway).execute(claim)

        self.assertEqual(result.card_status, CardStatus.AWAITING_IMPLEMENTATION_APPROVAL)
        self.assertEqual(gateway.calls[0]["existing_thread_id"], "thr_existing")
        self.assertIsNone(gateway.calls[1]["existing_thread_id"])
        self.assertIn("rolled over automatically", gateway.calls[1]["prompt"])
        self.thread_store.rollover_codex_thread_id.assert_called_once_with(
            claim,
            old_thread_id="thr_existing",
            new_thread_id="thr_new",
            reason="remote_compact_404",
        )
        self.assertEqual(
            result.metadata["thread_rollover"],
            {
                "old_thread_id": "thr_existing",
                "new_thread_id": "thr_new",
                "reason": "remote_compact_404",
            },
        )

    def test_second_remote_compact_failure_is_not_retried_again(self):
        class AlwaysCompactGateway:
            def __init__(self):
                self.calls = []

            def run_turn(self, **arguments):
                self.calls.append(arguments)
                if arguments["existing_thread_id"] is None:
                    arguments["on_thread_created"]("thr_new")
                raise CodexRemoteCompactNotFound(
                    "Error running remote compact task: unexpected status "
                    "404 Not Found, url: /codex/responses/compact"
                )

        claim = claimed_run()
        claim = type(claim)(**{**claim.__dict__, "codex_thread_id": "thr_existing"})
        gateway = AlwaysCompactGateway()

        with self.assertRaises(CodexRemoteCompactNotFound):
            self.executor(gateway).execute(claim)

        self.assertEqual(len(gateway.calls), 2)
        self.thread_store.rollover_codex_thread_id.assert_called_once()

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

    def test_dual_repository_workspace_is_used_as_cwd(self):
        workspace = self.repository / "workspace"
        backend = workspace / "backend"
        android = workspace / "android"
        backend.mkdir(parents=True)
        android.mkdir()
        (backend / ".git").write_text("gitdir: /tmp/backend\n")
        (android / ".git").write_text("gitdir: /tmp/android\n")
        gateway = RecordingGateway(response={
            "response_markdown": "Android client-only plan.",
            "ready_for_implementation": True,
            "repository_scope": "android",
        })
        executor = CodexPlanningExecutor(
            planning_workspace_path=workspace,
            backend_repository_path=backend,
            android_repository_path=android,
            thread_store=self.thread_store,
            gateway=gateway,
        )

        result = executor.execute(claimed_run())

        self.assertEqual(gateway.calls[0]["repository_path"], workspace.resolve())
        self.assertIn("backend/", gateway.calls[0]["prompt"])
        self.assertIn("android/", gateway.calls[0]["prompt"])
        self.assertEqual(result.repository_scope, RepositoryScope.ANDROID)
        self.assertEqual(
            result.metadata["planning_workspace"]["mode"],
            "dual-repository",
        )

    def test_labeled_repositories_must_be_workspace_children(self):
        workspace = self.repository / "workspace"
        backend = self.repository / "backend"
        android = workspace / "android"
        for path in (workspace, backend, android):
            path.mkdir(parents=True, exist_ok=True)
        (backend / ".git").write_text("gitdir: /tmp/backend\n")
        (android / ".git").write_text("gitdir: /tmp/android\n")

        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "backend child",
        ):
            CodexPlanningExecutor(
                planning_workspace_path=workspace,
                backend_repository_path=backend,
                android_repository_path=android,
                thread_store=self.thread_store,
                gateway=RecordingGateway(),
            )


class CodexPlanningCompactionClassifierTests(unittest.TestCase):
    def test_exact_remote_compact_404_is_recognized(self):
        error = RuntimeError(
            "Error running remote compact task: unexpected status 404 Not Found: "
            '{"detail":"Not Found"}, url: https://chatgpt.com/backend-api/'
            "codex/responses/compact"
        )
        self.assertTrue(_is_remote_compact_not_found(error))

    def test_arbitrary_404_is_not_recognized(self):
        self.assertFalse(
            _is_remote_compact_not_found(
                RuntimeError("404 Not Found: /some/other/path")
            )
        )


class PlanningResponseTests(unittest.TestCase):
    def test_invalid_json_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "invalid structured"):
            _parse_planning_response("not json")

    def test_schema_requires_readiness_decision(self):
        with self.assertRaisesRegex(RuntimeError, "ready_for_implementation"):
            _parse_planning_response(
                json.dumps(
                    {
                        "response_markdown": "A plan",
                        "repository_scope": "backend",
                    }
                )
            )

    def test_schema_requires_resolved_repository_scope(self):
        with self.assertRaisesRegex(RuntimeError, "repository_scope"):
            _parse_planning_response(
                json.dumps(
                    {
                        "response_markdown": "A plan",
                        "ready_for_implementation": True,
                    }
                )
            )
        with self.assertRaisesRegex(RuntimeError, "repository_scope"):
            _parse_planning_response(
                json.dumps(
                    {
                        "response_markdown": "A plan",
                        "ready_for_implementation": True,
                        "repository_scope": "auto",
                    }
                )
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
                    "repository_scope": "backend",
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

import json
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.core.agent_state import CardStatus, RepositoryScope, RunPhase
from backend.core.agent_worker import (
    AgentTemporarilyBlockedError,
    AgentWorkerConfigurationError,
)
from backend.core.agent_workspace import ImplementationWorkspace, WorkspaceSnapshot
from backend.core.codex_implementation import (
    IMPLEMENTATION_OUTPUT_SCHEMA,
    CodexImplementationExecutor,
    CodexImplementationRemoteCompactNotFound,
    CodexImplementationTemporaryFailure,
    CodexImplementationTurn,
    OpenAICodexImplementationGateway,
    _implementation_prompt,
    _is_remote_compact_not_found,
    _parse_implementation_response,
)
from tests.test_agent_worker import claimed_run


class RecordingWorkspaceManager:
    def __init__(self, root: Path):
        self.workspace = ImplementationWorkspace(
            source_repository=root / "source.git",
            path=root / "worktree",
            base_branch="main",
            feature_branch="agent/card-test",
            base_commit="base123",
        )
        self.workspace.path.mkdir()
        self.snapshot = WorkspaceSnapshot(
            branch=self.workspace.feature_branch,
            head_commit="base123",
            changed_files=("backend/example.py",),
            status_porcelain=" M backend/example.py",
            diff_stat=" backend/example.py | 1 +",
            patch_path=root / "run.patch",
            patch_size_bytes=123,
        )
        self.lock_calls = []
        self.capture_calls = []

    @contextmanager
    def locked_workspace(self, claim, *, persist_workspace):
        self.lock_calls.append(claim)
        persist_workspace(self.workspace.feature_branch, str(self.workspace.path))
        yield self.workspace

    def capture_snapshot(self, claim, workspace):
        self.capture_calls.append((claim, workspace))
        return self.snapshot


class RecordingGateway:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []
        self.interrupted = False

    def run_turn(self, **arguments):
        self.calls.append(arguments)
        if self.error:
            raise self.error
        thread_id = arguments["thread_id"] or "thr_rollover"
        if arguments["thread_id"] is None:
            arguments["on_thread_created"](thread_id)
        arguments["on_turn_control"](self._interrupt)
        return CodexImplementationTurn(
            thread_id=thread_id,
            turn_id="turn_implementation",
            final_response=json.dumps(
                {
                    "response_markdown": "Implemented the approved change.",
                    "tests": [
                        {
                            "command": "pytest -q tests/test_example.py",
                            "status": "passed",
                            "details": "1 passed",
                        }
                    ],
                }
            ),
            duration_ms=321,
            sdk_version="0.1.0b3",
            usage={"last": {"total_tokens": 42}},
        )

    def _interrupt(self):
        self.interrupted = True


class RecordingValidator:
    def __init__(self):
        self.calls = []

    def validate(self, *, claim, workspace):
        self.calls.append((claim, workspace))
        return {
            "success": True,
            "validator": "trusted_android_offline_gradle",
            "release_apk": {"signed": False},
        }


class CodexImplementationExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.manager = RecordingWorkspaceManager(self.root)
        self.store = MagicMock()
        self.claim = replace(
            claimed_run(phase=RunPhase.IMPLEMENTATION),
            codex_thread_id="thr_existing",
        )

    def executor(self, gateway):
        return CodexImplementationExecutor(
            workspace_manager=self.manager,
            workspace_store=self.store,
            model="gpt-test",
            retry_after_seconds=600,
            gateway=gateway,
        )

    def test_implementation_resumes_thread_and_stops_at_review_ready(self):
        gateway = RecordingGateway()

        result = self.executor(gateway).execute(self.claim)

        self.assertEqual(result.card_status, CardStatus.REVIEW_READY)
        self.assertIn("Implemented the approved change", result.message)
        self.assertIn("backend/example.py", result.message)
        self.assertEqual(result.metadata["sandbox"], "workspace-write")
        self.assertEqual(result.metadata["approval_mode"], "deny-all")
        self.assertEqual(result.metadata["thread_id"], "thr_existing")
        self.assertEqual(result.metadata["tests"][0]["status"], "passed")
        self.assertEqual(
            result.metadata["workspace"]["branch"],
            "agent/card-test",
        )
        self.store.persist_implementation_workspace.assert_called_once()
        self.assertEqual(gateway.calls[0]["thread_id"], "thr_existing")
        self.assertEqual(
            gateway.calls[0]["repository_path"],
            self.manager.workspace.path,
        )


    def test_retry_prompt_uses_latest_user_guidance(self):
        retry_guidance = (
            "Fix the Kotlin compiler errors and rerun authoritative validation."
        )
        claim = replace(
            self.claim,
            feature_branch="agent/card-test",
            messages=(
                {
                    "author_type": "user",
                    "content": "Original workout module request.",
                },
                {
                    "author_type": "assistant",
                    "content": "Implementation validation failed.",
                },
                {
                    "author_type": "user",
                    "content": retry_guidance,
                },
            ),
        )

        prompt = _implementation_prompt(claim, self.manager.workspace)

        self.assertIn(
            "Continue implementation in the existing card worktree",
            prompt,
        )
        self.assertIn(retry_guidance, prompt)
        self.assertNotIn("Original workout module request.", prompt)

    def test_android_scope_uses_trusted_validator(self):
        gateway = RecordingGateway()
        validator = RecordingValidator()
        claim = replace(
            self.claim,
            repository_scope=RepositoryScope.ANDROID,
        )
        executor = CodexImplementationExecutor(
            workspace_manager=self.manager,
            workspace_store=self.store,
            model="gpt-test",
            retry_after_seconds=600,
            gateway=gateway,
            repository_scope=RepositoryScope.ANDROID,
            validator=validator,
        )

        result = executor.execute(claim)

        self.assertEqual(
            result.metadata["repository_scope"],
            RepositoryScope.ANDROID.value,
        )
        self.assertFalse(
            result.metadata["trusted_validation"]["release_apk"]["signed"]
        )
        self.assertEqual(len(validator.calls), 1)

    def test_android_executor_rejects_backend_claim(self):
        executor = CodexImplementationExecutor(
            workspace_manager=self.manager,
            workspace_store=self.store,
            gateway=RecordingGateway(),
            repository_scope=RepositoryScope.ANDROID,
        )
        with self.assertRaisesRegex(ValueError, "requires repository_scope=android"):
            executor.execute(self.claim)

    def test_temporary_codex_failure_blocks_for_retry(self):
        gateway = RecordingGateway(
            error=CodexImplementationTemporaryFailure("usage limit reached")
        )

        with self.assertRaises(AgentTemporarilyBlockedError) as raised:
            self.executor(gateway).execute(self.claim)

        self.assertEqual(raised.exception.retry_after_seconds, 600)

    def test_remote_compact_404_rolls_thread_and_continues_same_run(self):
        class RolloverGateway(RecordingGateway):
            def __init__(self):
                super().__init__()
                self.first = True

            def run_turn(self, **arguments):
                if self.first:
                    self.first = False
                    self.calls.append(arguments)
                    raise CodexImplementationRemoteCompactNotFound(
                        "Error running remote compact task: unexpected status "
                        "404 Not Found, url: https://chatgpt.com/backend-api/"
                        "codex/responses/compact"
                    )
                return super().run_turn(**arguments)

        gateway = RolloverGateway()

        result = self.executor(gateway).execute(self.claim)

        self.assertEqual(result.card_status, CardStatus.REVIEW_READY)
        self.assertEqual(gateway.calls[0]["thread_id"], "thr_existing")
        self.assertIsNone(gateway.calls[1]["thread_id"])
        self.assertIn("may already have modified files", gateway.calls[1]["prompt"])
        self.assertIn("Do not reset", gateway.calls[1]["prompt"])
        self.store.rollover_codex_thread_id.assert_called_once_with(
            self.claim,
            old_thread_id="thr_existing",
            new_thread_id="thr_rollover",
            reason="remote_compact_404",
        )
        self.assertEqual(result.metadata["thread_id"], "thr_rollover")
        self.assertEqual(
            result.metadata["thread_rollover"],
            {
                "old_thread_id": "thr_existing",
                "new_thread_id": "thr_rollover",
                "reason": "remote_compact_404",
            },
        )

    def test_second_remote_compact_failure_is_not_retried_again(self):
        class AlwaysCompactGateway:
            def __init__(self):
                self.calls = []

            def run_turn(self, **arguments):
                self.calls.append(arguments)
                if arguments["thread_id"] is None:
                    arguments["on_thread_created"]("thr_rollover")
                raise CodexImplementationRemoteCompactNotFound(
                    "Error running remote compact task: unexpected status "
                    "404 Not Found, url: /codex/responses/compact"
                )

        gateway = AlwaysCompactGateway()

        with self.assertRaises(CodexImplementationRemoteCompactNotFound):
            self.executor(gateway).execute(self.claim)

        self.assertEqual(len(gateway.calls), 2)
        self.store.rollover_codex_thread_id.assert_called_once()

    def test_missing_planning_thread_fails_closed(self):
        with self.assertRaisesRegex(AgentWorkerConfigurationError, "persistent"):
            self.executor(RecordingGateway()).execute(
                replace(self.claim, codex_thread_id=None)
            )

    def test_unresolved_repository_scope_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "resolved"):
            self.executor(RecordingGateway()).execute(
                replace(self.claim, repository_scope=RepositoryScope.AUTO)
            )

    def test_non_implementation_phase_is_rejected(self):
        with self.assertRaisesRegex(AgentWorkerConfigurationError, "cannot run"):
            self.executor(RecordingGateway()).execute(
                claimed_run(phase=RunPhase.PLANNING)
            )

    def test_cancel_interrupts_only_active_matching_run(self):
        executor = self.executor(RecordingGateway())
        interrupted = []
        executor._set_turn_control(self.claim.id, lambda: interrupted.append(True))

        executor.cancel(replace(self.claim, id="different"))
        self.assertEqual(interrupted, [])

        executor.cancel(self.claim)
        self.assertEqual(interrupted, [True])


class CodexImplementationCompactionClassifierTests(unittest.TestCase):
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


class ImplementationResponseTests(unittest.TestCase):
    def test_invalid_json_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "invalid structured"):
            _parse_implementation_response("not json")

    def test_invalid_test_status_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "invalid status"):
            _parse_implementation_response(
                json.dumps(
                    {
                        "response_markdown": "Done",
                        "tests": [
                            {
                                "command": "pytest",
                                "status": "maybe",
                                "details": "unknown",
                            }
                        ],
                    }
                )
            )


class OpenAICodexImplementationGatewayTests(unittest.TestCase):
    def test_gateway_resumes_thread_with_workspace_write_and_interrupt_handle(self):
        calls = {}

        class ApprovalMode:
            deny_all = "deny-all"

        class Sandbox:
            workspace_write = "workspace-write"

        class CodexConfig:
            def __init__(self, **arguments):
                calls["config"] = arguments

        class Result:
            id = "turn_456"
            final_response = json.dumps(
                {"response_markdown": "Done", "tests": []}
            )
            duration_ms = 50
            usage = None

        class Handle:
            def interrupt(self):
                calls["interrupted"] = True

            def run(self):
                calls["run"] = True
                return Result()

        class Thread:
            id = "thr_existing"

            def turn(self, prompt, **arguments):
                calls["turn"] = (prompt, arguments)
                return Handle()

        class Codex:
            def __init__(self, config):
                calls["codex_config"] = config

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def thread_resume(self, thread_id, **arguments):
                calls["resume"] = (thread_id, arguments)
                return Thread()

        fake_sdk = types.ModuleType("openai_codex")
        fake_sdk.__version__ = "test-sdk"
        fake_sdk.ApprovalMode = ApprovalMode
        fake_sdk.Codex = Codex
        fake_sdk.CodexConfig = CodexConfig
        fake_sdk.Sandbox = Sandbox
        fake_sdk.is_retryable_error = lambda _exc: False

        controls = []
        with tempfile.NamedTemporaryFile() as wrapper_file:
            wrapper_path = Path(wrapper_file.name)
            wrapper_path.chmod(0o700)
            with patch.dict(sys.modules, {"openai_codex": fake_sdk}):
                result = OpenAICodexImplementationGateway(
                    codex_bin=str(wrapper_path)
                ).run_turn(
                    thread_id="thr_existing",
                    repository_path=Path("/tmp/worktree"),
                    prompt="Implement this",
                    model=None,
                    on_thread_created=lambda _thread_id: None,
                    on_turn_control=controls.append,
                )

        self.assertEqual(calls["resume"][0], "thr_existing")
        self.assertEqual(
            calls["config"]["codex_bin"],
            str(wrapper_path.resolve()),
        )
        self.assertEqual(calls["config"]["cwd"], "/tmp/worktree")
        self.assertEqual(calls["resume"][1]["approval_mode"], ApprovalMode.deny_all)
        self.assertEqual(calls["resume"][1]["sandbox"], Sandbox.workspace_write)
        self.assertEqual(calls["turn"][1]["approval_mode"], ApprovalMode.deny_all)
        self.assertEqual(calls["turn"][1]["sandbox"], Sandbox.workspace_write)
        self.assertEqual(
            calls["turn"][1]["output_schema"],
            IMPLEMENTATION_OUTPUT_SCHEMA,
        )
        self.assertTrue(callable(controls[0]))
        self.assertIsNone(controls[-1])
        self.assertEqual(result.thread_id, "thr_existing")

    def test_gateway_can_start_persistent_successor_thread(self):
        calls = {}

        class ApprovalMode:
            deny_all = "deny-all"

        class Sandbox:
            workspace_write = "workspace-write"

        class CodexConfig:
            def __init__(self, **arguments):
                calls["config"] = arguments

        class Result:
            id = "turn_new"
            final_response = json.dumps(
                {"response_markdown": "Done", "tests": []}
            )
            duration_ms = 10
            usage = None

        class Handle:
            def interrupt(self):
                return None

            def run(self):
                return Result()

        class Thread:
            id = "thr_new"

            def turn(self, _prompt, **_arguments):
                return Handle()

        class Codex:
            def __init__(self, _config):
                pass

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
        fake_sdk.CodexConfig = CodexConfig
        fake_sdk.Sandbox = Sandbox
        fake_sdk.is_retryable_error = lambda _exc: False

        created = []
        controls = []
        with tempfile.NamedTemporaryFile() as wrapper_file:
            wrapper_path = Path(wrapper_file.name)
            wrapper_path.chmod(0o700)
            with patch.dict(sys.modules, {"openai_codex": fake_sdk}):
                result = OpenAICodexImplementationGateway(
                    codex_bin=str(wrapper_path)
                ).run_turn(
                    thread_id=None,
                    repository_path=Path("/tmp/worktree"),
                    prompt="Continue after rollover",
                    model=None,
                    on_thread_created=created.append,
                    on_turn_control=controls.append,
                )

        self.assertEqual(created, ["thr_new"])
        self.assertFalse(calls["start"]["ephemeral"])
        self.assertEqual(result.thread_id, "thr_new")



if __name__ == "__main__":
    unittest.main()

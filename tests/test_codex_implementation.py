import json
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.core.agent_state import CardStatus, RunPhase
from backend.core.agent_worker import (
    AgentTemporarilyBlockedError,
    AgentWorkerConfigurationError,
)
from backend.core.agent_workspace import ImplementationWorkspace, WorkspaceSnapshot
from backend.core.codex_implementation import (
    IMPLEMENTATION_OUTPUT_SCHEMA,
    CodexImplementationExecutor,
    CodexImplementationTemporaryFailure,
    CodexImplementationTurn,
    OpenAICodexImplementationGateway,
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
        arguments["on_turn_control"](self._interrupt)
        return CodexImplementationTurn(
            thread_id=arguments["thread_id"],
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

    def test_temporary_codex_failure_blocks_for_retry(self):
        gateway = RecordingGateway(
            error=CodexImplementationTemporaryFailure("usage limit reached")
        )

        with self.assertRaises(AgentTemporarilyBlockedError) as raised:
            self.executor(gateway).execute(self.claim)

        self.assertEqual(raised.exception.retry_after_seconds, 600)

    def test_missing_planning_thread_fails_closed(self):
        with self.assertRaisesRegex(AgentWorkerConfigurationError, "persistent"):
            self.executor(RecordingGateway()).execute(
                replace(self.claim, codex_thread_id=None)
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


if __name__ == "__main__":
    unittest.main()

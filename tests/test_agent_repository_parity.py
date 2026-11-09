import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.core.agent_worker import AgentWorkerConfigurationError
from backend.core.codex_implementation import _require_backend_source_parity


class BackendGitParityGuardTests(unittest.TestCase):

    @patch("backend.core.codex_implementation.subprocess.run")
    def test_ok(self, run):
        run.return_value = SimpleNamespace(returncode=0, stderr="")
        _require_backend_source_parity()
        self.assertEqual(run.call_count, 1)

    @patch("backend.core.codex_implementation.subprocess.run")
    def test_mismatch_stops_implementation(self, run):
        run.return_value = SimpleNamespace(
            returncode=1,
            stderr="repository mismatch"
        )
        with self.assertRaisesRegex(
            AgentWorkerConfigurationError,
            "repository mismatch"
        ):
            _require_backend_source_parity()

    @patch(
        "backend.core.codex_implementation.subprocess.run",
        side_effect=OSError("missing")
    )
    def test_guard_missing_fails_closed(self, run):
        with self.assertRaises(AgentWorkerConfigurationError):
            _require_backend_source_parity()

from pathlib import Path
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from backend.core.agent_deployment_trigger import (
    AgentDeploymentTriggerError,
    trigger_deployment_worker,
)
from backend.core.agent_state import RepositoryScope


class AgentDeploymentTriggerTests(unittest.TestCase):
    @patch("backend.core.agent_deployment_trigger.subprocess.run")
    def test_android_trigger_uses_exact_noninteractive_helper_command(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "ok\n", "")

        trigger_deployment_worker(RepositoryScope.ANDROID)

        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/local/libexec/remihub-agent-deployment-trigger",
                "android",
            ],
        )
        self.assertNotIn("*", " ".join(command))
        self.assertFalse(run.call_args.kwargs["shell"] if "shell" in run.call_args.kwargs else False)

    @patch("backend.core.agent_deployment_trigger.subprocess.run")
    def test_backend_trigger_uses_exact_scope(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "ok\n", "")
        trigger_deployment_worker(RepositoryScope.BACKEND)
        self.assertEqual(run.call_args.args[0][-1], "backend")

    @patch("backend.core.agent_deployment_trigger.subprocess.run")
    def test_trigger_failure_is_actionable(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "", "denied\n")
        with self.assertRaisesRegex(AgentDeploymentTriggerError, "denied"):
            trigger_deployment_worker(RepositoryScope.ANDROID)

    def test_combined_scope_is_rejected(self):
        with self.assertRaises(AgentDeploymentTriggerError):
            trigger_deployment_worker(RepositoryScope.BACKEND_AND_ANDROID)


class DeploymentTriggerAssetTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_helper_has_exact_unit_allowlist(self):
        text = (self.ROOT / "deployments/agent_common/libexec/remihub-agent-deployment-trigger").read_text()
        self.assertIn('"backend": "remihub-agent-deployment-production.service"', text)
        self.assertIn('"android": "remihub-agent-android-deployment.service"', text)
        self.assertNotIn("shell=True", text)
        self.assertNotIn("safe.directory=*", text)

    def test_sudoers_grants_only_exact_helper_commands(self):
        text = (self.ROOT / "deployments/agent_common/sudoers/remihub-agent-deployment-trigger").read_text()
        self.assertIn("remihub-agent-deployment-trigger backend", text)
        self.assertIn("remihub-agent-deployment-trigger android", text)
        self.assertNotIn("*", text)
        self.assertNotIn("systemctl", text)

    def test_fallback_timers_target_exact_run_once_units(self):
        android = (self.ROOT / "deployments/agent_common/systemd/remihub-agent-android-deployment.timer").read_text()
        backend = (self.ROOT / "deployments/agent_common/systemd/remihub-agent-backend-deployment.timer").read_text()
        self.assertIn("Unit=remihub-agent-android-deployment.service", android)
        self.assertIn("Unit=remihub-agent-deployment-production.service", backend)
        self.assertIn("OnUnitInactiveSec=1min", android)
        self.assertIn("OnUnitInactiveSec=1min", backend)
        self.assertIn("Persistent=true", android)
        self.assertIn("Persistent=true", backend)


if __name__ == "__main__":
    unittest.main()

import os
from pathlib import Path
import unittest
from unittest.mock import ANY, patch

from backend.core.agent_deployment_trigger import (
    AgentDeploymentTriggerError,
    TRIGGER_DIRECTORY,
    TRIGGER_REQUESTS,
    trigger_deployment_worker,
)
from backend.core.agent_state import RepositoryScope


class AgentDeploymentTriggerTests(unittest.TestCase):
    @patch("backend.core.agent_deployment_trigger.os.close")
    @patch("backend.core.agent_deployment_trigger.os.fsync")
    @patch("backend.core.agent_deployment_trigger.os.write")
    @patch("backend.core.agent_deployment_trigger.os.open")
    def test_android_trigger_writes_exact_unprivileged_request(
        self,
        open_file,
        write,
        fsync,
        close,
    ):
        open_file.return_value = 17
        write.return_value = len(b"android\n")

        trigger_deployment_worker(RepositoryScope.ANDROID)

        request_path, flags, mode = open_file.call_args.args
        self.assertEqual(
            request_path,
            Path("/run/remihub-agent/deployment-trigger/android.request"),
        )
        self.assertEqual(mode, 0o640)
        self.assertTrue(flags & os.O_WRONLY)
        self.assertTrue(flags & os.O_CREAT)
        self.assertTrue(flags & os.O_TRUNC)
        if hasattr(os, "O_NOFOLLOW"):
            self.assertTrue(flags & os.O_NOFOLLOW)
        write.assert_called_once_with(17, ANY)
        self.assertEqual(bytes(write.call_args.args[1]), b"android\n")
        fsync.assert_called_once_with(17)
        close.assert_called_once_with(17)

    @patch("backend.core.agent_deployment_trigger.os.close")
    @patch("backend.core.agent_deployment_trigger.os.fsync")
    @patch("backend.core.agent_deployment_trigger.os.write")
    @patch("backend.core.agent_deployment_trigger.os.open")
    def test_backend_trigger_uses_exact_request_path(
        self,
        open_file,
        write,
        fsync,
        close,
    ):
        open_file.return_value = 19
        write.return_value = len(b"backend\n")

        trigger_deployment_worker(RepositoryScope.BACKEND)

        self.assertEqual(
            open_file.call_args.args[0],
            Path("/run/remihub-agent/deployment-trigger/backend.request"),
        )
        self.assertEqual(bytes(write.call_args.args[1]), b"backend\n")

    @patch("backend.core.agent_deployment_trigger.os.open")
    def test_trigger_failure_is_actionable(self, open_file):
        open_file.side_effect = PermissionError("denied")
        with self.assertRaisesRegex(AgentDeploymentTriggerError, "denied"):
            trigger_deployment_worker(RepositoryScope.ANDROID)

    def test_combined_scope_is_rejected(self):
        with self.assertRaises(AgentDeploymentTriggerError):
            trigger_deployment_worker(RepositoryScope.BACKEND_AND_ANDROID)


class DeploymentTriggerAssetTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_helper_has_exact_unit_allowlist(self):
        text = (
            self.ROOT
            / "deployments/agent_common/libexec/remihub-agent-deployment-trigger"
        ).read_text()
        self.assertIn(
            '"backend": "remihub-agent-deployment-production.service"',
            text,
        )
        self.assertIn(
            '"android": "remihub-agent-android-deployment.service"',
            text,
        )
        self.assertNotIn("shell=True", text)
        self.assertNotIn("safe.directory=*", text)

    def test_http_trigger_has_no_sudo_or_subprocess_boundary(self):
        text = (
            self.ROOT / "backend/core/agent_deployment_trigger.py"
        ).read_text()
        self.assertNotIn("/usr/bin/sudo", text)
        self.assertNotIn("subprocess", text)
        self.assertEqual(
            TRIGGER_DIRECTORY,
            Path("/run/remihub-agent/deployment-trigger"),
        )
        self.assertEqual(
            TRIGGER_REQUESTS[RepositoryScope.ANDROID],
            Path(
                "/run/remihub-agent/deployment-trigger/android.request"
            ),
        )
        self.assertEqual(
            TRIGGER_REQUESTS[RepositoryScope.BACKEND],
            Path(
                "/run/remihub-agent/deployment-trigger/backend.request"
            ),
        )

    def test_deprecated_sudoers_asset_is_removed(self):
        self.assertFalse(
            (
                self.ROOT
                / "deployments/agent_common/sudoers/"
                "remihub-agent-deployment-trigger"
            ).exists()
        )

    def test_tmpfiles_boundary_is_exact(self):
        text = (
            self.ROOT
            / "deployments/agent_common/tmpfiles/"
            "remihub-agent-deployment-trigger.conf"
        ).read_text()
        self.assertIn(
            "d /run/remihub-agent/deployment-trigger 0750 alex storage -",
            text,
        )
        self.assertNotIn("0777", text)

    def test_path_units_watch_exact_markers(self):
        systemd = self.ROOT / "deployments/agent_common/systemd"
        android_path = (
            systemd / "remihub-agent-android-deployment-trigger.path"
        ).read_text()
        backend_path = (
            systemd / "remihub-agent-backend-deployment-trigger.path"
        ).read_text()
        self.assertIn(
            "PathExists=/run/remihub-agent/deployment-trigger/android.request",
            android_path,
        )
        self.assertIn(
            "PathExists=/run/remihub-agent/deployment-trigger/backend.request",
            backend_path,
        )

    def test_trigger_services_use_fixed_root_helper_commands(self):
        systemd = self.ROOT / "deployments/agent_common/systemd"
        android = (
            systemd / "remihub-agent-android-deployment-trigger.service"
        ).read_text()
        backend = (
            systemd / "remihub-agent-backend-deployment-trigger.service"
        ).read_text()
        self.assertIn(
            "ExecStart=/usr/local/libexec/"
            "remihub-agent-deployment-trigger android",
            android,
        )
        self.assertIn(
            "ExecStart=/usr/local/libexec/"
            "remihub-agent-deployment-trigger backend",
            backend,
        )
        self.assertIn("NoNewPrivileges=true", android)
        self.assertIn("NoNewPrivileges=true", backend)
        self.assertNotIn("sudo", android)
        self.assertNotIn("sudo", backend)

    def test_fallback_timers_are_independent_calendar_polls(self):
        systemd = self.ROOT / "deployments/agent_common/systemd"
        android = (
            systemd / "remihub-agent-android-deployment.timer"
        ).read_text()
        backend = (
            systemd / "remihub-agent-backend-deployment.timer"
        ).read_text()
        self.assertIn("OnCalendar=*-*-* *:*:00", android)
        self.assertIn("OnCalendar=*-*-* *:*:30", backend)
        self.assertNotIn("OnUnitInactiveSec", android)
        self.assertNotIn("OnUnitInactiveSec", backend)
        self.assertIn(
            "Unit=remihub-agent-android-deployment.service",
            android,
        )
        self.assertIn(
            "Unit=remihub-agent-deployment-production.service",
            backend,
        )
        self.assertIn("Persistent=true", android)
        self.assertIn("Persistent=true", backend)

    def test_worker_grant_asset_is_column_limited(self):
        text = (
            self.ROOT
            / "deployments/agent_common/sql/"
            "agent-worker-card-update-grants.sql"
        ).read_text()
        self.assertIn(
            "GRANT UPDATE (repository_scope, base_branch) "
            "ON agent.cards",
            text,
        )
        self.assertNotIn("GRANT ALL", text)
        self.assertNotIn("title", text)
        self.assertNotIn("description", text)
        self.assertNotIn("DELETE", text)


if __name__ == "__main__":
    unittest.main()

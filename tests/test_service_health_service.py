import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

from pydantic import ValidationError

from backend.models.health_models import (
    HealthComponent,
    HealthComponentGroup,
    HealthComponentKind,
    HealthDependencyCheck,
    HealthStatus,
    ServiceHealthSnapshotResponse,
)
from backend.services import service_health_service as health


def unit_status(
    *,
    unit: str = "remihub.service",
    available: bool = True,
    load_state: str = "loaded",
    active_state: str = "active",
    sub_state: str = "running",
    result: str = "success",
    exec_main_code: str | int | None = "exited",
    exec_main_status: str | int | None = "0",
    unit_type: str = "simple",
) -> health.SystemdUnitStatus:
    return health.SystemdUnitStatus(
        unit=unit,
        available=available,
        properties={
            "LoadState": load_state,
            "ActiveState": active_state,
            "SubState": sub_state,
            "Result": result,
            "ExecMainCode": exec_main_code,
            "ExecMainStatus": exec_main_status,
            "Type": unit_type,
        },
    )


class ServiceHealthSystemdSemanticsTests(unittest.TestCase):
    def test_numeric_exec_main_code_with_zero_status_is_successful_exit(self):
        self.assertTrue(
            health._is_success_exit(
                unit_status(exec_main_code=1, exec_main_status=0)
            )
        )
        self.assertFalse(
            health._is_failed_exit(
                unit_status(exec_main_code=1, exec_main_status=0)
            )
        )

    def test_string_numeric_exec_main_code_with_zero_status_is_successful_exit(self):
        self.assertTrue(
            health._is_success_exit(
                unit_status(exec_main_code="1", exec_main_status="0")
            )
        )
        self.assertFalse(
            health._is_failed_exit(
                unit_status(exec_main_code="1", exec_main_status="0")
            )
        )

    def test_exited_exec_main_code_with_zero_status_remains_successful_exit(self):
        self.assertTrue(
            health._is_success_exit(
                unit_status(exec_main_code="exited", exec_main_status=0)
            )
        )
        self.assertFalse(
            health._is_failed_exit(
                unit_status(exec_main_code="exited", exec_main_status=0)
            )
        )

    def test_numeric_exec_main_code_with_nonzero_status_is_failed_exit(self):
        self.assertFalse(
            health._is_success_exit(
                unit_status(exec_main_code=1, exec_main_status="2")
            )
        )
        self.assertTrue(
            health._is_failed_exit(
                unit_status(exec_main_code=1, exec_main_status="2")
            )
        )

    def test_inventory_contains_all_mandatory_remihub_units(self):
        expected = {
            "remihub-agent-android-deployment.service",
            "remihub-agent-android-deployment.timer",
            "remihub-agent-android-deployment-trigger.path",
            "remihub-agent-android-deployment-trigger.service",
            "remihub-agent-android-implementation.service",
            "remihub-agent-backend-deployment.timer",
            "remihub-agent-backend-deployment-trigger.path",
            "remihub-agent-backend-deployment-trigger.service",
            "remihub-agent-backend-github-sync.service",
            "remihub-agent-deployment-production.service",
            "remihub-agent-deployment-qa.service",
            "remihub-agent-implementation-qa.service",
            "remihub-agent-implementation.service",
            "remihub-agent-planning-sync.service",
            "remihub-agent-worker.service",
            "remihub-backend-qa.service",
            "remihub-bindmount.service",
            "remihub.service",
        }

        self.assertTrue(expected.issubset(health.ALLOWED_SYSTEMD_UNITS))

    def test_inventory_contains_additional_first_class_units(self):
        expected = {
            "rh-storage.service",
            "mergerfs-pool1.service",
            "mergerfs-pool2.service",
            "caddy.service",
            "postgresql.service",
            "plexmediaserver.service",
            "remihub-health-collector.service",
            "remihub-health-collector.timer",
        }

        self.assertTrue(expected.issubset(health.ALLOWED_SYSTEMD_UNITS))

    def test_persistent_active_running_is_healthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(),
            health.ExpectedMode.PERSISTENT_RUNNING,
        )

        self.assertEqual(status, HealthStatus.HEALTHY)

    def test_persistent_inactive_is_unhealthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(active_state="inactive", sub_state="dead"),
            health.ExpectedMode.PERSISTENT_RUNNING,
        )

        self.assertEqual(status, HealthStatus.UNHEALTHY)

    def test_failed_unit_is_unhealthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(active_state="failed", sub_state="failed", result="exit-code"),
            health.ExpectedMode.PERSISTENT_RUNNING,
        )

        self.assertEqual(status, HealthStatus.UNHEALTHY)

    def test_unavailable_unit_is_unknown(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(available=False),
            health.ExpectedMode.PERSISTENT_RUNNING,
        )

        self.assertEqual(status, HealthStatus.UNKNOWN)

    def test_timer_or_path_active_waiting_is_healthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(
                unit="remihub-agent-backend-deployment.timer",
                active_state="active",
                sub_state="waiting",
                result="success",
            ),
            health.ExpectedMode.ARMED_TIMER_OR_PATH,
        )

        self.assertEqual(status, HealthStatus.HEALTHY)

    def test_timer_or_path_active_running_is_healthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(
                unit="remihub-agent-backend-deployment.timer",
                active_state="active",
                sub_state="running",
                result="success",
            ),
            health.ExpectedMode.ARMED_TIMER_OR_PATH,
        )

        self.assertEqual(status, HealthStatus.HEALTHY)

    def test_timer_or_path_inactive_is_unhealthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(active_state="inactive", sub_state="dead"),
            health.ExpectedMode.ARMED_TIMER_OR_PATH,
        )

        self.assertEqual(status, HealthStatus.UNHEALTHY)

    def test_timer_or_path_failed_state_is_unhealthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(
                active_state="failed",
                sub_state="failed",
                result="success",
            ),
            health.ExpectedMode.ARMED_TIMER_OR_PATH,
        )

        self.assertEqual(status, HealthStatus.UNHEALTHY)

    def test_timer_or_path_failed_result_is_unhealthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(
                active_state="active",
                sub_state="waiting",
                result="exit-code",
            ),
            health.ExpectedMode.ARMED_TIMER_OR_PATH,
        )

        self.assertEqual(status, HealthStatus.UNHEALTHY)

    def test_on_demand_inactive_success_is_idle(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(active_state="inactive", sub_state="dead"),
            health.ExpectedMode.ON_DEMAND,
        )

        self.assertEqual(status, HealthStatus.IDLE)

    def test_on_demand_inactive_dead_oneshot_success_is_idle_without_bad_message(self):
        status, message = health.evaluate_systemd_status(
            unit_status(
                unit="remihub-agent-deployment-production.service",
                active_state="inactive",
                sub_state="dead",
                result="success",
                exec_main_code="exited",
                exec_main_status="1",
                unit_type="oneshot",
            ),
            health.ExpectedMode.ON_DEMAND,
        )

        self.assertEqual(status, HealthStatus.IDLE)
        self.assertNotEqual(
            message,
            "On-demand unit last exit status was not successful",
        )

    def test_on_demand_active_running_is_healthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(active_state="active", sub_state="running"),
            health.ExpectedMode.ON_DEMAND,
        )

        self.assertEqual(status, HealthStatus.HEALTHY)

    def test_on_demand_prior_failed_invocation_is_unhealthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(
                active_state="inactive",
                sub_state="dead",
                result="exit-code",
                exec_main_status="1",
            ),
            health.ExpectedMode.ON_DEMAND,
        )

        self.assertEqual(status, HealthStatus.UNHEALTHY)

    def test_on_demand_nonzero_exit_status_is_unhealthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(
                active_state="inactive",
                sub_state="dead",
                result="success",
                exec_main_status="1",
            ),
            health.ExpectedMode.ON_DEMAND,
        )

        self.assertEqual(status, HealthStatus.UNHEALTHY)

    def test_on_demand_never_run_no_failure_is_idle(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(
                active_state="inactive",
                sub_state="dead",
                result="success",
                exec_main_code="",
                exec_main_status="",
            ),
            health.ExpectedMode.ON_DEMAND,
        )

        self.assertEqual(status, HealthStatus.IDLE)

    def test_on_demand_inactive_dead_numeric_successful_oneshot_is_idle(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(
                active_state="inactive",
                sub_state="dead",
                result="success",
                exec_main_code=1,
                exec_main_status=0,
                unit_type="oneshot",
            ),
            health.ExpectedMode.ON_DEMAND,
        )

        self.assertEqual(status, HealthStatus.IDLE)

    def test_health_collector_style_inactive_dead_successful_oneshot_is_idle(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(
                unit="remihub-health-collector.service",
                active_state="inactive",
                sub_state="dead",
                result="success",
                exec_main_code="1",
                exec_main_status="0",
                unit_type="oneshot",
            ),
            health.ExpectedMode.ON_DEMAND,
        )

        self.assertEqual(status, HealthStatus.IDLE)

    def test_on_demand_oneshot_activating_start_is_healthy(self):
        status, message = health.evaluate_systemd_status(
            unit_status(
                unit="remihub-health-collector.service",
                active_state="activating",
                sub_state="start",
                result="success",
                exec_main_code="",
                exec_main_status="",
                unit_type="oneshot",
            ),
            health.ExpectedMode.ON_DEMAND,
        )

        self.assertEqual(status, HealthStatus.HEALTHY)
        self.assertNotEqual(message, "On-demand unit state is not recognized")

    def test_health_collector_activating_start_oneshot_is_not_unknown(self):
        status, message = health.evaluate_systemd_status(
            unit_status(
                unit="remihub-health-collector.service",
                active_state="activating",
                sub_state="start",
                result="success",
                unit_type="oneshot",
            ),
            health.ExpectedMode.ON_DEMAND,
        )

        self.assertEqual(status, HealthStatus.HEALTHY)
        self.assertNotIn("not recognized", message)

    def test_unrecognized_on_demand_state_remains_unknown(self):
        status, message = health.evaluate_systemd_status(
            unit_status(
                active_state="reloading",
                sub_state="reload",
                result="success",
                exec_main_code="",
                exec_main_status="",
                unit_type="oneshot",
            ),
            health.ExpectedMode.ON_DEMAND,
        )

        self.assertEqual(status, HealthStatus.UNKNOWN)
        self.assertEqual(message, "On-demand unit state is not recognized")

    def test_qa_runtime_inactive_success_is_idle(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(active_state="inactive", sub_state="dead"),
            health.ExpectedMode.QA_RUNTIME,
        )

        self.assertEqual(status, HealthStatus.IDLE)

    def test_qa_runtime_failed_is_unhealthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(active_state="failed", sub_state="failed", result="exit-code"),
            health.ExpectedMode.QA_RUNTIME,
        )

        self.assertEqual(status, HealthStatus.UNHEALTHY)

    def test_oneshot_successful_exited_is_healthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(active_state="active", sub_state="exited"),
            health.ExpectedMode.ONESHOT_SUCCESS_EXITED,
        )

        self.assertEqual(status, HealthStatus.HEALTHY)

    def test_bindmount_style_numeric_successful_oneshot_is_healthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(
                unit="remihub-bindmount.service",
                active_state="active",
                sub_state="exited",
                result="success",
                exec_main_code="1",
                exec_main_status="0",
                unit_type="oneshot",
            ),
            health.ExpectedMode.ONESHOT_SUCCESS_EXITED,
        )

        self.assertEqual(status, HealthStatus.HEALTHY)

    def test_oneshot_failed_result_is_unhealthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(active_state="active", sub_state="exited", result="exit-code"),
            health.ExpectedMode.ONESHOT_SUCCESS_EXITED,
        )

        self.assertEqual(status, HealthStatus.UNHEALTHY)

    def test_oneshot_nonzero_exit_status_is_unhealthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(
                active_state="active",
                sub_state="exited",
                result="success",
                exec_main_status="1",
            ),
            health.ExpectedMode.ONESHOT_SUCCESS_EXITED,
        )

        self.assertEqual(status, HealthStatus.UNHEALTHY)

    def test_postgresql_active_exited_is_healthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(unit="postgresql.service", active_state="active", sub_state="exited"),
            health.ExpectedMode.POSTGRESQL,
        )

        self.assertEqual(status, HealthStatus.HEALTHY)


class ServiceHealthPathAndJDownloaderTests(unittest.TestCase):
    def jdownloader_runtime(self, *, uid: int = 4242) -> health.JDownloaderUserRuntime:
        home = "/home/alex"
        runtime_dir = f"/run/user/{uid}"
        return health.JDownloaderUserRuntime(
            username="alex",
            uid=uid,
            home=health.Path(home),
            runtime_dir=health.Path(runtime_dir),
            bus_path=health.Path(runtime_dir) / "bus",
            unit_file=health.Path(home) / ".config/systemd/user/jdownloader.service",
            enabled_link=(
                health.Path(home)
                / ".config/systemd/user/graphical-session.target.wants/"
                / "jdownloader.service"
            ),
        )

    def test_path_check_existing_directory_is_healthy(self):
        definition = health.PathDefinition(
            "tmp",
            "Temp",
            HealthComponentGroup.STORAGE,
            "/tmp",
            HealthComponentKind.PATH,
        )

        check = health._path_check(definition)

        self.assertEqual(check.status, HealthStatus.HEALTHY)

    def test_path_check_missing_path_is_unhealthy(self):
        definition = health.PathDefinition(
            "missing",
            "Missing",
            HealthComponentGroup.STORAGE,
            "/tmp/remihub-definitely-missing-health-path",
            HealthComponentKind.PATH,
        )

        check = health._path_check(definition)

        self.assertEqual(check.status, HealthStatus.UNHEALTHY)

    @patch("backend.services.service_health_service.Path.exists", side_effect=OSError("nope"))
    def test_path_check_exception_is_unknown(self, _exists):
        definition = health.PathDefinition(
            "unknown",
            "Unknown",
            HealthComponentGroup.STORAGE,
            "/tmp",
            HealthComponentKind.PATH,
        )

        check = health._path_check(definition)

        self.assertEqual(check.status, HealthStatus.UNKNOWN)

    @patch("backend.services.service_health_service.inspect_jdownloader_process")
    @patch("backend.services.service_health_service.inspect_jdownloader_user_service")
    @patch("backend.services.service_health_service._path_check")
    def test_jdownloader_healthy_runtime_and_paths_is_healthy(
        self,
        path_check,
        user_service,
        process,
    ):
        path_check.side_effect = lambda definition: health.HealthDependencyCheck(
            id=definition.id,
            name=definition.name,
            kind=definition.kind,
            status=HealthStatus.HEALTHY,
            message="ok",
            path=definition.path,
        )
        user_service.return_value = health.HealthDependencyCheck(
            id="user",
            name="User",
            kind=HealthComponentKind.USER_SYSTEMD_UNIT,
            status=HealthStatus.HEALTHY,
            message="running",
        )
        process.return_value = health.HealthDependencyCheck(
            id="process",
            name="Process",
            kind=HealthComponentKind.PROCESS,
            status=HealthStatus.HEALTHY,
            message="running",
        )

        component = health.evaluate_jdownloader_component(datetime.now(timezone.utc))

        self.assertEqual(component.status, HealthStatus.HEALTHY)

    @patch("backend.services.service_health_service.inspect_jdownloader_process")
    @patch("backend.services.service_health_service.inspect_jdownloader_user_service")
    @patch("backend.services.service_health_service._path_check")
    def test_jdownloader_missing_jar_is_unhealthy(
        self,
        path_check,
        user_service,
        process,
    ):
        def fake_path_check(definition):
            status = (
                HealthStatus.UNHEALTHY
                if definition.id == "jdownloader-jar-path"
                else HealthStatus.HEALTHY
            )
            return health.HealthDependencyCheck(
                id=definition.id,
                name=definition.name,
                kind=definition.kind,
                status=status,
                message="checked",
                path=definition.path,
            )

        path_check.side_effect = fake_path_check
        user_service.return_value = health.HealthDependencyCheck(
            id="user",
            name="User",
            kind=HealthComponentKind.USER_SYSTEMD_UNIT,
            status=HealthStatus.HEALTHY,
            message="running",
        )
        process.return_value = health.HealthDependencyCheck(
            id="process",
            name="Process",
            kind=HealthComponentKind.PROCESS,
            status=HealthStatus.HEALTHY,
            message="running",
        )

        component = health.evaluate_jdownloader_component(datetime.now(timezone.utc))

        self.assertEqual(component.status, HealthStatus.UNHEALTHY)

    @patch("backend.services.service_health_service.inspect_jdownloader_process")
    @patch("backend.services.service_health_service.inspect_jdownloader_user_service")
    @patch("backend.services.service_health_service._path_check")
    def test_jdownloader_stopped_runtime_is_degraded(
        self,
        path_check,
        user_service,
        process,
    ):
        path_check.side_effect = lambda definition: health.HealthDependencyCheck(
            id=definition.id,
            name=definition.name,
            kind=definition.kind,
            status=HealthStatus.HEALTHY,
            message="ok",
            path=definition.path,
        )
        user_service.return_value = health.HealthDependencyCheck(
            id="user",
            name="User",
            kind=HealthComponentKind.USER_SYSTEMD_UNIT,
            status=HealthStatus.DEGRADED,
            message="stopped",
        )
        process.return_value = health.HealthDependencyCheck(
            id="process",
            name="Process",
            kind=HealthComponentKind.PROCESS,
            status=HealthStatus.DEGRADED,
            message="not present",
        )

        component = health.evaluate_jdownloader_component(datetime.now(timezone.utc))

        self.assertEqual(component.status, HealthStatus.DEGRADED)

    @patch("backend.services.service_health_service.inspect_jdownloader_process")
    @patch("backend.services.service_health_service.inspect_jdownloader_user_service")
    @patch("backend.services.service_health_service._path_check")
    def test_jdownloader_user_manager_unavailable_is_degraded(
        self,
        path_check,
        user_service,
        process,
    ):
        path_check.side_effect = lambda definition: health.HealthDependencyCheck(
            id=definition.id,
            name=definition.name,
            kind=definition.kind,
            status=HealthStatus.HEALTHY,
            message="ok",
            path=definition.path,
        )
        user_service.return_value = health.HealthDependencyCheck(
            id="user",
            name="User",
            kind=HealthComponentKind.USER_SYSTEMD_UNIT,
            status=HealthStatus.UNKNOWN,
            message="unavailable",
        )
        process.return_value = health.HealthDependencyCheck(
            id="process",
            name="Process",
            kind=HealthComponentKind.PROCESS,
            status=HealthStatus.HEALTHY,
            message="running",
        )

        component = health.evaluate_jdownloader_component(datetime.now(timezone.utc))

        self.assertEqual(component.status, HealthStatus.DEGRADED)

    @patch("backend.services.service_health_service.pwd.getpwnam")
    def test_jdownloader_user_runtime_resolves_configured_username_and_uid(self, getpwnam):
        getpwnam.return_value = Mock(pw_uid=4242, pw_dir="/home/alex")

        runtime = health._jdownloader_user_runtime()

        getpwnam.assert_called_once_with("alex")
        self.assertEqual(runtime.uid, 4242)
        self.assertEqual(str(runtime.runtime_dir), "/run/user/4242")
        self.assertEqual(str(runtime.bus_path), "/run/user/4242/bus")
        self.assertNotEqual(str(runtime.runtime_dir), "/run/user/1000")

    @patch.dict("os.environ", {}, clear=True)
    @patch("backend.services.service_health_service.os.geteuid", return_value=4242)
    @patch("backend.services.service_health_service.subprocess.run")
    @patch("backend.services.service_health_service.Path.is_socket", return_value=True)
    @patch("backend.services.service_health_service.Path.is_dir", return_value=True)
    @patch("backend.services.service_health_service.Path.exists", return_value=True)
    @patch("backend.services.service_health_service.Path.is_file", return_value=True)
    @patch("backend.services.service_health_service._jdownloader_user_runtime")
    def test_jdownloader_user_service_running_is_healthy(
        self,
        runtime,
        _is_file,
        _exists,
        _is_dir,
        _is_socket,
        run,
        _geteuid,
    ):
        runtime.return_value = self.jdownloader_runtime()
        run.return_value = Mock(
            returncode=0,
            stdout=(
                "LoadState=loaded\n"
                "ActiveState=active\n"
                "SubState=running\n"
                "UnitFileState=enabled\n"
                "Result=success\n"
                "ExecMainCode=1\n"
                "ExecMainStatus=0\n"
            ),
            stderr="",
        )

        check = health.inspect_jdownloader_user_service()

        self.assertEqual(check.status, HealthStatus.HEALTHY)
        self.assertIn("load=loaded", check.observed)
        self.assertIn("active=active", check.observed)
        self.assertIn("sub=running", check.observed)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], [health.SYSTEMCTL, "--user", "show"])
        self.assertIn(health.JDOWNLOADER_SYSTEMD_UNIT_NAME, command)
        self.assertNotIn("start", command)
        self.assertNotIn("restart", command)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/4242")
        self.assertEqual(
            env["DBUS_SESSION_BUS_ADDRESS"],
            "unix:path=/run/user/4242/bus",
        )

    @patch.dict(
        "os.environ",
        {
            "XDG_RUNTIME_DIR": "/wrong",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/wrong/bus",
        },
        clear=True,
    )
    @patch("backend.services.service_health_service.os.geteuid", return_value=4242)
    @patch("backend.services.service_health_service.subprocess.run")
    @patch("backend.services.service_health_service.Path.is_socket", return_value=True)
    @patch("backend.services.service_health_service.Path.is_dir", return_value=True)
    @patch("backend.services.service_health_service.Path.exists", return_value=True)
    @patch("backend.services.service_health_service.Path.is_file", return_value=True)
    @patch("backend.services.service_health_service._jdownloader_user_runtime")
    def test_jdownloader_user_service_stopped_is_degraded(
        self,
        runtime,
        _is_file,
        _exists,
        _is_dir,
        _is_socket,
        run,
        _geteuid,
    ):
        runtime.return_value = self.jdownloader_runtime()
        run.return_value = Mock(
            returncode=0,
            stdout=(
                "LoadState=loaded\n"
                "ActiveState=inactive\n"
                "SubState=dead\n"
                "Result=success\n"
                "ExecMainCode=1\n"
                "ExecMainStatus=0\n"
            ),
            stderr="",
        )

        check = health.inspect_jdownloader_user_service()

        self.assertEqual(check.status, HealthStatus.DEGRADED)
        self.assertIn("manager is observable", check.message)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/4242")
        self.assertEqual(
            env["DBUS_SESSION_BUS_ADDRESS"],
            "unix:path=/run/user/4242/bus",
        )

    @patch.dict("os.environ", {}, clear=True)
    @patch("backend.services.service_health_service.os.geteuid", return_value=4242)
    @patch("backend.services.service_health_service.subprocess.run")
    @patch("backend.services.service_health_service.Path.is_socket", return_value=True)
    @patch("backend.services.service_health_service.Path.is_dir", return_value=True)
    @patch("backend.services.service_health_service.Path.exists", return_value=True)
    @patch("backend.services.service_health_service.Path.is_file", return_value=True)
    @patch("backend.services.service_health_service._jdownloader_user_runtime")
    def test_jdownloader_user_service_failed_is_unhealthy(
        self,
        runtime,
        _is_file,
        _exists,
        _is_dir,
        _is_socket,
        run,
        _geteuid,
    ):
        runtime.return_value = self.jdownloader_runtime()
        run.return_value = Mock(
            returncode=0,
            stdout=(
                "LoadState=loaded\n"
                "ActiveState=failed\n"
                "SubState=failed\n"
                "Result=exit-code\n"
                "ExecMainCode=1\n"
                "ExecMainStatus=2\n"
            ),
            stderr="",
        )

        check = health.inspect_jdownloader_user_service()

        self.assertEqual(check.status, HealthStatus.UNHEALTHY)

    @patch.dict("os.environ", {}, clear=True)
    @patch("backend.services.service_health_service.os.geteuid", return_value=4242)
    @patch("backend.services.service_health_service.subprocess.run")
    @patch("backend.services.service_health_service.Path.is_dir", return_value=False)
    @patch("backend.services.service_health_service.Path.exists", return_value=True)
    @patch("backend.services.service_health_service.Path.is_file", return_value=True)
    @patch("backend.services.service_health_service._jdownloader_user_runtime")
    def test_jdownloader_missing_user_runtime_is_degraded(
        self,
        runtime,
        _is_file,
        _exists,
        _is_dir,
        run,
        _geteuid,
    ):
        runtime.return_value = self.jdownloader_runtime()

        check = health.inspect_jdownloader_user_service()

        self.assertEqual(check.status, HealthStatus.DEGRADED)
        self.assertIn("graphical session is started", check.message)
        self.assertEqual(check.observed, "runtime_dir_unavailable")
        run.assert_not_called()

    @patch.dict("os.environ", {}, clear=True)
    @patch("backend.services.service_health_service.os.geteuid", return_value=4242)
    @patch("backend.services.service_health_service.subprocess.run")
    @patch("backend.services.service_health_service.Path.is_socket", return_value=False)
    @patch("backend.services.service_health_service.Path.is_dir", return_value=True)
    @patch("backend.services.service_health_service.Path.exists", return_value=True)
    @patch("backend.services.service_health_service.Path.is_file", return_value=True)
    @patch("backend.services.service_health_service._jdownloader_user_runtime")
    def test_jdownloader_missing_user_bus_is_degraded(
        self,
        runtime,
        _is_file,
        _exists,
        _is_dir,
        _is_socket,
        run,
        _geteuid,
    ):
        runtime.return_value = self.jdownloader_runtime()

        check = health.inspect_jdownloader_user_service()

        self.assertEqual(check.status, HealthStatus.DEGRADED)
        self.assertIn("graphical session is started", check.message)
        self.assertEqual(check.observed, "user_bus_unavailable")
        run.assert_not_called()

    @patch.dict("os.environ", {}, clear=True)
    @patch("backend.services.service_health_service.os.geteuid", return_value=4242)
    @patch("backend.services.service_health_service.subprocess.run")
    @patch("backend.services.service_health_service.Path.is_socket", return_value=True)
    @patch("backend.services.service_health_service.Path.is_dir", return_value=True)
    @patch("backend.services.service_health_service.Path.exists", return_value=True)
    @patch("backend.services.service_health_service.Path.is_file", return_value=True)
    @patch("backend.services.service_health_service._jdownloader_user_runtime")
    def test_jdownloader_user_manager_query_failure_is_unknown(
        self,
        runtime,
        _is_file,
        _exists,
        _is_dir,
        _is_socket,
        run,
        _geteuid,
    ):
        runtime.return_value = self.jdownloader_runtime()
        run.return_value = Mock(returncode=1, stdout="", stderr="permission denied")

        check = health.inspect_jdownloader_user_service()

        self.assertEqual(check.status, HealthStatus.UNKNOWN)

    @patch.dict("os.environ", {}, clear=True)
    @patch("backend.services.service_health_service.os.geteuid", return_value=4242)
    @patch("backend.services.service_health_service.subprocess.run")
    @patch("backend.services.service_health_service.Path.is_socket", return_value=True)
    @patch("backend.services.service_health_service.Path.is_dir", return_value=True)
    @patch("backend.services.service_health_service.Path.exists", return_value=True)
    @patch("backend.services.service_health_service.Path.is_file", return_value=True)
    @patch("backend.services.service_health_service._jdownloader_user_runtime")
    def test_jdownloader_user_manager_connection_failure_is_degraded(
        self,
        runtime,
        _is_file,
        _exists,
        _is_dir,
        _is_socket,
        run,
        _geteuid,
    ):
        runtime.return_value = self.jdownloader_runtime()
        run.return_value = Mock(
            returncode=1,
            stdout="",
            stderr="Failed to connect to bus: Connection refused",
        )

        check = health.inspect_jdownloader_user_service()

        self.assertEqual(check.status, HealthStatus.DEGRADED)
        self.assertEqual(check.observed, "user_manager_unavailable")


class ServiceHealthDeploymentBaselineParityTests(unittest.TestCase):
    BASE = "a" * 40
    OTHER = "b" * 40

    def observation_payload(
        self,
        *,
        observed_at: datetime | None = None,
        qa_runtime: str | None = None,
        qa_status: str = "ok",
        qa_branch: str = "qa-runtime",
    ) -> dict:
        commits = {
            "canonical": self.BASE,
            "planning": self.BASE,
            "implementation-main": self.BASE,
            "qa-source": self.BASE,
            "qa-runtime": qa_runtime or self.BASE,
            "production-source": self.BASE,
            "github-main": self.BASE,
        }
        rows = []
        for id, name, ref, branch in health.DEPLOYMENT_BASELINE_OBSERVATION_ROWS:
            status = "ok"
            observed_branch = branch
            if id == "qa-runtime":
                status = qa_status
                observed_branch = qa_branch
            rows.append(
                {
                    "id": id,
                    "name": name,
                    "ref": ref,
                    "expected_branch": branch,
                    "observed_branch": observed_branch,
                    "status": status,
                    "reason": "branch_mismatch" if status == "branch_mismatch" else None,
                    "commit": commits[id],
                }
            )
        return {
            "schema_version": 1,
            "repository": "backend",
            "observed_at": (observed_at or datetime.now(timezone.utc)).isoformat(),
            "observations": rows,
        }

    def write_observation(self, payload: dict) -> health.Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = health.Path(directory.name) / "deployment-baseline.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    @patch("backend.services.service_health_service._backend_deployment_is_active", return_value=False)
    def test_all_backend_baseline_surfaces_equal_is_healthy(
        self,
        _active,
    ):
        path = self.write_observation(self.observation_payload())

        with patch.object(health, "DEPLOYMENT_BASELINE_OBSERVATION_PATH", path):
            component = health.evaluate_deployment_baseline_parity_component(
                datetime.now(timezone.utc)
            )

        self.assertEqual(component.status, HealthStatus.HEALTHY)
        self.assertEqual(component.message, "Backend deployment baseline identities are converged.")
        self.assertEqual(len(component.dependencies), 7)
        self.assertEqual(component.dependencies[0].name, "Canonical / Production Runtime")
        self.assertTrue(
            all(dependency.expected == self.BASE for dependency in component.dependencies)
        )

    @patch("backend.services.service_health_service._backend_deployment_is_active", return_value=False)
    def test_one_settled_baseline_differs_is_degraded(
        self,
        _active,
    ):
        path = self.write_observation(self.observation_payload(qa_runtime=self.OTHER))

        with patch.object(health, "DEPLOYMENT_BASELINE_OBSERVATION_PATH", path):
            component = health.evaluate_deployment_baseline_parity_component(
                datetime.now(timezone.utc)
            )

        self.assertEqual(component.status, HealthStatus.DEGRADED)
        self.assertEqual(
            component.message,
            "QA runtime does not match the settled backend baseline.",
        )
        qa_runtime = next(
            dependency
            for dependency in component.dependencies
            if dependency.id == "deployment-baseline-qa-runtime"
        )
        self.assertEqual(qa_runtime.status, HealthStatus.DEGRADED)
        self.assertEqual(qa_runtime.expected, self.BASE)
        self.assertEqual(qa_runtime.observed, self.OTHER)

    def test_missing_sanitized_observation_is_unknown(
        self,
    ):
        missing = health.Path(tempfile.mkdtemp()) / "missing.json"
        self.addCleanup(lambda: missing.parent.rmdir())

        with patch.object(health, "DEPLOYMENT_BASELINE_OBSERVATION_PATH", missing):
            component = health.evaluate_deployment_baseline_parity_component(
                datetime.now(timezone.utc)
            )

        self.assertEqual(component.status, HealthStatus.UNKNOWN)
        self.assertEqual(component.message, "Protected baseline observation is unavailable.")

    @patch("backend.services.service_health_service._backend_deployment_is_active", return_value=False)
    def test_github_behind_settled_local_baseline_is_degraded(
        self,
        _active,
    ):
        payload = self.observation_payload()
        for row in payload["observations"]:
            if row["id"] == "github-main":
                row["commit"] = self.OTHER
        path = self.write_observation(payload)

        with patch.object(health, "DEPLOYMENT_BASELINE_OBSERVATION_PATH", path):
            component = health.evaluate_deployment_baseline_parity_component(
                datetime.now(timezone.utc)
            )

        self.assertEqual(component.status, HealthStatus.DEGRADED)
        self.assertEqual(
            component.message,
            "GitHub main does not match the deployed backend baseline.",
        )

    @patch("backend.services.service_health_service._backend_deployment_is_active", return_value=True)
    def test_legitimate_staged_transition_reports_in_progress_not_persistent_failure(
        self,
        _active,
    ):
        path = self.write_observation(self.observation_payload(qa_runtime=self.OTHER))

        with patch.object(health, "DEPLOYMENT_BASELINE_OBSERVATION_PATH", path):
            component = health.evaluate_deployment_baseline_parity_component(
                datetime.now(timezone.utc)
            )

        self.assertEqual(component.status, HealthStatus.DEGRADED)
        self.assertEqual(
            component.message,
            "Backend deployment is in progress; baseline convergence is still settling.",
        )

    @patch("backend.services.service_health_service._backend_deployment_is_active", return_value=False)
    def test_settled_state_after_deployment_requires_convergence(
        self,
        _active,
    ):
        path = self.write_observation(self.observation_payload(qa_runtime=self.OTHER))

        with patch.object(health, "DEPLOYMENT_BASELINE_OBSERVATION_PATH", path):
            component = health.evaluate_deployment_baseline_parity_component(
                datetime.now(timezone.utc)
            )

        self.assertEqual(component.status, HealthStatus.DEGRADED)
        self.assertNotIn("in progress", component.message)

    def test_stale_sanitized_observation_is_unknown(self):
        stale_at = datetime.now(timezone.utc) - timedelta(
            seconds=health.DEPLOYMENT_BASELINE_OBSERVATION_STALE_AFTER_SECONDS + 1
        )
        path = self.write_observation(self.observation_payload(observed_at=stale_at))

        with patch.object(health, "DEPLOYMENT_BASELINE_OBSERVATION_PATH", path):
            component = health.evaluate_deployment_baseline_parity_component(
                datetime.now(timezone.utc)
            )

        self.assertEqual(component.status, HealthStatus.UNKNOWN)
        self.assertEqual(component.message, "Protected baseline observation is stale.")

    def test_qa_runtime_branch_mismatch_is_not_inspection_failure(self):
        path = self.write_observation(
            self.observation_payload(
                qa_status="branch_mismatch",
                qa_branch="main",
            )
        )

        with patch.object(health, "DEPLOYMENT_BASELINE_OBSERVATION_PATH", path):
            component = health.evaluate_deployment_baseline_parity_component(
                datetime.now(timezone.utc)
            )

        self.assertEqual(component.status, HealthStatus.DEGRADED)
        qa_runtime = next(
            dependency
            for dependency in component.dependencies
            if dependency.id == "deployment-baseline-qa-runtime"
        )
        self.assertEqual(qa_runtime.status, HealthStatus.DEGRADED)
        self.assertIn("expected qa-runtime", qa_runtime.message)

    def test_qa_runtime_unavailable_is_unknown_not_branch_mismatch(self):
        payload = self.observation_payload(qa_status="unknown")
        for row in payload["observations"]:
            if row["id"] == "qa-runtime":
                row["commit"] = None
                row["reason"] = "runtime_boundary_unavailable"
        path = self.write_observation(payload)

        with patch.object(health, "DEPLOYMENT_BASELINE_OBSERVATION_PATH", path):
            component = health.evaluate_deployment_baseline_parity_component(
                datetime.now(timezone.utc)
            )

        self.assertEqual(component.status, HealthStatus.UNKNOWN)
        qa_runtime = next(
            dependency
            for dependency in component.dependencies
            if dependency.id == "deployment-baseline-qa-runtime"
        )
        self.assertEqual(qa_runtime.message, "runtime_boundary_unavailable")


class ServiceHealthAggregationAndSafetyTests(unittest.TestCase):
    def component(self, status: HealthStatus, required: bool = True) -> HealthComponent:
        return HealthComponent(
            id=f"component-{status.value}",
            name="Component",
            group=HealthComponentGroup.CORE,
            kind=HealthComponentKind.SYSTEMD_UNIT,
            status=status,
            message="checked",
            required=required,
            checked_at=datetime.now(timezone.utc),
        )

    def test_healthy_and_idle_aggregate_healthy(self):
        overall = health.aggregate_overall(
            [
                self.component(HealthStatus.HEALTHY),
                self.component(HealthStatus.IDLE),
            ]
        )

        self.assertEqual(overall, HealthStatus.HEALTHY)

    def test_one_degraded_aggregates_degraded(self):
        overall = health.aggregate_overall(
            [
                self.component(HealthStatus.HEALTHY),
                self.component(HealthStatus.DEGRADED),
            ]
        )

        self.assertEqual(overall, HealthStatus.DEGRADED)

    def test_required_unknown_aggregates_degraded(self):
        overall = health.aggregate_overall(
            [
                self.component(HealthStatus.HEALTHY),
                self.component(HealthStatus.UNKNOWN),
            ]
        )

        self.assertEqual(overall, HealthStatus.DEGRADED)

    def test_any_unhealthy_aggregates_unhealthy(self):
        overall = health.aggregate_overall(
            [
                self.component(HealthStatus.DEGRADED),
                self.component(HealthStatus.UNHEALTHY),
            ]
        )

        self.assertEqual(overall, HealthStatus.UNHEALTHY)

    @patch("backend.services.service_health_service.evaluate_jdownloader_component")
    @patch("backend.services.service_health_service.evaluate_deployment_baseline_parity_component")
    @patch("backend.services.service_health_service.evaluate_path_component")
    @patch("backend.services.service_health_service.evaluate_unit_component")
    def test_one_probe_error_does_not_abort_snapshot(
        self,
        unit_component,
        path_component,
        parity_component,
        jdownloader_component,
    ):
        checked_at = datetime.now(timezone.utc)
        unit_component.side_effect = [
            RuntimeError("timeout"),
            self.component(HealthStatus.HEALTHY),
        ] + [self.component(HealthStatus.IDLE)] * 30
        path_component.return_value = self.component(HealthStatus.HEALTHY)
        parity_component.return_value = self.component(HealthStatus.HEALTHY)
        jdownloader_component.return_value = self.component(HealthStatus.DEGRADED)

        snapshot = health.collect_service_health_snapshot()

        self.assertTrue(snapshot.success)
        self.assertGreater(len(snapshot.components), 2)
        self.assertEqual(snapshot.components[0].status, HealthStatus.UNKNOWN)
        self.assertEqual(snapshot.overall, HealthStatus.DEGRADED)
        self.assertIsInstance(checked_at, datetime)

    @patch("backend.services.service_health_service.subprocess.run")
    def test_systemd_inspection_uses_fixed_read_only_show_command(self, run):
        run.return_value = Mock(
            returncode=0,
            stdout="LoadState=loaded\nActiveState=active\nSubState=running\n",
            stderr="",
        )

        status = health.inspect_systemd_unit("remihub.service")

        self.assertTrue(status.available)
        command = run.call_args.args[0]
        self.assertEqual(command[0], health.SYSTEMCTL)
        self.assertEqual(command[1], "show")
        self.assertIn("remihub.service", command)
        self.assertIn("--no-pager", command)
        self.assertNotIn("start", command)
        self.assertFalse(run.call_args.kwargs["check"])

    def test_arbitrary_systemd_unit_is_rejected(self):
        with self.assertRaises(ValueError):
            health.inspect_systemd_unit("ssh.service")

    @patch("backend.services.service_health_service.subprocess.run")
    def test_systemd_timeout_is_reported_unknown_without_stderr(self, run):
        run.side_effect = subprocess.TimeoutExpired(cmd=["systemctl"], timeout=3)

        status = health.inspect_systemd_unit("remihub.service")

        self.assertFalse(status.available)
        self.assertEqual(status.error, "systemd status unavailable: TimeoutExpired")


class ServiceHealthPersistenceTests(unittest.TestCase):
    def component(self, status: HealthStatus, checked_at: datetime | None = None) -> HealthComponent:
        return HealthComponent(
            id=f"component-{status.value}",
            name="Component",
            group=HealthComponentGroup.CORE,
            kind=HealthComponentKind.SYSTEMD_UNIT,
            status=status,
            message="checked",
            required=True,
            checked_at=checked_at or datetime.now(timezone.utc),
        )

    def snapshot(
        self,
        *,
        checked_at: datetime | None = None,
        overall: HealthStatus = HealthStatus.HEALTHY,
    ) -> ServiceHealthSnapshotResponse:
        timestamp = checked_at or datetime.now(timezone.utc)
        return ServiceHealthSnapshotResponse(
            success=True,
            checked_at=timestamp,
            overall=overall,
            components=[self.component(HealthStatus.HEALTHY, timestamp)],
        )

    @patch("backend.services.service_health_service.put_db_conn")
    @patch("backend.services.service_health_service.get_db_conn")
    def test_persist_snapshot_upserts_singleton_current_record(self, get_db_conn, put_db_conn):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1
        get_db_conn.return_value = conn

        health.persist_service_health_snapshot(self.snapshot())

        sql = cur.execute.call_args.args[0]
        self.assertIn("INSERT INTO public.service_health_current_snapshot", sql)
        self.assertIn("ON CONFLICT (singleton_id) DO UPDATE", sql)
        self.assertNotIn("INSERT INTO public.service_health_history", sql)
        self.assertEqual(cur.execute.call_args.args[1][0], health.SNAPSHOT_SINGLETON_ID)
        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()
        put_db_conn.assert_called_once_with(conn)

    @patch("backend.services.service_health_service.put_db_conn")
    @patch("backend.services.service_health_service.get_db_conn")
    def test_second_persist_replaces_current_record_without_append_sql(self, get_db_conn, _put_db_conn):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1
        get_db_conn.return_value = conn
        first = self.snapshot(checked_at=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))
        second = self.snapshot(checked_at=datetime(2026, 8, 10, 12, 1, tzinfo=timezone.utc))

        health.persist_service_health_snapshot(first)
        health.persist_service_health_snapshot(second)

        self.assertEqual(cur.execute.call_count, 2)
        second_params = cur.execute.call_args.args[1]
        self.assertEqual(second_params[1], second.checked_at)
        self.assertEqual(conn.commit.call_count, 2)

    @patch("backend.services.service_health_service.put_db_conn")
    @patch("backend.services.service_health_service.get_db_conn")
    def test_db_failure_rolls_back_and_leaves_prior_snapshot_untouched(self, get_db_conn, _put_db_conn):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.execute.side_effect = RuntimeError("db failed")
        get_db_conn.return_value = conn

        with self.assertRaises(RuntimeError):
            health.persist_service_health_snapshot(self.snapshot())

        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_malformed_snapshot_is_rejected_before_db_access(self):
        with patch("backend.services.service_health_service.get_db_conn") as get_db_conn:
            with self.assertRaises(ValidationError):
                health.persist_service_health_snapshot({"checked_at": "bad"})

        get_db_conn.assert_not_called()

    @patch("backend.services.service_health_service.put_db_conn")
    @patch("backend.services.service_health_service.get_db_conn")
    def test_load_current_snapshot_reconstructs_typed_response(self, get_db_conn, put_db_conn):
        stored = self.snapshot()
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (stored.model_dump(mode="json"),)
        get_db_conn.return_value = conn

        loaded = health.load_current_service_health_snapshot()

        self.assertEqual(loaded, stored)
        put_db_conn.assert_called_once_with(conn)

    @patch("backend.services.service_health_service.put_db_conn")
    @patch("backend.services.service_health_service.get_db_conn")
    def test_load_current_snapshot_accepts_json_string_payload(self, get_db_conn, _put_db_conn):
        stored = self.snapshot()
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (json.dumps(stored.model_dump(mode="json")),)
        get_db_conn.return_value = conn

        loaded = health.load_current_service_health_snapshot()

        self.assertEqual(loaded, stored)

    @patch("backend.services.service_health_service.load_current_service_health_snapshot")
    @patch("backend.services.service_health_service.inspect_jdownloader_process")
    @patch("backend.services.service_health_service.inspect_jdownloader_user_service")
    @patch("backend.services.service_health_service.evaluate_path_component")
    @patch("backend.services.service_health_service.inspect_systemd_unit")
    def test_api_read_path_uses_persisted_snapshot_without_live_probes(
        self,
        inspect_systemd_unit,
        evaluate_path_component,
        user_service,
        process,
        load_snapshot,
    ):
        stored = self.snapshot()
        load_snapshot.return_value = stored

        response = health.get_service_health_snapshot()

        self.assertEqual(response.checked_at, stored.checked_at)
        self.assertIn(
            health.SNAPSHOT_FRESHNESS_COMPONENT_ID,
            {component.id for component in response.components},
        )
        inspect_systemd_unit.assert_not_called()
        evaluate_path_component.assert_not_called()
        user_service.assert_not_called()
        process.assert_not_called()

    @patch("backend.services.service_health_service.load_current_service_health_snapshot")
    def test_missing_snapshot_returns_deterministic_degraded_response(self, load_snapshot):
        load_snapshot.return_value = None

        response = health.get_service_health_snapshot()

        self.assertEqual(response.overall, HealthStatus.DEGRADED)
        self.assertEqual(len(response.components), 1)
        self.assertEqual(response.components[0].id, health.SNAPSHOT_FRESHNESS_COMPONENT_ID)
        self.assertEqual(response.components[0].status, HealthStatus.DEGRADED)

    @patch("backend.services.service_health_service.load_current_service_health_snapshot")
    def test_stale_snapshot_cannot_appear_healthy(self, load_snapshot):
        stale_time = datetime.now(timezone.utc) - timedelta(
            seconds=health.SNAPSHOT_STALE_AFTER_SECONDS + 30
        )
        load_snapshot.return_value = self.snapshot(checked_at=stale_time)

        response = health.get_service_health_snapshot()

        freshness = next(
            component
            for component in response.components
            if component.id == health.SNAPSHOT_FRESHNESS_COMPONENT_ID
        )
        self.assertEqual(freshness.status, HealthStatus.DEGRADED)
        self.assertEqual(response.overall, HealthStatus.DEGRADED)

    @patch("backend.services.service_health_service.load_current_service_health_snapshot")
    def test_fresh_snapshot_returns_stored_state_with_healthy_freshness(self, load_snapshot):
        load_snapshot.return_value = self.snapshot()

        response = health.get_service_health_snapshot()

        freshness = next(
            component
            for component in response.components
            if component.id == health.SNAPSHOT_FRESHNESS_COMPONENT_ID
        )
        self.assertEqual(freshness.status, HealthStatus.HEALTHY)
        self.assertEqual(response.overall, HealthStatus.HEALTHY)


if __name__ == "__main__":
    unittest.main()

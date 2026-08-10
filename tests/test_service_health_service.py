import subprocess
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from backend.models.health_models import HealthComponent, HealthComponentGroup, HealthComponentKind, HealthStatus
from backend.services import service_health_service as health


def unit_status(
    *,
    unit: str = "remihub.service",
    available: bool = True,
    load_state: str = "loaded",
    active_state: str = "active",
    sub_state: str = "running",
    result: str = "success",
    exec_main_code: str = "exited",
    exec_main_status: str = "0",
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
        },
    )


class ServiceHealthSystemdSemanticsTests(unittest.TestCase):
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
            unit_status(unit="remihub-agent-backend-deployment.timer", sub_state="waiting"),
            health.ExpectedMode.ARMED_TIMER_OR_PATH,
        )

        self.assertEqual(status, HealthStatus.HEALTHY)

    def test_timer_or_path_inactive_is_unhealthy(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(active_state="inactive", sub_state="dead"),
            health.ExpectedMode.ARMED_TIMER_OR_PATH,
        )

        self.assertEqual(status, HealthStatus.UNHEALTHY)

    def test_on_demand_inactive_success_is_idle(self):
        status, _message = health.evaluate_systemd_status(
            unit_status(active_state="inactive", sub_state="dead"),
            health.ExpectedMode.ON_DEMAND,
        )

        self.assertEqual(status, HealthStatus.IDLE)

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

    @patch.dict(
        "os.environ",
        {
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        },
        clear=True,
    )
    @patch("backend.services.service_health_service.subprocess.run")
    @patch("backend.services.service_health_service.Path.exists", return_value=True)
    @patch("backend.services.service_health_service.Path.is_file", return_value=True)
    def test_jdownloader_user_service_running_is_healthy(
        self,
        _is_file,
        _exists,
        run,
    ):
        run.return_value = Mock(
            returncode=0,
            stdout="LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n",
            stderr="",
        )

        check = health.inspect_jdownloader_user_service()

        self.assertEqual(check.status, HealthStatus.HEALTHY)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], [health.SYSTEMCTL, "--user", "show"])
        self.assertIn(health.JDOWNLOADER_SYSTEMD_UNIT_NAME, command)
        self.assertNotIn("start", command)
        self.assertNotIn("restart", command)

    @patch.dict(
        "os.environ",
        {
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        },
        clear=True,
    )
    @patch("backend.services.service_health_service.subprocess.run")
    @patch("backend.services.service_health_service.Path.exists", return_value=True)
    @patch("backend.services.service_health_service.Path.is_file", return_value=True)
    def test_jdownloader_user_service_stopped_is_degraded(
        self,
        _is_file,
        _exists,
        run,
    ):
        run.return_value = Mock(
            returncode=0,
            stdout="LoadState=loaded\nActiveState=inactive\nSubState=dead\nResult=success\n",
            stderr="",
        )

        check = health.inspect_jdownloader_user_service()

        self.assertEqual(check.status, HealthStatus.DEGRADED)

    @patch.dict("os.environ", {}, clear=True)
    @patch("backend.services.service_health_service.subprocess.run")
    @patch("backend.services.service_health_service.Path.exists", return_value=True)
    @patch("backend.services.service_health_service.Path.is_file", return_value=True)
    def test_jdownloader_user_manager_without_runtime_env_is_unknown(
        self,
        _is_file,
        _exists,
        run,
    ):
        check = health.inspect_jdownloader_user_service()

        self.assertEqual(check.status, HealthStatus.UNKNOWN)
        run.assert_not_called()


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
    @patch("backend.services.service_health_service.evaluate_path_component")
    @patch("backend.services.service_health_service.evaluate_unit_component")
    def test_one_probe_error_does_not_abort_snapshot(
        self,
        unit_component,
        path_component,
        jdownloader_component,
    ):
        checked_at = datetime.now(timezone.utc)
        unit_component.side_effect = [
            RuntimeError("timeout"),
            self.component(HealthStatus.HEALTHY),
        ] + [self.component(HealthStatus.IDLE)] * 30
        path_component.return_value = self.component(HealthStatus.HEALTHY)
        jdownloader_component.return_value = self.component(HealthStatus.DEGRADED)

        snapshot = health.get_service_health_snapshot()

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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import os
import subprocess

from backend.models.health_models import (
    HealthComponent,
    HealthComponentGroup,
    HealthComponentKind,
    HealthDependencyCheck,
    HealthStatus,
    HealthSystemdMetadata,
    ServiceHealthSnapshotResponse,
)


SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_TIMEOUT_SECONDS = 3
SYSTEMD_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "Type",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "ExecMainPID",
    "MainPID",
    "NRestarts",
    "ActiveEnterTimestamp",
    "InactiveEnterTimestamp",
    "StateChangeTimestamp",
)


class ExpectedMode:
    PERSISTENT_RUNNING = "persistent_running"
    ARMED_TIMER_OR_PATH = "armed_timer_or_path"
    ON_DEMAND = "on_demand"
    QA_RUNTIME = "qa_runtime"
    ONESHOT_SUCCESS_EXITED = "oneshot_success_exited"
    POSTGRESQL = "postgresql"


@dataclass(frozen=True)
class UnitDefinition:
    id: str
    name: str
    group: HealthComponentGroup
    unit: str
    expected_mode: str
    required: bool = True


@dataclass(frozen=True)
class PathDefinition:
    id: str
    name: str
    group: HealthComponentGroup
    path: str
    kind: HealthComponentKind
    expected_type: str = "directory"
    required: bool = True


UNIT_DEFINITIONS = (
    UnitDefinition(
        "remihub",
        "RemiHub Backend",
        HealthComponentGroup.CORE,
        "remihub.service",
        ExpectedMode.PERSISTENT_RUNNING,
    ),
    UnitDefinition(
        "caddy",
        "Caddy",
        HealthComponentGroup.CORE,
        "caddy.service",
        ExpectedMode.PERSISTENT_RUNNING,
    ),
    UnitDefinition(
        "postgresql",
        "PostgreSQL",
        HealthComponentGroup.CORE,
        "postgresql.service",
        ExpectedMode.POSTGRESQL,
    ),
    UnitDefinition(
        "remihub-agent-worker",
        "Agent Worker",
        HealthComponentGroup.AGENT,
        "remihub-agent-worker.service",
        ExpectedMode.PERSISTENT_RUNNING,
    ),
    UnitDefinition(
        "remihub-agent-implementation",
        "Agent Implementation Worker",
        HealthComponentGroup.AGENT,
        "remihub-agent-implementation.service",
        ExpectedMode.PERSISTENT_RUNNING,
    ),
    UnitDefinition(
        "remihub-agent-android-implementation",
        "Agent Android Implementation Worker",
        HealthComponentGroup.AGENT,
        "remihub-agent-android-implementation.service",
        ExpectedMode.PERSISTENT_RUNNING,
    ),
    UnitDefinition(
        "remihub-agent-android-deployment-timer",
        "Android Deployment Timer",
        HealthComponentGroup.AGENT,
        "remihub-agent-android-deployment.timer",
        ExpectedMode.ARMED_TIMER_OR_PATH,
    ),
    UnitDefinition(
        "remihub-agent-android-deployment-trigger-path",
        "Android Deployment Trigger Path",
        HealthComponentGroup.AGENT,
        "remihub-agent-android-deployment-trigger.path",
        ExpectedMode.ARMED_TIMER_OR_PATH,
    ),
    UnitDefinition(
        "remihub-agent-backend-deployment-timer",
        "Backend Deployment Timer",
        HealthComponentGroup.AGENT,
        "remihub-agent-backend-deployment.timer",
        ExpectedMode.ARMED_TIMER_OR_PATH,
    ),
    UnitDefinition(
        "remihub-agent-backend-deployment-trigger-path",
        "Backend Deployment Trigger Path",
        HealthComponentGroup.AGENT,
        "remihub-agent-backend-deployment-trigger.path",
        ExpectedMode.ARMED_TIMER_OR_PATH,
    ),
    UnitDefinition(
        "remihub-agent-android-deployment",
        "Android Deployment Worker",
        HealthComponentGroup.AGENT,
        "remihub-agent-android-deployment.service",
        ExpectedMode.ON_DEMAND,
    ),
    UnitDefinition(
        "remihub-agent-android-deployment-trigger",
        "Android Deployment Trigger",
        HealthComponentGroup.AGENT,
        "remihub-agent-android-deployment-trigger.service",
        ExpectedMode.ON_DEMAND,
    ),
    UnitDefinition(
        "remihub-agent-backend-deployment-trigger",
        "Backend Deployment Trigger",
        HealthComponentGroup.AGENT,
        "remihub-agent-backend-deployment-trigger.service",
        ExpectedMode.ON_DEMAND,
    ),
    UnitDefinition(
        "remihub-agent-backend-github-sync",
        "Backend GitHub Sync",
        HealthComponentGroup.AGENT,
        "remihub-agent-backend-github-sync.service",
        ExpectedMode.ON_DEMAND,
    ),
    UnitDefinition(
        "remihub-agent-deployment-production",
        "Backend Production Deployment",
        HealthComponentGroup.AGENT,
        "remihub-agent-deployment-production.service",
        ExpectedMode.ON_DEMAND,
    ),
    UnitDefinition(
        "remihub-agent-deployment-qa",
        "Backend QA Deployment",
        HealthComponentGroup.AGENT,
        "remihub-agent-deployment-qa.service",
        ExpectedMode.ON_DEMAND,
    ),
    UnitDefinition(
        "remihub-agent-implementation-qa",
        "Agent QA Implementation",
        HealthComponentGroup.AGENT,
        "remihub-agent-implementation-qa.service",
        ExpectedMode.ON_DEMAND,
    ),
    UnitDefinition(
        "remihub-agent-planning-sync",
        "Agent Planning Sync",
        HealthComponentGroup.AGENT,
        "remihub-agent-planning-sync.service",
        ExpectedMode.ON_DEMAND,
    ),
    UnitDefinition(
        "remihub-backend-qa",
        "Backend QA Runtime",
        HealthComponentGroup.AGENT,
        "remihub-backend-qa.service",
        ExpectedMode.QA_RUNTIME,
    ),
    UnitDefinition(
        "remihub-bindmount",
        "RemiHub Bind Mount",
        HealthComponentGroup.STORAGE,
        "remihub-bindmount.service",
        ExpectedMode.ONESHOT_SUCCESS_EXITED,
    ),
    UnitDefinition(
        "rh-storage",
        "RH-Storage",
        HealthComponentGroup.RH_STORAGE,
        "rh-storage.service",
        ExpectedMode.PERSISTENT_RUNNING,
    ),
    UnitDefinition(
        "mergerfs-pool1",
        "mergerfs Pool 1",
        HealthComponentGroup.STORAGE,
        "mergerfs-pool1.service",
        ExpectedMode.PERSISTENT_RUNNING,
    ),
    UnitDefinition(
        "mergerfs-pool2",
        "mergerfs Pool 2",
        HealthComponentGroup.STORAGE,
        "mergerfs-pool2.service",
        ExpectedMode.PERSISTENT_RUNNING,
    ),
    UnitDefinition(
        "plexmediaserver",
        "Plex Media Server",
        HealthComponentGroup.MEDIA,
        "plexmediaserver.service",
        ExpectedMode.PERSISTENT_RUNNING,
    ),
)

ALLOWED_SYSTEMD_UNITS = frozenset(unit.unit for unit in UNIT_DEFINITIONS)

PATH_DEFINITIONS = (
    PathDefinition(
        "plex-pool-path",
        "Plex Pool Mount",
        HealthComponentGroup.STORAGE,
        "/mnt/plex-pool",
        HealthComponentKind.MOUNT,
    ),
    PathDefinition(
        "secure-pool-path",
        "Secure Pool Mount",
        HealthComponentGroup.STORAGE,
        "/mnt/secure-pool",
        HealthComponentKind.MOUNT,
    ),
    PathDefinition(
        "remihub-source-path",
        "RemiHub Source Path",
        HealthComponentGroup.STORAGE,
        "/mnt/secure-pool/Q_Drive/Projects/RemiHub",
        HealthComponentKind.PATH,
    ),
    PathDefinition(
        "remihub-runtime-path",
        "RemiHub Runtime Path",
        HealthComponentGroup.STORAGE,
        "/opt/remihub",
        HealthComponentKind.PATH,
    ),
    PathDefinition(
        "download-temp-path",
        "Download Temp Path",
        HealthComponentGroup.MEDIA,
        "/srv/remihub/Temp",
        HealthComponentKind.PATH,
    ),
    PathDefinition(
        "jdownloader-watch-path",
        "JDownloader FolderWatch",
        HealthComponentGroup.MEDIA,
        "/srv/remihub/Temp/JDownloaderWatch",
        HealthComponentKind.PATH,
    ),
)

JDOWNLOADER_PATHS = (
    PathDefinition(
        "jdownloader-install-path",
        "JDownloader Install Directory",
        HealthComponentGroup.MEDIA,
        "/opt/jdownloader",
        HealthComponentKind.PATH,
    ),
    PathDefinition(
        "jdownloader-jar-path",
        "JDownloader JAR",
        HealthComponentGroup.MEDIA,
        "/opt/jdownloader/JDownloader.jar",
        HealthComponentKind.PATH,
        expected_type="file",
    ),
    PathDefinition(
        "jdownloader-cfg-path",
        "JDownloader Config Directory",
        HealthComponentGroup.MEDIA,
        "/opt/jdownloader/cfg",
        HealthComponentKind.PATH,
    ),
    PathDefinition(
        "jdownloader-folderwatch-path",
        "JDownloader FolderWatch",
        HealthComponentGroup.MEDIA,
        "/srv/remihub/Temp/JDownloaderWatch",
        HealthComponentKind.PATH,
    ),
)

JDOWNLOADER_USER_UNIT = "/home/alex/.config/systemd/user/jdownloader.service"
JDOWNLOADER_SYSTEMD_UNIT_NAME = "jdownloader.service"
JDOWNLOADER_USER_UNIT_LINK = (
    "/home/alex/.config/systemd/user/graphical-session.target.wants/"
    "jdownloader.service"
)


@dataclass(frozen=True)
class SystemdUnitStatus:
    unit: str
    available: bool
    properties: dict[str, str]
    error: str | None = None

    def get(self, key: str) -> str | None:
        return self.properties.get(key)


def _positive_int(value: str | None) -> int | None:
    if value and value.isdigit():
        parsed = int(value)
        if parsed > 0:
            return parsed
    return None


def _is_success_result(value: str | None) -> bool:
    return value in {None, "", "success"}


def _is_success_exit(status: SystemdUnitStatus) -> bool:
    code = status.get("ExecMainCode")
    exit_status = status.get("ExecMainStatus")
    if code in {None, "", "0"} and exit_status in {None, "", "0"}:
        return True
    return code == "exited" and exit_status in {None, "", "0"}


def inspect_systemd_unit(unit: str, *, timeout: float = SYSTEMD_TIMEOUT_SECONDS) -> SystemdUnitStatus:
    if unit not in ALLOWED_SYSTEMD_UNITS:
        raise ValueError(f"Systemd unit is not allowlisted: {unit}")

    command = [
        SYSTEMCTL,
        "show",
        unit,
        *(f"--property={prop}" for prop in SYSTEMD_PROPERTIES),
        "--no-pager",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        return SystemdUnitStatus(
            unit=unit,
            available=False,
            properties={},
            error=f"systemd status unavailable: {type(exc).__name__}",
        )

    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        properties[key] = value

    load_state = properties.get("LoadState")
    available = result.returncode == 0 and load_state not in {"", "not-found", "error"}
    return SystemdUnitStatus(
        unit=unit,
        available=available,
        properties=properties,
        error=None if available else "systemd status unavailable",
    )


def systemd_status_for_rh_storage_compat(service_name: str) -> dict[str, object]:
    status = inspect_systemd_unit(service_name)
    pid = _positive_int(status.get("ExecMainPID")) or _positive_int(status.get("MainPID"))
    active_state = status.get("ActiveState") or "unknown"
    return {
        "available": status.available,
        "service_name": service_name,
        "active": active_state == "active",
        "active_state": active_state,
        "sub_state": status.get("SubState") or "unknown",
        "load_state": status.get("LoadState") or "unknown",
        "unit_file_state": status.get("UnitFileState") or "unknown",
        "pid": pid,
        "active_since": status.get("ActiveEnterTimestamp") or None,
        "error": status.error,
    }


def _systemd_metadata(status: SystemdUnitStatus) -> HealthSystemdMetadata:
    return HealthSystemdMetadata(
        unit=status.unit,
        available=status.available,
        load_state=status.get("LoadState"),
        active_state=status.get("ActiveState"),
        sub_state=status.get("SubState"),
        unit_file_state=status.get("UnitFileState"),
        type=status.get("Type"),
        result=status.get("Result"),
        exec_main_code=status.get("ExecMainCode"),
        exec_main_status=status.get("ExecMainStatus"),
        main_pid=_positive_int(status.get("MainPID"))
        or _positive_int(status.get("ExecMainPID")),
        n_restarts=_positive_int(status.get("NRestarts")),
        active_enter_timestamp=status.get("ActiveEnterTimestamp") or None,
        inactive_enter_timestamp=status.get("InactiveEnterTimestamp") or None,
        state_change_timestamp=status.get("StateChangeTimestamp") or None,
    )


def evaluate_systemd_status(
    status: SystemdUnitStatus,
    expected_mode: str,
) -> tuple[HealthStatus, str]:
    if not status.available:
        return HealthStatus.UNKNOWN, "Systemd unit state is unavailable"

    load_state = status.get("LoadState")
    active_state = status.get("ActiveState")
    sub_state = status.get("SubState")
    result = status.get("Result")

    if load_state in {"not-found", "error"}:
        return HealthStatus.UNKNOWN, "Systemd unit is absent or unreadable"

    if active_state == "failed" or result not in {None, "", "success"}:
        return HealthStatus.UNHEALTHY, "Systemd unit is failed or last invocation failed"

    if expected_mode == ExpectedMode.PERSISTENT_RUNNING:
        if active_state == "active" and sub_state == "running":
            return HealthStatus.HEALTHY, "Persistent service is active and running"
        return HealthStatus.UNHEALTHY, "Persistent service is not running"

    if expected_mode == ExpectedMode.ARMED_TIMER_OR_PATH:
        if active_state == "active" and sub_state == "waiting":
            return HealthStatus.HEALTHY, "Timer or path watcher is armed"
        return HealthStatus.UNHEALTHY, "Timer or path watcher is not armed"

    if expected_mode in {ExpectedMode.ON_DEMAND, ExpectedMode.QA_RUNTIME}:
        if active_state == "active":
            return HealthStatus.HEALTHY, "On-demand unit is currently active"
        if not _is_success_exit(status):
            return HealthStatus.UNHEALTHY, "On-demand unit last exit status was not successful"
        if active_state == "inactive" and sub_state in {"dead", "exited"} and _is_success_result(result) and _is_success_exit(status):
            return HealthStatus.IDLE, "On-demand unit is idle with no failed result"
        return HealthStatus.UNKNOWN, "On-demand unit state is not recognized"

    if expected_mode == ExpectedMode.ONESHOT_SUCCESS_EXITED:
        if not _is_success_exit(status):
            return HealthStatus.UNHEALTHY, "Oneshot dependency last exit status was not successful"
        if active_state == "active" and sub_state == "exited" and _is_success_result(result) and _is_success_exit(status):
            return HealthStatus.HEALTHY, "Oneshot dependency completed successfully"
        if active_state == "inactive" and sub_state in {"dead", "exited"} and _is_success_result(result) and _is_success_exit(status):
            return HealthStatus.IDLE, "Oneshot dependency is inactive after successful completion"
        return HealthStatus.UNHEALTHY, "Oneshot dependency is not in a successful completed state"

    if expected_mode == ExpectedMode.POSTGRESQL:
        if active_state == "active" and sub_state in {"running", "exited"}:
            return HealthStatus.HEALTHY, "PostgreSQL service is active"
        return HealthStatus.UNHEALTHY, "PostgreSQL service is not active"

    return HealthStatus.UNKNOWN, "Unknown expected systemd mode"


def _combine_status(statuses: list[HealthStatus], *, unknown_degrades: bool = True) -> HealthStatus:
    if HealthStatus.UNHEALTHY in statuses:
        return HealthStatus.UNHEALTHY
    if HealthStatus.DEGRADED in statuses:
        return HealthStatus.DEGRADED
    if unknown_degrades and HealthStatus.UNKNOWN in statuses:
        return HealthStatus.DEGRADED
    if all(status == HealthStatus.IDLE for status in statuses):
        return HealthStatus.IDLE
    return HealthStatus.HEALTHY


def _path_check(definition: PathDefinition) -> HealthDependencyCheck:
    path = Path(definition.path)
    try:
        exists = path.exists()
        if not exists:
            return HealthDependencyCheck(
                id=definition.id,
                name=definition.name,
                kind=definition.kind,
                status=HealthStatus.UNHEALTHY,
                message="Required path is missing",
                path=definition.path,
                expected=definition.expected_type,
                observed="missing",
            )

        if definition.expected_type == "directory" and not path.is_dir():
            return HealthDependencyCheck(
                id=definition.id,
                name=definition.name,
                kind=definition.kind,
                status=HealthStatus.UNHEALTHY,
                message="Required path is not a directory",
                path=definition.path,
                expected="directory",
                observed="not_directory",
            )

        if definition.expected_type == "file" and not path.is_file():
            return HealthDependencyCheck(
                id=definition.id,
                name=definition.name,
                kind=definition.kind,
                status=HealthStatus.UNHEALTHY,
                message="Required path is not a file",
                path=definition.path,
                expected="file",
                observed="not_file",
            )

        if definition.kind == HealthComponentKind.MOUNT and not os.path.ismount(definition.path):
            return HealthDependencyCheck(
                id=definition.id,
                name=definition.name,
                kind=definition.kind,
                status=HealthStatus.UNHEALTHY,
                message="Required mount point is not mounted",
                path=definition.path,
                expected="mounted_directory",
                observed="directory_not_mounted",
            )

        return HealthDependencyCheck(
            id=definition.id,
            name=definition.name,
            kind=definition.kind,
            status=HealthStatus.HEALTHY,
            message="Required path is present",
            path=definition.path,
            expected=definition.expected_type,
            observed=definition.expected_type,
        )
    except Exception as exc:
        return HealthDependencyCheck(
            id=definition.id,
            name=definition.name,
            kind=definition.kind,
            status=HealthStatus.UNKNOWN,
            message=f"Path inspection unavailable: {type(exc).__name__}",
            path=definition.path,
            expected=definition.expected_type,
            observed="unknown",
        )


def _component(
    *,
    id: str,
    name: str,
    group: HealthComponentGroup,
    kind: HealthComponentKind,
    status: HealthStatus,
    message: str,
    checked_at: datetime,
    required: bool = True,
    unit: str | None = None,
    expected_mode: str | None = None,
    systemd: HealthSystemdMetadata | None = None,
    dependencies: list[HealthDependencyCheck] | None = None,
) -> HealthComponent:
    return HealthComponent(
        id=id,
        name=name,
        group=group,
        kind=kind,
        status=status,
        message=message,
        required=required,
        unit=unit,
        expected_mode=expected_mode,
        systemd=systemd,
        dependencies=dependencies or [],
        checked_at=checked_at,
    )


def evaluate_unit_component(definition: UnitDefinition, checked_at: datetime) -> HealthComponent:
    status = inspect_systemd_unit(definition.unit)
    normalized, message = evaluate_systemd_status(status, definition.expected_mode)
    dependencies: list[HealthDependencyCheck] = []

    if definition.unit == "remihub-bindmount.service":
        dependencies = [
            _path_check(
                PathDefinition(
                    "remihub-bindmount-source",
                    "RemiHub Bind Mount Source",
                    HealthComponentGroup.STORAGE,
                    "/mnt/secure-pool/Q_Drive/Projects/RemiHub",
                    HealthComponentKind.PATH,
                )
            ),
            _path_check(
                PathDefinition(
                    "remihub-bindmount-target",
                    "RemiHub Bind Mount Target",
                    HealthComponentGroup.STORAGE,
                    "/opt/remihub",
                    HealthComponentKind.PATH,
                )
            ),
        ]
        combined = _combine_status([normalized, *(dep.status for dep in dependencies)])
        if combined != normalized:
            normalized = combined
            message = "Bind mount dependency path check is not healthy"

    return _component(
        id=definition.id,
        name=definition.name,
        group=definition.group,
        kind=HealthComponentKind.SYSTEMD_UNIT,
        status=normalized,
        message=message,
        checked_at=checked_at,
        required=definition.required,
        unit=definition.unit,
        expected_mode=definition.expected_mode,
        systemd=_systemd_metadata(status),
        dependencies=dependencies,
    )


def evaluate_path_component(definition: PathDefinition, checked_at: datetime) -> HealthComponent:
    check = _path_check(definition)
    return _component(
        id=definition.id,
        name=definition.name,
        group=definition.group,
        kind=definition.kind,
        status=check.status,
        message=check.message,
        checked_at=checked_at,
        required=definition.required,
        dependencies=[check],
    )


def inspect_jdownloader_user_service() -> HealthDependencyCheck:
    unit_file = Path(JDOWNLOADER_USER_UNIT)
    enabled_link = Path(JDOWNLOADER_USER_UNIT_LINK)
    try:
        if not unit_file.is_file():
            return HealthDependencyCheck(
                id="jdownloader-user-service",
                name="JDownloader User Service",
                kind=HealthComponentKind.USER_SYSTEMD_UNIT,
                status=HealthStatus.UNKNOWN,
                message="Alex user service file is not present",
                path=JDOWNLOADER_USER_UNIT,
                expected="user_systemd_unit",
                observed="missing",
            )
        if not enabled_link.exists():
            return HealthDependencyCheck(
                id="jdownloader-user-service",
                name="JDownloader User Service",
                kind=HealthComponentKind.USER_SYSTEMD_UNIT,
                status=HealthStatus.DEGRADED,
                message="Alex user service is installed but not enabled for graphical session",
                path=JDOWNLOADER_USER_UNIT,
                expected="enabled_user_systemd_unit",
                observed="installed_not_enabled",
            )
    except Exception as exc:
        return HealthDependencyCheck(
            id="jdownloader-user-service",
            name="JDownloader User Service",
            kind=HealthComponentKind.USER_SYSTEMD_UNIT,
            status=HealthStatus.UNKNOWN,
            message=f"Alex user service inspection unavailable: {type(exc).__name__}",
            path=JDOWNLOADER_USER_UNIT,
            expected="user_systemd_unit",
            observed="unknown",
        )

    if os.environ.get("XDG_RUNTIME_DIR") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        command = [
            SYSTEMCTL,
            "--user",
            "show",
            JDOWNLOADER_SYSTEMD_UNIT_NAME,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=Result",
            "--no-pager",
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=SYSTEMD_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            return HealthDependencyCheck(
                id="jdownloader-user-service",
                name="JDownloader User Service",
                kind=HealthComponentKind.USER_SYSTEMD_UNIT,
                status=HealthStatus.UNKNOWN,
                message=f"Alex user service state unavailable: {type(exc).__name__}",
                path=JDOWNLOADER_USER_UNIT,
                expected="running_user_systemd_unit",
                observed="unknown",
            )

        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value

        load_state = values.get("LoadState")
        active_state = values.get("ActiveState")
        sub_state = values.get("SubState")
        service_result = values.get("Result")

        if result.returncode != 0 or load_state in {"", "not-found", "error"}:
            return HealthDependencyCheck(
                id="jdownloader-user-service",
                name="JDownloader User Service",
                kind=HealthComponentKind.USER_SYSTEMD_UNIT,
                status=HealthStatus.UNKNOWN,
                message="Alex user service state is unavailable",
                path=JDOWNLOADER_USER_UNIT,
                expected="running_user_systemd_unit",
                observed="unknown",
            )
        if active_state == "failed" or service_result not in {None, "", "success"}:
            return HealthDependencyCheck(
                id="jdownloader-user-service",
                name="JDownloader User Service",
                kind=HealthComponentKind.USER_SYSTEMD_UNIT,
                status=HealthStatus.UNHEALTHY,
                message="Alex user service is failed or last invocation failed",
                path=JDOWNLOADER_USER_UNIT,
                expected="running_user_systemd_unit",
                observed=f"{active_state}/{sub_state}",
            )
        if active_state == "active":
            return HealthDependencyCheck(
                id="jdownloader-user-service",
                name="JDownloader User Service",
                kind=HealthComponentKind.USER_SYSTEMD_UNIT,
                status=HealthStatus.HEALTHY,
                message="Alex user service is active",
                path=JDOWNLOADER_USER_UNIT,
                expected="running_user_systemd_unit",
                observed=f"{active_state}/{sub_state}",
            )
        if active_state == "inactive":
            return HealthDependencyCheck(
                id="jdownloader-user-service",
                name="JDownloader User Service",
                kind=HealthComponentKind.USER_SYSTEMD_UNIT,
                status=HealthStatus.DEGRADED,
                message="Alex user service is installed but stopped",
                path=JDOWNLOADER_USER_UNIT,
                expected="running_user_systemd_unit",
                observed=f"{active_state}/{sub_state}",
            )

        return HealthDependencyCheck(
            id="jdownloader-user-service",
            name="JDownloader User Service",
            kind=HealthComponentKind.USER_SYSTEMD_UNIT,
            status=HealthStatus.UNKNOWN,
            message="Alex user service state is not recognized",
            path=JDOWNLOADER_USER_UNIT,
            expected="running_user_systemd_unit",
            observed=f"{active_state}/{sub_state}",
        )

    return HealthDependencyCheck(
        id="jdownloader-user-service",
        name="JDownloader User Service",
        kind=HealthComponentKind.USER_SYSTEMD_UNIT,
        status=HealthStatus.UNKNOWN,
        message="Alex user systemd manager state is not safely observable from the backend runtime",
        path=JDOWNLOADER_USER_UNIT,
        expected="running_user_systemd_unit",
        observed="unobservable",
    )


def inspect_jdownloader_process() -> HealthDependencyCheck:
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            cmdline_path = Path("/proc") / pid / "cmdline"
            try:
                cmdline = cmdline_path.read_text(encoding="utf-8", errors="replace")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if "JDownloader.jar" in cmdline:
                return HealthDependencyCheck(
                    id="jdownloader-process",
                    name="JDownloader Process",
                    kind=HealthComponentKind.PROCESS,
                    status=HealthStatus.HEALTHY,
                    message="Expected JDownloader process is present",
                    expected="process_present",
                    observed="process_present",
                )
    except Exception as exc:
        return HealthDependencyCheck(
            id="jdownloader-process",
            name="JDownloader Process",
            kind=HealthComponentKind.PROCESS,
            status=HealthStatus.UNKNOWN,
            message=f"Process inspection unavailable: {type(exc).__name__}",
            expected="process_present",
            observed="unknown",
        )

    return HealthDependencyCheck(
        id="jdownloader-process",
        name="JDownloader Process",
        kind=HealthComponentKind.PROCESS,
        status=HealthStatus.DEGRADED,
        message="Expected JDownloader process is not present",
        expected="process_present",
        observed="not_present",
    )


def evaluate_jdownloader_component(checked_at: datetime) -> HealthComponent:
    checks = [_path_check(definition) for definition in JDOWNLOADER_PATHS]
    checks.append(inspect_jdownloader_user_service())
    checks.append(inspect_jdownloader_process())

    status = _combine_status([check.status for check in checks])
    if status == HealthStatus.HEALTHY:
        message = "JDownloader runtime and required FolderWatch dependencies are healthy"
    elif HealthStatus.UNHEALTHY in {check.status for check in checks}:
        message = "JDownloader required file or FolderWatch dependency is unhealthy"
    elif HealthStatus.DEGRADED in {check.status for check in checks}:
        message = "JDownloader is installed but runtime signal is degraded"
    else:
        message = "JDownloader runtime state is not safely observable"

    return _component(
        id="jdownloader",
        name="JDownloader",
        group=HealthComponentGroup.MEDIA,
        kind=HealthComponentKind.COMPOSITE,
        status=status,
        message=message,
        checked_at=checked_at,
        dependencies=checks,
    )


def aggregate_overall(components: list[HealthComponent]) -> HealthStatus:
    required_statuses = [
        component.status for component in components if component.required
    ]
    if HealthStatus.UNHEALTHY in required_statuses:
        return HealthStatus.UNHEALTHY
    if HealthStatus.DEGRADED in required_statuses or HealthStatus.UNKNOWN in required_statuses:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


def get_service_health_snapshot() -> ServiceHealthSnapshotResponse:
    checked_at = datetime.now(timezone.utc)
    components: list[HealthComponent] = []

    for definition in UNIT_DEFINITIONS:
        try:
            components.append(evaluate_unit_component(definition, checked_at))
        except Exception as exc:
            components.append(
                _component(
                    id=definition.id,
                    name=definition.name,
                    group=definition.group,
                    kind=HealthComponentKind.SYSTEMD_UNIT,
                    status=HealthStatus.UNKNOWN,
                    message=f"Health evaluation unavailable: {type(exc).__name__}",
                    checked_at=checked_at,
                    required=definition.required,
                    unit=definition.unit,
                    expected_mode=definition.expected_mode,
                )
            )

    for definition in PATH_DEFINITIONS:
        try:
            components.append(evaluate_path_component(definition, checked_at))
        except Exception as exc:
            components.append(
                _component(
                    id=definition.id,
                    name=definition.name,
                    group=definition.group,
                    kind=definition.kind,
                    status=HealthStatus.UNKNOWN,
                    message=f"Path health evaluation unavailable: {type(exc).__name__}",
                    checked_at=checked_at,
                    required=definition.required,
                )
            )

    try:
        components.append(evaluate_jdownloader_component(checked_at))
    except Exception as exc:
        components.append(
            _component(
                id="jdownloader",
                name="JDownloader",
                group=HealthComponentGroup.MEDIA,
                kind=HealthComponentKind.COMPOSITE,
                status=HealthStatus.UNKNOWN,
                message=f"JDownloader health evaluation unavailable: {type(exc).__name__}",
                checked_at=checked_at,
            )
        )

    return ServiceHealthSnapshotResponse(
        success=True,
        checked_at=checked_at,
        overall=aggregate_overall(components),
        components=components,
    )

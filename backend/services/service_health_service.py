from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import os
import pwd
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
SNAPSHOT_SINGLETON_ID = "current"
SNAPSHOT_FRESHNESS_COMPONENT_ID = "health-collector-snapshot-freshness"
SNAPSHOT_STALE_AFTER_SECONDS = 90
COMMIT_HASH_LENGTH = 40
DEPLOYMENT_BASELINE_PARITY_COMPONENT_ID = "deployment-baseline-parity"
DEPLOYMENT_BASELINE_OBSERVATION_PATH = Path(
    "/var/lib/remihub-agent/health-observations/backend/deployment-baseline.json"
)
DEPLOYMENT_BASELINE_OBSERVATION_STALE_AFTER_SECONDS = 180
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
        "Backend Production Deployment Timer",
        HealthComponentGroup.AGENT,
        "remihub-agent-backend-deployment.timer",
        ExpectedMode.ARMED_TIMER_OR_PATH,
    ),
    UnitDefinition(
        "remihub-agent-backend-qa-deployment-timer",
        "Backend QA Deployment Timer",
        HealthComponentGroup.AGENT,
        "remihub-agent-backend-qa-deployment.timer",
        ExpectedMode.ARMED_TIMER_OR_PATH,
    ),
    UnitDefinition(
        "remihub-agent-backend-deployment-trigger-path",
        "Backend QA Deployment Trigger Path",
        HealthComponentGroup.AGENT,
        "remihub-agent-backend-deployment-trigger.path",
        ExpectedMode.ARMED_TIMER_OR_PATH,
    ),
    UnitDefinition(
        "remihub-agent-backend-production-deployment-trigger-path",
        "Backend Production Deployment Trigger Path",
        HealthComponentGroup.AGENT,
        "remihub-agent-backend-production-deployment-trigger.path",
        ExpectedMode.ARMED_TIMER_OR_PATH,
    ),
    UnitDefinition(
        "remihub-health-collector-timer",
        "Health Collector Timer",
        HealthComponentGroup.CORE,
        "remihub-health-collector.timer",
        ExpectedMode.ARMED_TIMER_OR_PATH,
    ),
    UnitDefinition(
        "remihub-agent-deployment-baseline-observer-timer",
        "Deployment Baseline Observer Timer",
        HealthComponentGroup.AGENT,
        "remihub-agent-deployment-baseline-observer.timer",
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
        "Backend QA Deployment Trigger",
        HealthComponentGroup.AGENT,
        "remihub-agent-backend-deployment-trigger.service",
        ExpectedMode.ON_DEMAND,
    ),
    UnitDefinition(
        "remihub-agent-backend-production-deployment-trigger",
        "Backend Production Deployment Trigger",
        HealthComponentGroup.AGENT,
        "remihub-agent-backend-production-deployment-trigger.service",
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
        "remihub-health-collector",
        "Health Collector",
        HealthComponentGroup.CORE,
        "remihub-health-collector.service",
        ExpectedMode.ON_DEMAND,
    ),
    UnitDefinition(
        "remihub-agent-deployment-baseline-observer",
        "Deployment Baseline Observer",
        HealthComponentGroup.AGENT,
        "remihub-agent-deployment-baseline-observer.service",
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

JDOWNLOADER_USER = "alex"
JDOWNLOADER_SYSTEMD_UNIT_NAME = "jdownloader.service"

DEPLOYMENT_BASELINE_OBSERVATION_ROWS = (
    ("canonical", "Canonical / Production Runtime", "HEAD", "main"),
    ("planning", "Planning", "HEAD", "main"),
    ("implementation-main", "Implementation Main", "refs/heads/main", None),
    ("qa-source", "QA Source", "refs/heads/qa-main", None),
    ("qa-runtime", "QA Runtime", "HEAD", "qa-runtime"),
    ("production-source", "Production Source", "refs/heads/production-main", None),
    ("github-main", "GitHub Main", "refs/heads/main", None),
)


@dataclass(frozen=True)
class JDownloaderUserRuntime:
    username: str
    uid: int
    home: Path
    runtime_dir: Path
    bus_path: Path
    unit_file: Path
    enabled_link: Path


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


def _normalized_systemd_value(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _is_zero_exit_status(value: object) -> bool:
    return _normalized_systemd_value(value) in {None, "", "0"}


def _is_success_exit(status: SystemdUnitStatus) -> bool:
    code = _normalized_systemd_value(status.get("ExecMainCode"))
    return code in {None, "", "0", "1", "exited"} and _is_zero_exit_status(
        status.get("ExecMainStatus")
    )


def _is_failed_exit(status: SystemdUnitStatus) -> bool:
    code = _normalized_systemd_value(status.get("ExecMainCode"))
    if code in {None, "", "0", "1", "exited"}:
        return not _is_zero_exit_status(status.get("ExecMainStatus"))
    if code not in {None, "", "0"}:
        return True
    return False


def _is_commit_hash(value: str | None) -> bool:
    if value is None or len(value) != COMMIT_HASH_LENGTH:
        return False
    return all(character in "0123456789abcdef" for character in value)


def get_db_conn():
    from backend.database.database import get_db_conn as acquire_connection

    return acquire_connection()


def put_db_conn(conn) -> None:
    from backend.database.database import put_db_conn as release_connection

    release_connection(conn)


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
    unit_type = status.get("Type")

    if load_state in {"not-found", "error"}:
        return HealthStatus.UNKNOWN, "Systemd unit is absent or unreadable"

    if active_state == "failed" or result not in {None, "", "success"}:
        return HealthStatus.UNHEALTHY, "Systemd unit is failed or last invocation failed"

    if expected_mode == ExpectedMode.PERSISTENT_RUNNING:
        if active_state == "active" and sub_state == "running":
            return HealthStatus.HEALTHY, "Persistent service is active and running"
        return HealthStatus.UNHEALTHY, "Persistent service is not running"

    if expected_mode == ExpectedMode.ARMED_TIMER_OR_PATH:
        if active_state == "active" and sub_state in {"waiting", "running"}:
            return HealthStatus.HEALTHY, "Timer or path watcher is armed"
        return HealthStatus.UNHEALTHY, "Timer or path watcher is not armed"

    if expected_mode in {ExpectedMode.ON_DEMAND, ExpectedMode.QA_RUNTIME}:
        if active_state == "active":
            return HealthStatus.HEALTHY, "On-demand unit is currently active"
        if active_state == "activating" and sub_state == "start" and unit_type == "oneshot":
            return HealthStatus.HEALTHY, "On-demand oneshot is currently starting"
        if (
            active_state == "inactive"
            and sub_state in {"dead", "exited"}
            and unit_type == "oneshot"
            and _is_success_result(result)
        ):
            return HealthStatus.IDLE, "On-demand unit is idle with no failed result"
        if _is_failed_exit(status):
            return HealthStatus.UNHEALTHY, "On-demand unit last exit status was not successful"
        if active_state == "inactive" and sub_state in {"dead", "exited"} and _is_success_result(result):
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


def _baseline_check(
    *,
    id: str,
    name: str,
    status: HealthStatus,
    message: str,
    path: str | None,
    expected: str | None,
    observed: str | None,
) -> HealthDependencyCheck:
    return HealthDependencyCheck(
        id=f"deployment-baseline-{id}",
        name=name,
        kind=HealthComponentKind.COMPOSITE,
        status=status,
        message=message,
        path=path,
        expected=expected,
        observed=observed,
    )


def _deployment_baseline_empty_checks(message: str) -> list[HealthDependencyCheck]:
    return [
        _baseline_check(
            id=id,
            name=name,
            status=HealthStatus.UNKNOWN,
            message=message,
            path=str(DEPLOYMENT_BASELINE_OBSERVATION_PATH),
            expected=branch or ref,
            observed="unknown",
        )
        for id, name, ref, branch in DEPLOYMENT_BASELINE_OBSERVATION_ROWS
    ]


def _parse_observation_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _observation_row_status(row: dict, name: str) -> tuple[HealthStatus, str, str | None]:
    status = row.get("status")
    reason = row.get("reason")
    if not isinstance(reason, str) or not reason:
        reason = None
    if status in {"ok", "verified"}:
        return HealthStatus.HEALTHY, f"{name} commit is observable", None
    if status == "branch_mismatch":
        expected = row.get("expected_branch") or row.get("branch")
        observed = row.get("observed_branch")
        if isinstance(expected, str) and isinstance(observed, str):
            return (
                HealthStatus.DEGRADED,
                f"{name} is on {observed}, expected {expected}",
                observed,
            )
        return HealthStatus.DEGRADED, f"{name} branch does not match the expected branch", None
    if status in {"mismatch", "degraded", "unverified"}:
        return HealthStatus.DEGRADED, reason or f"{name} observation is degraded", None
    return HealthStatus.UNKNOWN, reason or f"{name} could not be inspected by the protected observer", None


def _deployment_baseline_checks_from_observation(
    checked_at: datetime,
) -> tuple[list[HealthDependencyCheck], str | None]:
    try:
        if not DEPLOYMENT_BASELINE_OBSERVATION_PATH.is_file():
            return (
                _deployment_baseline_empty_checks(
                    "Protected baseline observation is unavailable"
                ),
                "Protected baseline observation is unavailable.",
            )
        payload = json.loads(
            DEPLOYMENT_BASELINE_OBSERVATION_PATH.read_text(encoding="utf-8")
        )
    except Exception:
        return (
            _deployment_baseline_empty_checks(
                "Protected baseline observation is unreadable"
            ),
            "Protected baseline observation is unreadable.",
        )

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return (
            _deployment_baseline_empty_checks(
                "Protected baseline observation is invalid"
            ),
            "Protected baseline observation is invalid.",
        )

    observed_at = _parse_observation_timestamp(payload.get("observed_at"))
    if observed_at is None:
        return (
            _deployment_baseline_empty_checks(
                "Protected baseline observation timestamp is invalid"
            ),
            "Protected baseline observation timestamp is invalid.",
        )
    reference_time = checked_at if checked_at.tzinfo else checked_at.replace(tzinfo=timezone.utc)
    if reference_time - observed_at > timedelta(
        seconds=DEPLOYMENT_BASELINE_OBSERVATION_STALE_AFTER_SECONDS
    ):
        return (
            _deployment_baseline_empty_checks(
                "Protected baseline observation is stale"
            ),
            "Protected baseline observation is stale.",
        )

    rows = payload.get("observations")
    if not isinstance(rows, list):
        return (
            _deployment_baseline_empty_checks(
                "Protected baseline observation rows are invalid"
            ),
            "Protected baseline observation rows are invalid.",
        )
    by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}

    checks: list[HealthDependencyCheck] = []
    for id, name, ref, branch in DEPLOYMENT_BASELINE_OBSERVATION_ROWS:
        row = by_id.get(id)
        if not isinstance(row, dict):
            checks.append(
                _baseline_check(
                    id=id,
                    name=name,
                    status=HealthStatus.UNKNOWN,
                    message=f"{name} is missing from the protected observation",
                    path=str(DEPLOYMENT_BASELINE_OBSERVATION_PATH),
                    expected=branch or ref,
                    observed="unknown",
                )
            )
            continue
        commit = row.get("commit")
        status, message, fallback_observed = _observation_row_status(row, name)
        if status != HealthStatus.UNKNOWN and not _is_commit_hash(commit):
            status = HealthStatus.UNKNOWN
            message = f"{name} protected observation has no commit"
        checks.append(
            _baseline_check(
                id=id,
                name=name,
                status=status,
                message=message,
                path=str(DEPLOYMENT_BASELINE_OBSERVATION_PATH),
                expected=branch or ref,
                observed=commit if _is_commit_hash(commit) else fallback_observed or "unknown",
            )
        )
    return checks, None


def _backend_deployment_is_active() -> bool:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM agent.runs AS runs
                JOIN agent.cards AS cards
                  ON cards.id = runs.card_id
                WHERE runs.phase = 'deployment'
                  AND runs.status IN ('queued', 'claimed', 'running')
                  AND cards.repository_scope IN ('backend', 'backend_and_android')
                LIMIT 1;
                """
            )
            return cur.fetchone() is not None
    finally:
        put_db_conn(conn)


def _first_mismatch_message(mismatches: list[HealthDependencyCheck]) -> str:
    first = mismatches[0]
    if first.id == "deployment-baseline-github-main":
        return "GitHub main does not match the deployed backend baseline."
    if first.id == "deployment-baseline-qa-runtime":
        return "QA runtime does not match the settled backend baseline."
    return f"{first.name} does not match the settled backend baseline."


def evaluate_deployment_baseline_parity_component(
    checked_at: datetime,
) -> HealthComponent:
    checks, observation_error = _deployment_baseline_checks_from_observation(checked_at)
    if observation_error is not None:
        return _component(
            id=DEPLOYMENT_BASELINE_PARITY_COMPONENT_ID,
            name="Deployment Baseline Parity",
            group=HealthComponentGroup.CORE,
            kind=HealthComponentKind.COMPOSITE,
            status=HealthStatus.UNKNOWN,
            message=observation_error,
            checked_at=checked_at,
            dependencies=checks,
        )

    unknown = [check for check in checks if check.status == HealthStatus.UNKNOWN]
    baseline = next(
        (
            check.observed
            for check in checks
            if check.id == "deployment-baseline-canonical"
            and _is_commit_hash(check.observed)
        ),
        None,
    )
    if baseline is None:
        return _component(
            id=DEPLOYMENT_BASELINE_PARITY_COMPONENT_ID,
            name="Deployment Baseline Parity",
            group=HealthComponentGroup.CORE,
            kind=HealthComponentKind.COMPOSITE,
            status=HealthStatus.UNKNOWN,
            message="Canonical / Production Runtime backend baseline could not be inspected.",
            checked_at=checked_at,
            dependencies=checks,
        )
    if unknown:
        return _component(
            id=DEPLOYMENT_BASELINE_PARITY_COMPONENT_ID,
            name="Deployment Baseline Parity",
            group=HealthComponentGroup.CORE,
            kind=HealthComponentKind.COMPOSITE,
            status=HealthStatus.UNKNOWN,
            message=f"{unknown[0].name} could not be inspected.",
            checked_at=checked_at,
            dependencies=checks,
        )

    normalized: list[HealthDependencyCheck] = []
    mismatches: list[HealthDependencyCheck] = []
    for check in checks:
        if check.status == HealthStatus.DEGRADED:
            mismatch = check.model_copy(
                update={
                    "status": HealthStatus.DEGRADED,
                    "expected": baseline if _is_commit_hash(check.observed) else check.expected,
                }
            )
            normalized.append(mismatch)
            mismatches.append(mismatch)
        elif check.observed == baseline:
            normalized.append(
                check.model_copy(
                    update={
                        "status": HealthStatus.HEALTHY,
                        "message": f"{check.name} matches the backend baseline",
                        "expected": baseline,
                    }
                )
            )
        else:
            mismatch = check.model_copy(
                update={
                    "status": HealthStatus.DEGRADED,
                    "message": f"{check.name} observed commit differs from baseline",
                    "expected": baseline,
                }
            )
            normalized.append(mismatch)
            mismatches.append(mismatch)

    deployment_active = False
    try:
        deployment_active = _backend_deployment_is_active()
    except Exception:
        deployment_active = False

    if mismatches:
        message = (
            "Backend deployment is in progress; baseline convergence is still settling."
            if deployment_active
            else _first_mismatch_message(mismatches)
        )
        return _component(
            id=DEPLOYMENT_BASELINE_PARITY_COMPONENT_ID,
            name="Deployment Baseline Parity",
            group=HealthComponentGroup.CORE,
            kind=HealthComponentKind.COMPOSITE,
            status=HealthStatus.DEGRADED,
            message=message,
            checked_at=checked_at,
            dependencies=normalized,
        )

    return _component(
        id=DEPLOYMENT_BASELINE_PARITY_COMPONENT_ID,
        name="Deployment Baseline Parity",
        group=HealthComponentGroup.CORE,
        kind=HealthComponentKind.COMPOSITE,
        status=HealthStatus.HEALTHY,
        message="Backend deployment baseline identities are converged.",
        checked_at=checked_at,
        dependencies=normalized,
    )


def _jdownloader_user_runtime(username: str = JDOWNLOADER_USER) -> JDownloaderUserRuntime:
    user = pwd.getpwnam(username)
    home = Path(user.pw_dir)
    runtime_dir = Path("/run/user") / str(user.pw_uid)
    return JDownloaderUserRuntime(
        username=username,
        uid=user.pw_uid,
        home=home,
        runtime_dir=runtime_dir,
        bus_path=runtime_dir / "bus",
        unit_file=home / ".config/systemd/user/jdownloader.service",
        enabled_link=(
            home
            / ".config/systemd/user/graphical-session.target.wants/"
            / "jdownloader.service"
        ),
    )


def _jdownloader_user_service_check(
    *,
    status: HealthStatus,
    message: str,
    path: str | None,
    expected: str,
    observed: str,
) -> HealthDependencyCheck:
    return HealthDependencyCheck(
        id="jdownloader-user-service",
        name="JDownloader User Service",
        kind=HealthComponentKind.USER_SYSTEMD_UNIT,
        status=status,
        message=message,
        path=path,
        expected=expected,
        observed=observed,
    )


def _jdownloader_session_unavailable(
    runtime: JDownloaderUserRuntime,
    observed: str,
) -> HealthDependencyCheck:
    display_name = runtime.username.capitalize()
    return _jdownloader_user_service_check(
        status=HealthStatus.DEGRADED,
        message=(
            f"{display_name} user session/systemd manager is not active; "
            "JDownloader is unavailable until the graphical session is started"
        ),
        path=str(runtime.unit_file),
        expected="active_user_session_systemd_manager",
        observed=observed,
    )


def _jdownloader_observed_state(values: dict[str, str]) -> str:
    return (
        f"load={values.get('LoadState') or 'unknown'};"
        f"active={values.get('ActiveState') or 'unknown'};"
        f"sub={values.get('SubState') or 'unknown'};"
        f"result={values.get('Result') or 'unknown'};"
        f"unit_file={values.get('UnitFileState') or 'unknown'}"
    )


def _user_manager_unavailable_error(stderr: str) -> bool:
    lower = stderr.lower()
    return (
        "failed to connect to bus" in lower
        or "no medium found" in lower
        or "connection refused" in lower
    ) and "permission denied" not in lower


def _jdownloader_query_environment(runtime: JDownloaderUserRuntime) -> dict[str, str]:
    return {
        "HOME": str(runtime.home),
        "USER": runtime.username,
        "LOGNAME": runtime.username,
        "PATH": "/usr/bin:/bin",
        "XDG_RUNTIME_DIR": str(runtime.runtime_dir),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime.bus_path}",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def inspect_jdownloader_user_service() -> HealthDependencyCheck:
    try:
        runtime = _jdownloader_user_runtime()
    except KeyError:
        return _jdownloader_user_service_check(
            status=HealthStatus.UNKNOWN,
            message=f"{JDOWNLOADER_USER.capitalize()} user service owner is not present",
            path=None,
            expected="configured_user",
            observed="missing_user",
        )
    except Exception as exc:
        return _jdownloader_user_service_check(
            status=HealthStatus.UNKNOWN,
            message=f"Alex user service owner lookup unavailable: {type(exc).__name__}",
            path=None,
            expected="configured_user",
            observed="unknown",
        )

    try:
        if not runtime.unit_file.is_file():
            return _jdownloader_user_service_check(
                status=HealthStatus.UNKNOWN,
                message="Alex user service file is not present",
                path=str(runtime.unit_file),
                expected="user_systemd_unit",
                observed="missing",
            )
        if not runtime.enabled_link.exists():
            return _jdownloader_user_service_check(
                status=HealthStatus.DEGRADED,
                message="Alex user service is installed but not enabled for graphical session",
                path=str(runtime.unit_file),
                expected="enabled_user_systemd_unit",
                observed="installed_not_enabled",
            )
        if os.geteuid() != runtime.uid:
            return _jdownloader_user_service_check(
                status=HealthStatus.UNKNOWN,
                message="Alex user service state requires collection under the Alex user identity",
                path=str(runtime.unit_file),
                expected=f"user_identity:{runtime.uid}",
                observed=f"user_identity:{os.geteuid()}",
            )
        if not runtime.runtime_dir.exists() or not runtime.runtime_dir.is_dir():
            return _jdownloader_session_unavailable(runtime, "runtime_dir_unavailable")
        if not runtime.bus_path.exists() or not runtime.bus_path.is_socket():
            return _jdownloader_session_unavailable(runtime, "user_bus_unavailable")
    except Exception as exc:
        return _jdownloader_user_service_check(
            status=HealthStatus.UNKNOWN,
            message=f"Alex user service inspection unavailable: {type(exc).__name__}",
            path=str(runtime.unit_file),
            expected="user_systemd_unit",
            observed="unknown",
        )

    command = [
        SYSTEMCTL,
        "--user",
        "show",
        JDOWNLOADER_SYSTEMD_UNIT_NAME,
        *(f"--property={prop}" for prop in SYSTEMD_PROPERTIES),
        "--no-pager",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=SYSTEMD_TIMEOUT_SECONDS,
            env=_jdownloader_query_environment(runtime),
        )
    except Exception as exc:
        return _jdownloader_user_service_check(
            status=HealthStatus.UNKNOWN,
            message=f"Alex user service state unavailable: {type(exc).__name__}",
            path=str(runtime.unit_file),
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
    systemd_status = SystemdUnitStatus(
        unit=JDOWNLOADER_SYSTEMD_UNIT_NAME,
        available=(
            result.returncode == 0 and load_state not in {"", "not-found", "error"}
        ),
        properties=values,
    )
    observed = _jdownloader_observed_state(values)

    if result.returncode != 0:
        if _user_manager_unavailable_error(result.stderr):
            return _jdownloader_session_unavailable(runtime, "user_manager_unavailable")
        return _jdownloader_user_service_check(
            status=HealthStatus.UNKNOWN,
            message="Alex user service state is unavailable",
            path=str(runtime.unit_file),
            expected="running_user_systemd_unit",
            observed="unknown",
        )

    if not systemd_status.available:
        return _jdownloader_user_service_check(
            status=HealthStatus.UNKNOWN,
            message="Alex user service state is unavailable",
            path=str(runtime.unit_file),
            expected="running_user_systemd_unit",
            observed=observed,
        )

    if (
        active_state == "failed"
        or service_result not in {None, "", "success"}
        or _is_failed_exit(systemd_status)
    ):
        return _jdownloader_user_service_check(
            status=HealthStatus.UNHEALTHY,
            message="Alex user service is failed or last invocation failed",
            path=str(runtime.unit_file),
            expected="running_user_systemd_unit",
            observed=observed,
        )
    if active_state == "active" and sub_state == "running":
        return _jdownloader_user_service_check(
            status=HealthStatus.HEALTHY,
            message="Alex user service is active and running",
            path=str(runtime.unit_file),
            expected="running_user_systemd_unit",
            observed=observed,
        )
    if active_state in {"active", "inactive"}:
        return _jdownloader_user_service_check(
            status=HealthStatus.DEGRADED,
            message="Alex user systemd manager is observable but JDownloader is not running",
            path=str(runtime.unit_file),
            expected="running_user_systemd_unit",
            observed=observed,
        )

    return _jdownloader_user_service_check(
        status=HealthStatus.UNKNOWN,
        message="Alex user service state is not recognized",
        path=str(runtime.unit_file),
        expected="running_user_systemd_unit",
        observed=observed,
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


def collect_service_health_snapshot() -> ServiceHealthSnapshotResponse:
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
        components.append(evaluate_deployment_baseline_parity_component(checked_at))
    except Exception as exc:
        components.append(
            _component(
                id=DEPLOYMENT_BASELINE_PARITY_COMPONENT_ID,
                name="Deployment Baseline Parity",
                group=HealthComponentGroup.CORE,
                kind=HealthComponentKind.COMPOSITE,
                status=HealthStatus.UNKNOWN,
                message=f"Deployment baseline parity evaluation unavailable: {type(exc).__name__}",
                checked_at=checked_at,
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


def _snapshot_payload(snapshot: ServiceHealthSnapshotResponse) -> dict:
    validated = ServiceHealthSnapshotResponse.model_validate(snapshot)
    return validated.model_dump(mode="json")


def persist_service_health_snapshot(snapshot: ServiceHealthSnapshotResponse) -> None:
    validated = ServiceHealthSnapshotResponse.model_validate(snapshot)
    payload = _snapshot_payload(validated)
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.service_health_current_snapshot (
                    singleton_id,
                    checked_at,
                    overall,
                    snapshot,
                    updated_at
                )
                VALUES (%s, %s, %s, %s::jsonb, CURRENT_TIMESTAMP)
                ON CONFLICT (singleton_id) DO UPDATE
                SET checked_at = EXCLUDED.checked_at,
                    overall = EXCLUDED.overall,
                    snapshot = EXCLUDED.snapshot,
                    updated_at = CURRENT_TIMESTAMP
                WHERE public.service_health_current_snapshot.checked_at <= EXCLUDED.checked_at;
                """,
                (
                    SNAPSHOT_SINGLETON_ID,
                    validated.checked_at,
                    validated.overall.value,
                    json.dumps(payload, separators=(",", ":")),
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError("Stored service health snapshot is newer than this collection")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def load_current_service_health_snapshot() -> ServiceHealthSnapshotResponse | None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT snapshot
                FROM public.service_health_current_snapshot
                WHERE singleton_id = %s;
                """,
                (SNAPSHOT_SINGLETON_ID,),
            )
            row = cur.fetchone()
    finally:
        put_db_conn(conn)

    if row is None:
        return None

    payload = row[0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return ServiceHealthSnapshotResponse.model_validate(payload)


def _freshness_component(
    *,
    status: HealthStatus,
    message: str,
    checked_at: datetime,
) -> HealthComponent:
    return _component(
        id=SNAPSHOT_FRESHNESS_COMPONENT_ID,
        name="Health Collector / Snapshot Freshness",
        group=HealthComponentGroup.CORE,
        kind=HealthComponentKind.COMPOSITE,
        status=status,
        message=message,
        checked_at=checked_at,
        required=True,
    )


def _without_freshness_component(components: list[HealthComponent]) -> list[HealthComponent]:
    return [
        component
        for component in components
        if component.id != SNAPSHOT_FRESHNESS_COMPONENT_ID
    ]


def _with_freshness_component(
    snapshot: ServiceHealthSnapshotResponse,
    *,
    now: datetime | None = None,
) -> ServiceHealthSnapshotResponse:
    evaluated_at = now or datetime.now(timezone.utc)
    checked_at = snapshot.checked_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    age = evaluated_at - checked_at
    fresh = age <= timedelta(seconds=SNAPSHOT_STALE_AFTER_SECONDS)
    components = _without_freshness_component(list(snapshot.components))
    if fresh:
        freshness = _freshness_component(
            status=HealthStatus.HEALTHY,
            message="Health collector snapshot is fresh",
            checked_at=evaluated_at,
        )
    else:
        freshness = _freshness_component(
            status=HealthStatus.DEGRADED,
            message=(
                "Health collector snapshot is stale; showing last known component states"
            ),
            checked_at=evaluated_at,
        )
    components.append(freshness)
    return ServiceHealthSnapshotResponse(
        success=True,
        checked_at=snapshot.checked_at,
        overall=aggregate_overall(components),
        components=components,
    )


def _missing_snapshot_response(*, now: datetime | None = None) -> ServiceHealthSnapshotResponse:
    checked_at = now or datetime.now(timezone.utc)
    components = [
        _freshness_component(
            status=HealthStatus.DEGRADED,
            message="Health collector has not persisted a snapshot yet",
            checked_at=checked_at,
        )
    ]
    return ServiceHealthSnapshotResponse(
        success=True,
        checked_at=checked_at,
        overall=aggregate_overall(components),
        components=components,
    )


def get_service_health_snapshot() -> ServiceHealthSnapshotResponse:
    try:
        snapshot = load_current_service_health_snapshot()
    except Exception:
        checked_at = datetime.now(timezone.utc)
        components = [
            _freshness_component(
                status=HealthStatus.DEGRADED,
                message="Persisted health snapshot is unavailable",
                checked_at=checked_at,
            )
        ]
        return ServiceHealthSnapshotResponse(
            success=True,
            checked_at=checked_at,
            overall=aggregate_overall(components),
            components=components,
        )
    if snapshot is None:
        return _missing_snapshot_response()
    return _with_freshness_component(snapshot)

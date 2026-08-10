from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    IDLE = "idle"
    UNKNOWN = "unknown"


class HealthComponentGroup(str, Enum):
    CORE = "core"
    AGENT = "agent"
    STORAGE = "storage"
    RH_STORAGE = "rh_storage"
    MEDIA = "media"


class HealthComponentKind(str, Enum):
    SYSTEMD_UNIT = "systemd_unit"
    PATH = "path"
    MOUNT = "mount"
    COMPOSITE = "composite"
    PROCESS = "process"
    USER_SYSTEMD_UNIT = "user_systemd_unit"


class HealthDependencyCheck(BaseModel):
    id: str
    name: str
    kind: HealthComponentKind
    status: HealthStatus
    message: str
    path: str | None = None
    expected: str | None = None
    observed: str | None = None


class HealthSystemdMetadata(BaseModel):
    unit: str
    available: bool
    load_state: str | None = None
    active_state: str | None = None
    sub_state: str | None = None
    unit_file_state: str | None = None
    type: str | None = None
    result: str | None = None
    exec_main_code: str | None = None
    exec_main_status: str | None = None
    main_pid: int | None = None
    n_restarts: int | None = None
    active_enter_timestamp: str | None = None
    inactive_enter_timestamp: str | None = None
    state_change_timestamp: str | None = None


class HealthComponent(BaseModel):
    id: str
    name: str
    group: HealthComponentGroup
    kind: HealthComponentKind
    status: HealthStatus
    message: str
    required: bool = True
    unit: str | None = None
    expected_mode: str | None = None
    systemd: HealthSystemdMetadata | None = None
    dependencies: list[HealthDependencyCheck] = Field(default_factory=list)
    checked_at: datetime


class ServiceHealthSnapshotResponse(BaseModel):
    success: Literal[True] = True
    checked_at: datetime
    overall: HealthStatus
    components: list[HealthComponent]

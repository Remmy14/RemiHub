import { apiRequest } from "./authenticatedApi";

export type HealthStatus =
  | "healthy"
  | "degraded"
  | "unhealthy"
  | "idle"
  | "unknown";

export type HealthComponentGroup =
  | "core"
  | "agent"
  | "storage"
  | "rh_storage"
  | "media";

export type HealthComponentKind =
  | "systemd_unit"
  | "path"
  | "mount"
  | "composite"
  | "process"
  | "user_systemd_unit";

export type HealthDependencyCheck = {
  id: string;
  name: string;
  kind: HealthComponentKind;
  status: HealthStatus;
  message: string;
  path: string | null;
  expected: string | null;
  observed: string | null;
};

export type HealthSystemdMetadata = {
  unit: string;
  available: boolean;
  load_state: string | null;
  active_state: string | null;
  sub_state: string | null;
  unit_file_state: string | null;
  type: string | null;
  result: string | null;
  exec_main_code: string | null;
  exec_main_status: string | null;
  main_pid: number | null;
  n_restarts: number | null;
  active_enter_timestamp: string | null;
  inactive_enter_timestamp: string | null;
  state_change_timestamp: string | null;
};

export type HealthComponent = {
  id: string;
  name: string;
  group: HealthComponentGroup | string;
  kind: HealthComponentKind | string;
  status: HealthStatus;
  message: string;
  required: boolean;
  unit: string | null;
  expected_mode: string | null;
  systemd: HealthSystemdMetadata | null;
  dependencies: HealthDependencyCheck[];
  checked_at: string;
};

export type ServiceHealthSnapshotResponse = {
  success: true;
  checked_at: string;
  overall: HealthStatus;
  components: HealthComponent[];
};

export async function getServiceHealthSnapshot(): Promise<ServiceHealthSnapshotResponse> {
  return apiRequest<ServiceHealthSnapshotResponse>("/health/services");
}

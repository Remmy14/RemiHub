import { apiRequest } from "./authenticatedApi";

export type WorkerConfiguration = {
  default_workers: number;
  boost_workers: number | null;
  boost_until: string | null;
  boost_active: boolean;
  effective_workers: number;
  updated_at: string | null;
  server_now: string;
};

export type WorkerCounts = {
  active: number;
  busy: number;
  idle: number;
  retiring: number;
  healthy: number;
  unhealthy: number;
  observed_gap: number;
};

export type StorageWorker = {
  worker_id: string;
  hostname: string | null;
  pid: number | null;
  state: string;
  retiring: boolean;
  last_seen_at: string | null;
  heartbeat_age_seconds: number | null;
  last_error: string | null;
  pool_id: string | null;
  job_id: string | null;
  job_type: string | null;
  rel_path: string | null;
  pool_name: string | null;
  pool_mountpoint: string | null;
  source_branch_id: string | null;
  source_branch_path: string | null;
  source_branch_registered_path: string | null;
  target_branch_id: string | null;
  target_branch_path: string | null;
  target_branch_registered_path: string | null;
  bytes_copied: number | null;
  total_bytes: number | null;
  progress_percent: number | null;
  job_started_at: string | null;
  job_elapsed_seconds: number | null;
  last_progress_at: string | null;
  last_progress_age_seconds: number | null;
};

export type WorkerMonitoring = {
  configuration: WorkerConfiguration;
  monitoring_status: "available" | "no_recent_heartbeats" | "ambiguous";
  observed_daemon_pid: number | null;
  observed_hostname: string | null;
  observed_at: string | null;
  counts: WorkerCounts | null;
  workers: StorageWorker[];
};

type Envelope<T> = { success: boolean; data: T };

async function data<T>(request: Promise<Envelope<T>>): Promise<T> {
  const response = await request;
  if (!response.success) throw new Error("RH-Storage worker request failed.");
  return response.data;
}

const noStore: RequestInit = { cache: "no-store" };

export const rhStorageWorkersApi = {
  configuration: () => data(apiRequest<Envelope<WorkerConfiguration>>("/rh-storage/workers/config", noStore)),
  monitoring: () => data(apiRequest<Envelope<WorkerMonitoring>>("/rh-storage/workers", noStore)),
  role: () => data(apiRequest<Envelope<{ role: string }>>("/auth/me", noStore)),
  setDefault: (defaultWorkers: number) => data(apiRequest<Envelope<WorkerConfiguration>>(
    "/rh-storage/workers/config",
    { method: "PATCH", body: JSON.stringify({ default_workers: defaultWorkers }) },
  )),
  boost: (workers: number, durationMinutes: 15 | 60 | 120 | 240) => data(apiRequest<Envelope<WorkerConfiguration>>(
    "/rh-storage/workers/boost",
    { method: "POST", body: JSON.stringify({ workers, duration_minutes: durationMinutes }) },
  )),
  clearBoost: () => data(apiRequest<Envelope<WorkerConfiguration>>(
    "/rh-storage/workers/boost", { method: "DELETE" },
  )),
};

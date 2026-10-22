import type { StorageWorker, WorkerConfiguration } from "./api/rhStorageWorkers";

export function formatBytes(value: number | null): string {
  if (value === null || !Number.isFinite(value) || value < 0) return "Unavailable";
  if (value === 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const unit = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / 1024 ** unit;
  return `${amount.toFixed(unit === 0 ? 0 : amount < 10 ? 1 : 0)} ${units[unit]}`;
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return "Unavailable";
  const whole = Math.floor(seconds);
  if (whole < 60) return `${whole}s`;
  if (whole < 3600) return `${Math.floor(whole / 60)}m ${whole % 60}s`;
  return `${Math.floor(whole / 3600)}h ${Math.floor(whole % 3600 / 60)}m`;
}

export function formatTimestamp(value: string | null): string {
  if (!value) return "Unavailable";
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? "Unavailable" : time.toLocaleString();
}

export function fileName(path: string | null): string {
  if (!path) return "Unavailable";
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

export function progressPercent(worker: StorageWorker): number | null {
  const value = worker.progress_percent;
  return value === null || !Number.isFinite(value) ? null : Math.max(0, Math.min(100, value));
}

export function boostRemainingSeconds(config: WorkerConfiguration, receivedAt: number, now = Date.now()): number | null {
  if (!config.boost_active || !config.boost_until) return null;
  const expiry = Date.parse(config.boost_until);
  const serverNow = Date.parse(config.server_now);
  if (!Number.isFinite(expiry) || !Number.isFinite(serverNow)) return null;
  return Math.max(0, (expiry - serverNow - (now - receivedAt)) / 1000);
}

export function validWorkerCount(value: string): number | null {
  if (!/^(?:[1-9]|1[0-6])$/.test(value)) return null;
  return Number(value);
}

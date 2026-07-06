import React, { useEffect, useMemo, useState } from "react";

type DriftSummary = {
  total: number;
  needs_repair: number;
  repairing: number;
  blocked: number;
};

type JobSummary = {
  total: number;
  queued: number;
  running: number;
  succeeded: number;
  failed: number;
};

type BranchSummary = {
  pool_id: string;
  path: string;
  online: boolean;
  total_gb: number | null;
  used_gb: number | null;
  free_gb: number | null;
  used_percent: number | null;
  last_selected_at: string | null;
  updated_at: string | null;
};

type PoolStatus = {
  pool_id: string;
  name: string;
  mountpoint: string;
  replication: number;
  min_free_gb: number;
  branch_count: number;
  online_branch_count: number;
  total_gb: number | null;
  used_gb: number | null;
  free_gb: number | null;
  used_percent: number | null;
  drift: DriftSummary;
  jobs: JobSummary;
  branches: BranchSummary[];
};

type RecentJob = {
  job_id: string;
  pool_id: string;
  type: string;
  status: string;
  rel_path: string | null;
  attempts: number;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
  started_at: string | null;
  finished_at: string | null;
};

type ServiceStatus = {
  available: boolean;
  service_name: string;
  active: boolean;
  active_state: string;
  sub_state: string;
  load_state: string;
  unit_file_state: string;
  pid: number | null;
  active_since: string | null;
  error: string | null;
};

type RhStorageStatusResponse = {
  success: boolean;
  updated_at: string;
  service: ServiceStatus;
  pools: PoolStatus[];
  recent_jobs: RecentJob[];
};

const API_BASE = "https://remillard.duckdns.org";

function formatGb(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "—";
  }

  if (value >= 1024) {
    return `${(value / 1024).toFixed(2)} TB`;
  }

  return `${value.toFixed(1)} GB`;
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }

  return new Date(value).toLocaleString();
}

function statusColor(pool: PoolStatus): string {
  if (pool.jobs.failed > 0 || pool.drift.blocked > 0) {
    return "bg-red-500";
  }

  if (pool.drift.needs_repair > 0 || pool.drift.repairing > 0 || pool.jobs.queued > 0 || pool.jobs.running > 0) {
    return "bg-yellow-400";
  }

  return "bg-green-500";
}

function statusLabel(pool: PoolStatus): string {
  if (pool.jobs.failed > 0 || pool.drift.blocked > 0) {
    return "Needs attention";
  }

  if (pool.drift.needs_repair > 0 || pool.drift.repairing > 0 || pool.jobs.queued > 0 || pool.jobs.running > 0) {
    return "Repairing";
  }

  return "Healthy";
}

function ProgressBar({ percent }: { percent: number | null }) {
  const safePercent = Math.max(0, Math.min(100, percent ?? 0));

  return (
    <div className="h-3 w-full overflow-hidden rounded-full bg-slate-200">
      <div
        className="h-full rounded-full bg-blue-500 transition-all duration-500"
        style={{ width: `${safePercent}%` }}
      />
    </div>
  );
}

function StatCard({
  label,
  value,
  subtext,
}: {
  label: string;
  value: string | number;
  subtext?: string;
}) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="text-sm font-medium text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-bold text-slate-900">{value}</div>
      {subtext && <div className="mt-1 text-xs text-slate-500">{subtext}</div>}
    </div>
  );
}

function PoolCard({ pool }: { pool: PoolStatus }) {
  const [showBranches, setShowBranches] = useState(false);
  return (
    <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className={`h-3 w-3 rounded-full ${statusColor(pool)}`} />
            <h2 className="text-xl font-bold text-slate-900">
              {pool.name}
            </h2>
            <span className="rounded-full bg-slate-100 px-2 py-1 text-xs font-semibold text-slate-600">
              {pool.pool_id}
            </span>
          </div>

          <div className="mt-1 text-sm text-slate-500">
            {pool.mountpoint}
          </div>
        </div>

        <div className="rounded-full bg-slate-900 px-3 py-1 text-sm font-semibold text-white">
          {statusLabel(pool)}
        </div>
      </div>

      <div className="mt-5 grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard
          label="Replication"
          value={`x${pool.replication}`}
          subtext={`Min free: ${pool.min_free_gb} GB`}
        />
        <StatCard
          label="Branches"
          value={`${pool.online_branch_count}/${pool.branch_count}`}
          subtext="online"
        />
        <StatCard
          label="Drift"
          value={pool.drift.total}
          subtext={`${pool.drift.needs_repair} needs, ${pool.drift.repairing} repairing`}
        />
        <StatCard
          label="Jobs"
          value={pool.jobs.queued + pool.jobs.running}
          subtext={`${pool.jobs.queued} queued, ${pool.jobs.running} running`}
        />
      </div>

      <div className="mt-5 rounded-2xl bg-slate-50 p-4">
        <div className="mb-2 flex justify-between text-sm">
          <span className="font-semibold text-slate-700">Pool usage</span>
          <span className="text-slate-500">
            {pool.used_percent === null ? "—" : `${pool.used_percent}% used`}
          </span>
        </div>

        <ProgressBar percent={pool.used_percent} />

        <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-sm text-slate-600">
          <span>Used: {formatGb(pool.used_gb)}</span>
          <span>Free: {formatGb(pool.free_gb)}</span>
          <span>Total: {formatGb(pool.total_gb)}</span>
        </div>
      </div>

      <div className="mt-5">
        <button
          onClick={() => setShowBranches((current) => !current)}
          className="flex w-full items-center justify-between rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 text-left shadow-sm hover:bg-slate-100"
        >
          <div>
            <div className="text-sm font-bold text-slate-800">
              Branches
            </div>
            <div className="mt-1 text-xs text-slate-500">
              {pool.online_branch_count}/{pool.branch_count} online
            </div>
          </div>

          <div className="text-sm font-bold text-slate-700">
            {showBranches ? "Hide details ▲" : "Show details ▼"}
          </div>
        </button>

        {showBranches && (
          <div className="mt-3 overflow-hidden rounded-2xl border border-slate-200">
            <div className="divide-y divide-slate-200">
              {pool.branches.map((branch) => (
                <div
                  key={branch.path}
                  className="grid gap-3 px-4 py-3 md:grid-cols-[1fr_120px_120px_120px_90px] md:items-center"
                >
                  <div>
                    <div className="flex items-center gap-2">
                      <span
                        className={`h-2.5 w-2.5 rounded-full ${
                          branch.online ? "bg-green-500" : "bg-red-500"
                        }`}
                      />
                      <span className="break-all text-sm font-medium text-slate-800">
                        {branch.path}
                      </span>
                    </div>
                    <div className="mt-1 text-xs text-slate-500">
                      Updated: {formatDateTime(branch.updated_at)}
                    </div>
                  </div>

                  <div className="text-sm text-slate-600">
                    Size: {formatGb(branch.total_gb)}
                  </div>

                  <div className="text-sm text-slate-600">
                    Used: {formatGb(branch.used_gb)}
                  </div>

                  <div className="text-sm text-slate-600">
                    Free: {formatGb(branch.free_gb)}
                  </div>

                  <div className="text-sm font-semibold text-slate-700">
                    {branch.used_percent === null ? "—" : `${branch.used_percent}%`}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function ServiceStatusCard({ service }: { service: ServiceStatus | undefined }) {
  const isRunning = service?.active ?? false;

  return (
      <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="text-sm font-medium text-slate-500">Daemon</div>

        <div className="mt-1 flex items-center gap-2">
        <span
            className={`h-2.5 w-2.5 rounded-full ${
                isRunning ? "bg-green-500" : "bg-red-500"
            }`}
        />
          <span className="text-2xl font-bold text-slate-900">
          {isRunning ? "Running" : "Stopped"}
        </span>
        </div>

        <div className="mt-1 text-xs text-slate-500">
          {service?.sub_state ?? "unknown"} · PID: {service?.pid ?? "—"}
        </div>
      </div>
  );
}

const RhStorageStatusScreen: React.FC = () => {
  const [status, setStatus] = useState<RhStorageStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedPoolId, setSelectedPoolId] = useState<string>("all");

  const fetchStatus = async () => {
    try {
      const res = await fetch(`${API_BASE}/rh-storage/status`);
      const data = await res.json();

      if (!res.ok || !data.success) {
        throw new Error(data.detail || "RH-Storage status is unavailable.");
      }

      setStatus(data);
      setError(null);
    } catch (err) {
      console.error("Failed to fetch RH-Storage status:", err);
      setError("Failed to fetch RH-Storage status.");
    }
  };

  useEffect(() => {
    fetchStatus();

    const interval = setInterval(fetchStatus, 5000);
    return () => clearInterval(interval);
  }, []);

  const visiblePools = useMemo(() => {
    if (!status) {
      return [];
    }

    if (selectedPoolId === "all") {
      return status.pools;
    }

    return status.pools.filter((pool) => pool.pool_id === selectedPoolId);
  }, [status, selectedPoolId]);

  const totals = useMemo(() => {
    const pools = status?.pools ?? [];

    return {
      pools: pools.length,
      drift: pools.reduce((sum, pool) => sum + pool.drift.total, 0),
      queued: pools.reduce((sum, pool) => sum + pool.jobs.queued, 0),
      running: pools.reduce((sum, pool) => sum + pool.jobs.running, 0),
      failed: pools.reduce((sum, pool) => sum + pool.jobs.failed, 0),
    };
  }, [status]);

  return (
    <div className="min-h-screen bg-slate-100 p-4 font-sans text-slate-900 md:p-8">
      <div className="mx-auto max-w-7xl">
        <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
          <div>
            <div className="text-sm font-bold uppercase tracking-wide text-blue-600">
              RemiHub Storage
            </div>
            <h1 className="mt-1 text-3xl font-black text-slate-950">
              RH-Storage Status
            </h1>
            <p className="mt-2 max-w-2xl text-sm text-slate-600">
              Pool health, replication drift, queued repair jobs, and branch usage.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <select
              value={selectedPoolId}
              onChange={(event) => setSelectedPoolId(event.target.value)}
              className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm font-medium shadow-sm"
            >
              <option value="all">All pools</option>
              {status?.pools.map((pool) => (
                <option key={pool.pool_id} value={pool.pool_id}>
                  {pool.name}
                </option>
              ))}
            </select>

            <button
              onClick={fetchStatus}
              className="rounded-xl bg-slate-900 px-4 py-2 text-sm font-bold text-white shadow-sm hover:bg-slate-700"
            >
              Refresh
            </button>
          </div>
        </div>

        <div className="mb-5 text-sm text-slate-500">
          Last updated: {status ? formatDateTime(status.updated_at) : "Loading..."}
        </div>
        {error && (
          <div className="mb-5 rounded-2xl border border-red-200 bg-red-50 p-4 text-sm font-semibold text-red-700">
            {error}
          </div>
        )}

        <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-6">
          <ServiceStatusCard service={status?.service} />
          <StatCard label="Pools" value={totals.pools} />
          <StatCard label="Total drift" value={totals.drift} />
          <StatCard label="Queued jobs" value={totals.queued} />
          <StatCard label="Running jobs" value={totals.running} />
          <StatCard label="Failed jobs" value={totals.failed} />
        </div>

        <div className="grid gap-5">
          {visiblePools.map((pool) => (
            <PoolCard key={pool.pool_id} pool={pool} />
          ))}
        </div>

        {status && status.recent_jobs.length > 0 && (
          <div className="mt-6 overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm">
            <div className="bg-slate-50 px-5 py-4">
              <h2 className="text-lg font-bold text-slate-900">Recent Jobs</h2>
            </div>

            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-white text-left text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-5 py-3">Pool</th>
                    <th className="px-5 py-3">Status</th>
                    <th className="px-5 py-3">Attempts</th>
                    <th className="px-5 py-3">Path</th>
                    <th className="px-5 py-3">Updated</th>
                  </tr>
                </thead>

                <tbody className="divide-y divide-slate-100">
                  {status.recent_jobs.map((job) => (
                    <tr key={job.job_id}>
                      <td className="whitespace-nowrap px-5 py-3 font-semibold text-slate-700">
                        {job.pool_id}
                      </td>
                      <td className="whitespace-nowrap px-5 py-3">
                        <span className="rounded-full bg-slate-100 px-2 py-1 text-xs font-bold text-slate-700">
                          {job.status}
                        </span>
                      </td>
                      <td className="whitespace-nowrap px-5 py-3 text-slate-600">
                        {job.attempts}
                      </td>
                      <td className="max-w-xl truncate px-5 py-3 text-slate-600">
                        {job.rel_path ?? "—"}
                      </td>
                      <td className="whitespace-nowrap px-5 py-3 text-slate-500">
                        {formatDateTime(job.updated_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default RhStorageStatusScreen;
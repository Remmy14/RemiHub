import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { rhStorageWorkersApi } from "./api/rhStorageWorkers";
import type { StorageWorker, WorkerConfiguration, WorkerMonitoring } from "./api/rhStorageWorkers";
import {
  boostRemainingSeconds, fileName, formatBytes, formatDuration,
  formatTimestamp, progressPercent, validWorkerCount,
} from "./rhStorageWorkerView";

type ConfigurationSnapshot = { value: WorkerConfiguration; receivedAt: number };
type DurationMinutes = 15 | 60 | 120 | 240;
const durations: DurationMinutes[] = [15, 60, 120, 240];

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "Request failed. Please try again.";
}

function Metric({ label, value, note }: { label: string; value: number | string; note?: string }) {
  return <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
    <div className="text-xs font-medium text-slate-500">{label}</div>
    <div className="mt-1 text-xl font-bold text-slate-900">{value}</div>
    {note && <div className="text-xs text-slate-500">{note}</div>}
  </div>;
}

function PathDetail({ label, path }: { label: string; path: string | null }) {
  const [copyResult, setCopyResult] = useState("");
  return <div className="min-w-0">
    <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</div>
    {path ? <div className="mt-1 flex min-w-0 items-start gap-2">
      <code className="min-w-0 flex-1 select-text break-all rounded bg-slate-100 px-2 py-1 text-xs text-slate-800" title={path}>{path}</code>
      <button type="button" className="shrink-0 rounded border border-slate-300 px-2 py-1 text-xs font-semibold hover:bg-slate-100"
        onClick={() => {
          if (!navigator.clipboard) { setCopyResult("Select path to copy"); return; }
          void navigator.clipboard.writeText(path).then(() => setCopyResult("Copied"), () => setCopyResult("Select path to copy"));
        }}>
        Copy
      </button>
    </div> : <div className="mt-1 text-sm text-slate-500">Unavailable</div>}
    {copyResult && <div role="status" className="text-xs text-slate-500">{copyResult}</div>}
  </div>;
}

function Detail({ label, value }: { label: string; value: string | number | null }) {
  return <div className="min-w-0 text-sm"><span className="font-semibold text-slate-600">{label}: </span>
    <span className="break-all text-slate-800">{value ?? "Unavailable"}</span></div>;
}

function WorkerCard({ worker }: { worker: StorageWorker }) {
  const percent = progressPercent(worker);
  const hasJob = Boolean(worker.job_id);
  return <details className="min-w-0 rounded-2xl border border-slate-200 bg-white p-4">
    <summary className="cursor-pointer list-inside text-sm font-semibold text-slate-900">
      <span className="break-all">{worker.worker_id}</span>
      <span className="ml-2 rounded-full bg-slate-100 px-2 py-1 text-xs capitalize text-slate-700">{worker.state}</span>
      {worker.retiring && <span className="ml-2 rounded-full bg-amber-100 px-2 py-1 text-xs text-amber-900">Retiring</span>}
      <span className="ml-2 font-normal text-slate-600">{hasJob ? fileName(worker.rel_path) : "No current job"}</span>
    </summary>
    <div className="mt-4 grid min-w-0 gap-4 border-t border-slate-100 pt-4 md:grid-cols-2">
      <Detail label="Worker state" value={worker.state} />
      <Detail label="Host / PID" value={worker.hostname ? `${worker.hostname} / ${worker.pid ?? "—"}` : null} />
      <Detail label="Job ID" value={worker.job_id} />
      <Detail label="Job type" value={worker.job_type} />
      <Detail label="Pool" value={worker.pool_name ?? worker.pool_id} />
      <PathDetail label="Logical mountpoint" path={worker.pool_mountpoint} />
      <Detail label="File name" value={hasJob ? fileName(worker.rel_path) : null} />
      <PathDetail label="Relative file path" path={worker.rel_path} />
      <PathDetail label="Physical source branch / disk" path={worker.source_branch_path} />
      <PathDetail label="Physical destination branch / disk" path={worker.target_branch_path} />
      <Detail label="Source branch ID" value={worker.source_branch_id} />
      <Detail label="Destination branch ID" value={worker.target_branch_id} />
      {worker.source_branch_registered_path && worker.source_branch_registered_path !== worker.source_branch_path &&
        <PathDetail label="Registered source branch" path={worker.source_branch_registered_path} />}
      {worker.target_branch_registered_path && worker.target_branch_registered_path !== worker.target_branch_path &&
        <PathDetail label="Registered destination branch" path={worker.target_branch_registered_path} />}
      <Detail label="Job elapsed" value={hasJob ? formatDuration(worker.job_elapsed_seconds) : null} />
      <Detail label="Latest progress age" value={hasJob ? formatDuration(worker.last_progress_age_seconds) : null} />
      <Detail label="Heartbeat freshness" value={worker.heartbeat_age_seconds === null ? "Unavailable" : `${formatDuration(worker.heartbeat_age_seconds)} ago (fresh at snapshot)`} />
      <Detail label="Last heartbeat" value={formatTimestamp(worker.last_seen_at)} />
      {worker.last_error && <div className="min-w-0 break-words rounded-lg bg-amber-50 p-2 text-sm text-amber-900 md:col-span-2">
        <span className="font-semibold">Reported error (may be historical): </span>{worker.last_error}
      </div>}
      <div className="min-w-0 md:col-span-2">
        <div className="mb-1 text-sm font-semibold text-slate-700">Transfer progress: {percent === null ? "Unavailable" : `${percent.toFixed(1)}%`}</div>
        <div role="progressbar" aria-label={`Transfer progress for ${worker.worker_id}`}
          aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined}
          className="h-3 overflow-hidden rounded-full bg-slate-200">
          <div className="h-full rounded-full bg-blue-600 transition-all duration-500" style={{ width: `${percent ?? 0}%` }} />
        </div>
        <div className="mt-1 text-xs text-slate-600">{formatBytes(worker.bytes_copied)} / {formatBytes(worker.total_bytes)}</div>
      </div>
    </div>
  </details>;
}

export default function WorkerManagement({ refreshToken }: { refreshToken: number }) {
  const [configuration, setConfiguration] = useState<ConfigurationSnapshot | null>(null);
  const [monitoring, setMonitoring] = useState<WorkerMonitoring | null>(null);
  const [monitorError, setMonitorError] = useState<string | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [role, setRole] = useState<string | null>(null);
  const [roleError, setRoleError] = useState(false);
  const [defaultDraft, setDefaultDraft] = useState<string | null>(null);
  const [boostDraft, setBoostDraft] = useState<string | null>(null);
  const [duration, setDuration] = useState<DurationMinutes>(120);
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [clock, setClock] = useState(Date.now());
  const mounted = useRef(false);
  const pendingRequest = useRef(false);
  const monitorRequest = useRef<Promise<void> | null>(null);

  const loadMonitoring = useCallback((): Promise<void> => {
    if (monitorRequest.current) return monitorRequest.current;
    const request = rhStorageWorkersApi.monitoring().then((value) => {
      if (!mounted.current) return;
      setMonitoring(value);
      setConfiguration((current) => !current || Date.parse(value.configuration.server_now) >= Date.parse(current.value.server_now)
        ? { value: value.configuration, receivedAt: Date.now() } : current);
      setMonitorError(null);
      setConfigError(null);
    }).catch((error: unknown) => {
      if (mounted.current) setMonitorError(errorText(error));
    }).finally(() => { monitorRequest.current = null; });
    monitorRequest.current = request;
    return request;
  }, []);

  useEffect(() => {
    mounted.current = true;
    void loadMonitoring();
    void rhStorageWorkersApi.configuration().then((value) => {
      if (mounted.current) {
        setConfiguration((current) => !current || Date.parse(value.server_now) >= Date.parse(current.value.server_now)
          ? { value, receivedAt: Date.now() } : current);
        setConfigError(null);
      }
    }).catch((error: unknown) => { if (mounted.current) setConfigError(errorText(error)); });
    void rhStorageWorkersApi.role().then((value) => {
      if (mounted.current) setRole(value.role);
    }).catch(() => { if (mounted.current) setRoleError(true); });
    const poll = window.setInterval(() => { void loadMonitoring(); }, 12000);
    const tick = window.setInterval(() => setClock(Date.now()), 1000);
    return () => { mounted.current = false; window.clearInterval(poll); window.clearInterval(tick); };
  }, [loadMonitoring]);

  useEffect(() => { if (refreshToken > 0) void loadMonitoring(); }, [refreshToken, loadMonitoring]);

  const config = configuration?.value;
  const remaining = config && configuration ? boostRemainingSeconds(config, configuration.receivedAt, clock) : null;
  const boostActive = Boolean(config?.boost_active && remaining !== null && remaining > 0);
  const effectiveTarget = config ? boostActive ? config.boost_workers ?? config.effective_workers : config.default_workers : null;

  async function mutate(action: () => Promise<WorkerConfiguration>, message: string): Promise<boolean> {
    if (pendingRequest.current || role !== "admin") return false;
    pendingRequest.current = true;
    setPending(true);
    setFeedback(null);
    setActionError(null);
    try {
      if (monitorRequest.current) await monitorRequest.current;
      const updated = await action();
      if (!mounted.current) return false;
      setConfiguration({ value: updated, receivedAt: Date.now() });
      setFeedback(`${message} Worker observations will update when monitoring confirms them.`);
      await loadMonitoring();
      return true;
    } catch (error) {
      if (mounted.current) setActionError(errorText(error));
      return false;
    } finally {
      pendingRequest.current = false;
      if (mounted.current) setPending(false);
    }
  }

  function submitDefault(event: FormEvent) {
    event.preventDefault();
    const count = validWorkerCount(defaultDraft ?? String(config?.default_workers ?? ""));
    if (count === null) { setActionError("Enter a whole worker count from 1 to 16."); return; }
    void mutate(() => rhStorageWorkersApi.setDefault(count), "Default target updated.").then((saved) => {
      if (saved && mounted.current) setDefaultDraft(null);
    });
  }

  function submitBoost(event: FormEvent) {
    event.preventDefault();
    const count = validWorkerCount(boostDraft ?? String(config?.boost_workers ?? config?.effective_workers ?? ""));
    if (count === null) { setActionError("Enter a whole worker count from 1 to 16."); return; }
    void mutate(() => rhStorageWorkersApi.boost(count, duration), "Temporary boost configured.");
  }

  const counts = monitoring?.counts;
  return <section aria-labelledby="worker-management-title" className="mb-6 min-w-0 rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
    <h2 id="worker-management-title" className="text-xl font-bold text-slate-900">Worker Management</h2>
    <p className="mt-1 text-sm text-slate-600">Requested concurrency and observed worker heartbeats are shown separately.</p>
    {configError && <p role="alert" className="mt-3 rounded-lg bg-amber-50 p-3 text-sm text-amber-900">Configuration refresh failed: {configError}</p>}
    {monitorError && <p role="alert" className="mt-3 rounded-lg bg-red-50 p-3 text-sm text-red-800">Worker monitoring failed: {monitorError}. Last available observations are shown below.</p>}
    <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-7">
      <Metric label="Default target" value={config?.default_workers ?? "—"} note="requested" />
      <Metric label="Effective target" value={effectiveTarget ?? "—"} note="requested" />
      <Metric label="Active" value={counts?.active ?? "—"} note="observed" />
      <Metric label="Busy" value={counts?.busy ?? "—"} note="observed" />
      <Metric label="Idle" value={counts?.idle ?? "—"} note="observed" />
      <Metric label="Retiring" value={counts?.retiring ?? "—"} note="observed; not unhealthy by itself" />
      <Metric label="Healthy / unhealthy" value={counts ? `${counts.healthy} / ${counts.unhealthy}` : "—"} note="observed" />
    </div>
    <div className="mt-3 text-sm text-slate-600">
      {monitoring?.monitoring_status === "ambiguous" ? "Monitoring is ambiguous: multiple daemon processes have equally recent heartbeats. Observed counts are unavailable."
        : monitoring?.monitoring_status === "no_recent_heartbeats" ? "No recent worker heartbeats (within 45 seconds). Workers may be missing or stale."
          : monitoring?.monitoring_status === "available" ? `Observed target shortfall: ${counts?.observed_gap ?? "—"}. This is not a count of identified missing workers.`
            : "Loading worker monitoring…"}
      {monitoring?.observed_hostname && <span className="ml-2">Daemon: {monitoring.observed_hostname} / PID {monitoring.observed_daemon_pid ?? "—"}.</span>}
    </div>
    <div className="mt-3 rounded-xl bg-blue-50 p-3 text-sm text-blue-950">
      <span className="font-semibold">Boost: </span>{boostActive ? `${config?.boost_workers} workers until ${formatTimestamp(config?.boost_until ?? null)} (${formatDuration(remaining)} remaining)` : "Inactive"}
      {!boostActive && config?.boost_until && <span> (last stored expiry: {formatTimestamp(config.boost_until)})</span>}
    </div>

    {roleError && <p className="mt-3 text-sm text-amber-800">Administrator access could not be checked; scaling controls are unavailable.</p>}
    {role === "admin" && <div className="mt-5 border-t border-slate-200 pt-5">
      <h3 className="text-lg font-bold text-slate-900">Scaling controls</h3>
      <p className="text-sm text-slate-600">Changes update requested targets. Worker starts and retirements appear in monitoring after the daemon reports them.</p>
      <div className="mt-3 flex flex-wrap items-end gap-4">
        <form onSubmit={submitDefault} className="flex flex-wrap items-end gap-2">
          <label className="text-sm font-semibold text-slate-700">Default workers (1–16)
            <input type="number" min={1} max={16} step={1} required value={defaultDraft ?? config?.default_workers ?? ""}
              onChange={(event) => setDefaultDraft(event.target.value)} className="mt-1 block w-28 rounded-lg border border-slate-300 px-3 py-2" />
          </label>
          <button disabled={pending || !config} className="rounded-lg bg-slate-900 px-3 py-2 text-sm font-bold text-white disabled:bg-slate-400">Save default</button>
        </form>
        <form onSubmit={submitBoost} className="flex flex-wrap items-end gap-2">
          <label className="text-sm font-semibold text-slate-700">Boost workers (1–16)
            <input type="number" min={1} max={16} step={1} required value={boostDraft ?? config?.boost_workers ?? config?.effective_workers ?? ""}
              onChange={(event) => setBoostDraft(event.target.value)} className="mt-1 block w-28 rounded-lg border border-slate-300 px-3 py-2" />
          </label>
          <label className="text-sm font-semibold text-slate-700">Duration
            <select value={duration} onChange={(event) => setDuration(Number(event.target.value) as DurationMinutes)} className="mt-1 block rounded-lg border border-slate-300 px-3 py-2">
              {durations.map((minutes) => <option key={minutes} value={minutes}>{minutes} minutes</option>)}
            </select>
          </label>
          <button disabled={pending || !config} className="rounded-lg bg-blue-700 px-3 py-2 text-sm font-bold text-white disabled:bg-slate-400">Start boost</button>
        </form>
        <button type="button" disabled={pending || !config} onClick={() => { void mutate(() => rhStorageWorkersApi.boost(5, 120), "Quick boost configured."); }}
          className="rounded-lg border border-blue-700 px-3 py-2 text-sm font-bold text-blue-800 disabled:opacity-50">Boost to 5 for 2 hours</button>
        <button type="button" disabled={pending || !boostActive} onClick={() => { void mutate(rhStorageWorkersApi.clearBoost, "Boost cleared; default target restored."); }}
          className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-bold text-slate-800 disabled:opacity-50">Clear boost</button>
      </div>
      {pending && <p role="status" className="mt-3 text-sm text-slate-600">Updating configuration…</p>}
      {feedback && <p role="status" className="mt-3 text-sm text-green-800">{feedback}</p>}
      {actionError && <p role="alert" className="mt-3 text-sm text-red-700">{actionError}</p>}
    </div>}

    <div className="mt-6 border-t border-slate-200 pt-5">
      <h3 className="text-lg font-bold text-slate-900">Individual workers</h3>
      {monitoring?.monitoring_status === "available" && monitoring.workers.length > 0
        ? <div className="mt-3 grid min-w-0 gap-3">{monitoring.workers.map((worker) => <WorkerCard key={worker.worker_id} worker={worker} />)}</div>
        : <p className="mt-2 text-sm text-slate-600">{monitoring?.monitoring_status === "ambiguous" ? "Worker details are unavailable until the daemon process can be identified." : "No current worker heartbeats to display."}</p>}
    </div>
  </section>;
}

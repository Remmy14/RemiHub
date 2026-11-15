import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getCodexConfiguration, getCodexExecutions, getCodexQuota, getCodexSummary,
} from "./api/agentApi";
import type {
  CodexExecution, CodexQuota, CodexStageConfiguration, CodexSummary,
  CodexTokenAggregate, CodexUsageWindow,
} from "./api/agentApi";

const number = new Intl.NumberFormat();
const QUOTA_REFRESH_MS = 300_000;
const CONFIG_REFRESH_MS = 120_000;
const MAX_TREND_RECORDS = 1_000;
const MAX_INITIAL_CARD_RECORDS = 1_000;
const STAGES = ["planning", "implementation", "review"];

function formatNumber(value: number | null | undefined): string {
  return value == null ? "Unavailable" : number.format(value);
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unavailable" : date.toLocaleString();
}

function age(value: string | null | undefined, now: number): string {
  if (!value) return "unknown age";
  const seconds = Math.max(0, Math.floor((now - new Date(value).getTime()) / 1000));
  if (!Number.isFinite(seconds)) return "unknown age";
  if (seconds < 60) return `${seconds} seconds ago`;
  if (seconds < 3_600) return `${Math.floor(seconds / 60)} minutes ago`;
  return `${Math.floor(seconds / 3_600)} hours ago`;
}

function countdown(value: string | null, now: number): string {
  if (!value) return "Reset time unavailable";
  const remaining = new Date(value).getTime() - now;
  if (!Number.isFinite(remaining)) return "Reset time unavailable";
  if (remaining <= 0) return "Reset time reached; awaiting refreshed quota data";
  const minutes = Math.ceil(remaining / 60_000);
  const days = Math.floor(minutes / 1_440);
  const hours = Math.floor((minutes % 1_440) / 60);
  const mins = minutes % 60;
  return `Resets in ${days ? `${days}d ` : ""}${hours ? `${hours}h ` : ""}${mins}m`;
}

function label(value: string): string {
  return value.replace(/[_-]/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function windowLabel(window: CodexUsageWindow): string {
  if (window.duration_minutes === 300) return "Five-hour window";
  if (window.duration_minutes === 10_080) return "Weekly window";
  return label(window.name);
}

function tokenMetrics(aggregate: CodexTokenAggregate) {
  return [
    ["Total", aggregate.total_tokens], ["Input", aggregate.input_tokens],
    ["Output", aggregate.output_tokens], ["Cached input", aggregate.cached_input_tokens],
  ] as const;
}

function QuotaWindow({ window, now }: { window: CodexUsageWindow; now: number }) {
  const available = window.status === "available" && window.used_percent != null;
  const used = available ? Math.max(0, Math.min(100, window.used_percent!)) : 0;
  return <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
    <div className="flex items-center justify-between gap-2 text-sm font-bold text-slate-900">
      <span>{windowLabel(window)}</span><span>{available ? `${used}% used` : "Unavailable"}</span>
    </div>
    {available && <>
      <div role="progressbar" aria-label={`${windowLabel(window)} quota used`} aria-valuenow={used}
        aria-valuemin={0} aria-valuemax={100} className="mt-2 h-2.5 overflow-hidden rounded-full bg-slate-200">
        <div className={`h-full rounded-full ${used >= 90 ? "bg-amber-600" : "bg-blue-600"}`}
          style={{ width: `${used}%` }} />
      </div>
      <p className="mt-2 text-xs text-slate-600">{window.remaining_percent == null ? "Remaining percentage unavailable" : `${number.format(window.remaining_percent)}% remaining`}</p>
    </>}
    <p className="mt-2 text-xs text-slate-600">{countdown(window.reset_at, now)}</p>
    {window.reset_at && <p className="text-xs text-slate-500">{formatDate(window.reset_at)}</p>}
  </div>;
}

function QuotaPanel({ quota, error, loading, now }: {
  quota: CodexQuota | null; error: string | null; loading: boolean; now: number;
}) {
  const collectedTime = quota?.collected_at ? new Date(quota.collected_at).getTime() : NaN;
  const clientStale = quota?.status === "available" && (!Number.isFinite(collectedTime) || now - collectedTime > 900_000);
  const warning = clientStale || quota?.status === "stale" || quota?.status === "collection_failed" || !!error;
  return <section aria-label="Codex account quota" className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h3 className="text-lg font-black text-slate-950">Account quota</h3>
      <span className="text-xs text-slate-500">Collector interval: five minutes</span>
    </div>
    {loading && !quota && <p role="status" className="mt-3 text-sm text-slate-600">Loading quota…</p>}
    {warning && <p role="alert" className="mt-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm font-semibold text-amber-900">
      {error ? `Quota refresh failed: ${error}. Showing previously loaded data where available.` :
        quota?.status === "collection_failed" ? "The latest quota collection failed. Any values below are historical." :
          "Quota data is stale. Values below are historical."}
      {quota?.collected_at && ` Last successful collection: ${formatDate(quota.collected_at)} (${age(quota.collected_at, now)}).`}
    </p>}
    {quota && <>
      {!warning && <p className="mt-2 text-xs text-slate-600">Last successful collection: {formatDate(quota.collected_at)} ({age(quota.collected_at, now)})</p>}
      {quota.last_attempt_at && <p className="text-xs text-slate-500">Last attempted: {formatDate(quota.last_attempt_at)}</p>}
      {quota.status === "unavailable" && <p className="mt-3 text-sm text-slate-600">Quota information is unavailable{quota.error_code ? ` (${quota.error_code})` : ""}.</p>}
      {quota.buckets.length === 0 && quota.status !== "unavailable" && <p className="mt-3 text-sm text-slate-600">No quota windows were reported.</p>}
      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        {quota.buckets.map((bucket, index) => <div key={`${bucket.limit_id ?? "limit"}-${index}`} className="rounded-lg border border-slate-200 p-3">
          <h4 className="font-bold text-slate-900">{bucket.limit_name || bucket.limit_id || "Codex limit"}</h4>
          <div className="mt-1 text-xs text-slate-600">Plan: {bucket.plan_type || "Unavailable"}{bucket.model_slug && ` · ${bucket.model_slug}`}</div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">{bucket.windows.map((window, windowIndex) =>
            <QuotaWindow key={`${window.name}-${windowIndex}`} window={window} now={now} />)}</div>
          {bucket.windows.length === 0 && <p className="mt-3 text-sm text-slate-600">No windows available for this limit.</p>}
          {bucket.credits && <p className="mt-2 text-xs text-slate-600">Additional usage: {bucket.credits.balance != null ? `balance ${bucket.credits.balance}` :
            bucket.credits.unlimited === true ? "unlimited" : bucket.credits.has_credits === true ? "available" :
              bucket.credits.has_credits === false ? "none reported" : "unavailable"}</p>}
        </div>)}
      </div>
      <div className="mt-3 flex flex-wrap gap-4 text-xs text-slate-600">
        {quota.available_reset_credits != null && <span>Available reset credits: {formatNumber(quota.available_reset_credits)}</span>}
        {quota.ordinary_usage_allowed != null && <span>Ordinary usage: {quota.ordinary_usage_allowed ? "allowed" : "not allowed"}</span>}
      </div>
      {quota.error_code && quota.status !== "unavailable" && <p className="mt-2 text-xs text-amber-800">Collector error: {quota.error_code}</p>}
    </>}
    {!quota && !loading && <p className="mt-3 text-sm text-slate-600">Quota information is unavailable.</p>}
  </section>;
}

function AggregateCard({ title, value }: { title: string; value: CodexTokenAggregate | null }) {
  return <div className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
    <h4 className="text-sm font-bold text-slate-900">{title}</h4>
    <p className="mt-1 text-xl font-black tabular-nums text-slate-950">{value ? formatNumber(value.total_tokens) : "Unavailable"}</p>
    <p className="text-xs text-slate-600">{value ? `${number.format(value.execution_count)} executions · ${number.format(value.unavailable_count)} without usage` : "No telemetry reported"}</p>
    {value && <dl className="mt-2 grid grid-cols-2 gap-2 text-xs">{tokenMetrics(value).slice(1).map(([name, count]) =>
      <div key={name}><dt className="text-slate-500">{name}</dt><dd className="font-semibold tabular-nums text-slate-800">{formatNumber(count)}</dd></div>)}</dl>}
  </div>;
}

function ConfigPanel({ items, error, loading }: { items: CodexStageConfiguration[] | null; error: string | null; loading: boolean }) {
  const stages = items?.filter((item) => item.stage !== "deployment") ?? [];
  return <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
    <h3 className="text-lg font-black text-slate-950">Model configuration</h3>
    <p className="mt-1 text-xs text-slate-600">Worker-reported settings and last verified execution models. Backend and Android implementation share one API stage; separate current settings are unavailable.</p>
    {error && <p role="alert" className="mt-2 text-sm text-amber-800">Configuration refresh failed: {error}. Previous values may be old.</p>}
    {loading && !items && <p role="status" className="mt-3 text-sm">Loading configuration…</p>}
    <div className="mt-3 grid gap-3 md:grid-cols-3">{stages.map((item) => <div key={item.stage} className="min-w-0 rounded-lg bg-slate-50 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="font-bold text-slate-900">{label(item.stage)}</h4><span className="text-xs font-semibold text-slate-600">{label(item.status)}</span></div>
      <dl className="mt-2 space-y-2 text-xs">
        <div><dt className="text-slate-500">{item.status === "stale" ? "Last reported model" : "Configured model"}</dt><dd className="break-all font-semibold text-slate-900">{item.status === "stale" ? item.last_reported_configured_model || "Unavailable" : item.configured_model || "Unavailable"}</dd></div>
        <div><dt className="text-slate-500">Reasoning effort</dt><dd className="font-semibold text-slate-900">{(item.status === "stale" ? item.last_reported_reasoning_effort : item.configured_reasoning_effort) || "Unavailable"}</dd></div>
        <div><dt className="text-slate-500">Last actual model</dt><dd className="break-all font-semibold text-slate-900">{item.actual_model_last_execution || "Unavailable"}</dd></div>
        <div><dt className="text-slate-500">Last actual reasoning</dt><dd className="font-semibold text-slate-900">{item.reasoning_effort_last_execution || "Unavailable"}</dd></div>
        <div><dt className="text-slate-500">Settings reported</dt><dd>{formatDate(item.configuration_reported_at)}</dd></div>
        <div><dt className="text-slate-500">Actual model observed</dt><dd>{formatDate(item.actual_model_observed_at)}</dd></div>
      </dl>
    </div>)}</div>
    {items && stages.length === 0 && <p className="mt-3 text-sm text-slate-600">No worker configuration is available.</p>}
  </section>;
}

function Trend({ records, total, periodDays }: { records: CodexExecution[]; total: number; periodDays: number }) {
  const [stage, setStage] = useState("all");
  const groups = new Map<string, number>();
  for (const row of records) {
    if (stage !== "all" && row.stage !== stage) continue;
    if (row.usage_status !== "available" || row.total_tokens == null) continue;
    const date = new Date(row.recorded_at);
    if (Number.isNaN(date.getTime())) continue;
    const key = periodDays <= 30 ? date.toISOString().slice(0, 10) :
      periodDays <= 90 ? new Date(date.getTime() - date.getUTCDay() * 86_400_000).toISOString().slice(0, 10) :
        `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}`;
    groups.set(key, (groups.get(key) ?? 0) + row.total_tokens);
  }
  const points = [...groups].sort(([a], [b]) => a.localeCompare(b));
  const max = Math.max(1, ...points.map(([, value]) => value));
  return <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
    <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-sm font-bold text-slate-900">Token trend</h3>
      <label className="text-xs font-semibold text-slate-700">Stage <select value={stage} onChange={(event) => setStage(event.target.value)} className="ml-1 rounded border border-slate-300 bg-white px-2 py-1">
        <option value="all">All stages</option>{STAGES.map((value) => <option key={value} value={value}>{label(value)}</option>)}
      </select></label></div>
    {total > records.length && <p className="mt-1 text-xs font-semibold text-amber-800">Partial trend: showing {number.format(records.length)} of {number.format(total)} executions.</p>}
    {points.length === 0 ? <p className="mt-2 text-sm text-slate-600">No reported token usage in loaded executions.</p> :
      <div className="mt-3 flex h-24 items-end gap-1 overflow-x-auto" role="img" aria-label={`Token usage trend: ${points.map(([key, value]) => `${key}, ${number.format(value)} tokens`).join("; ")}`}>
        {points.map(([key, value]) => <div key={key} title={`${key}: ${number.format(value)} tokens`}
          className="min-w-2 flex-1 rounded-t bg-blue-500" style={{ height: `${Math.max(4, value / max * 100)}%` }} />)}
      </div>}
    <p className="mt-2 text-xs text-slate-500">{periodDays <= 30 ? "Daily" : periodDays <= 90 ? "Weekly" : "Monthly"} totals from reported turns. Cached input is included in input tokens.</p>
  </div>;
}

export function CodexUsageDashboard({ onSummary, summaryRefresh }: {
  onSummary: (summary: CodexSummary, refreshVersion: number) => void;
  summaryRefresh: number;
}) {
  const [days, setDays] = useState(30);
  const [now, setNow] = useState(Date.now());
  const [quota, setQuota] = useState<CodexQuota | null>(null);
  const [configuration, setConfiguration] = useState<CodexStageConfiguration[] | null>(null);
  const [summary, setSummary] = useState<CodexSummary | null>(null);
  const [summaryDays, setSummaryDays] = useState<number | null>(null);
  const [trend, setTrend] = useState<{ items: CodexExecution[]; total: number; days: number } | null>(null);
  const [errors, setErrors] = useState<Record<string, string | null>>({});
  const [loading, setLoading] = useState<Record<string, boolean>>({});
  const [reload, setReload] = useState(0);
  const [quotaReload, setQuotaReload] = useState(0);
  const [configReload, setConfigReload] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    const timer = window.setInterval(() => setQuotaReload((value) => value + 1), QUOTA_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    const timer = window.setInterval(() => setConfigReload((value) => value + 1), CONFIG_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    let active = true;
    setLoading((old) => ({ ...old, quota: true }));
    getCodexQuota().then((data) => { if (active) { setQuota(data); setErrors((old) => ({ ...old, quota: null })); } })
      .catch((error: unknown) => { if (active) setErrors((old) => ({ ...old, quota: error instanceof Error ? error.message : "Request failed" })); })
      .finally(() => { if (active) setLoading((old) => ({ ...old, quota: false })); });
    return () => { active = false; };
  }, [quotaReload, reload]);
  useEffect(() => {
    let active = true;
    setLoading((old) => ({ ...old, configuration: true }));
    getCodexConfiguration().then((data) => { if (active) { setConfiguration(data); setErrors((old) => ({ ...old, configuration: null })); } })
      .catch((error: unknown) => { if (active) setErrors((old) => ({ ...old, configuration: error instanceof Error ? error.message : "Request failed" })); })
      .finally(() => { if (active) setLoading((old) => ({ ...old, configuration: false })); });
    return () => { active = false; };
  }, [configReload, reload]);
  useEffect(() => {
    let active = true;
    const to = new Date();
    const from = new Date(to.getTime() - days * 86_400_000);
    const fromTime = from.toISOString();
    const toTime = to.toISOString();
    setLoading((old) => ({ ...old, summary: true }));
    getCodexSummary(fromTime, toTime).then((data) => {
      if (active) { setSummary(data); setSummaryDays(days); onSummary(data, summaryRefresh); setErrors((old) => ({ ...old, summary: null })); }
    }).catch((error: unknown) => { if (active) setErrors((old) => ({ ...old, summary: error instanceof Error ? error.message : "Request failed" })); })
      .finally(() => { if (active) setLoading((old) => ({ ...old, summary: false })); });
    // Trend data is read separately; summary totals remain the authoritative totals.
    async function loadTrend() {
      try {
        const rows: CodexExecution[] = [];
        let total = 0;
        while (rows.length < MAX_TREND_RECORDS) {
          const page = await getCodexExecutions(fromTime, toTime, undefined, rows.length, 100);
          total = page.total;
          rows.push(...page.items);
          if (page.items.length === 0 || rows.length >= total) break;
        }
        if (active) { setTrend({ items: rows, total, days }); setErrors((old) => ({ ...old, trend: null })); }
      } catch (error) {
        if (active) setErrors((old) => ({ ...old, trend: error instanceof Error ? error.message : "Request failed" }));
      }
    }
    void loadTrend();
    return () => { active = false; };
  }, [days, reload, summaryRefresh, onSummary]);

  const stages = useMemo(() => summary?.token_accounting.by_stage ?? [], [summary]);
  return <section aria-label="Codex Usage" className="mb-6 space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="text-2xl font-black text-slate-950">Codex Usage</h2><p className="text-sm text-slate-600">Account quota and reported Agent execution tokens</p></div>
      <div className="flex items-center gap-2"><label htmlFor="codex-period" className="text-sm font-semibold text-slate-700">Period</label>
        <select id="codex-period" value={days} onChange={(event) => setDays(Number(event.target.value))}
          className="rounded-md border border-slate-300 bg-white px-2 py-2 text-sm">
          {[7, 30, 90, 365].map((value) => <option key={value} value={value}>Last {value} days</option>)}
        </select>
        <button type="button" onClick={() => setReload((value) => value + 1)}
          disabled={!!loading.quota || !!loading.configuration || !!loading.summary}
          className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-bold text-slate-700 hover:bg-slate-100 disabled:opacity-50">Refresh usage</button>
      </div>
    </div>
    <QuotaPanel quota={quota} error={errors.quota ?? null} loading={!!loading.quota} now={now} />
    <div>
      <div className="mb-2 flex flex-wrap justify-between gap-2"><h3 className="text-lg font-black text-slate-950">Execution tokens by stage</h3>
        <span className="text-xs text-slate-600">{summary ? `${formatDate(summary.period_start)} – ${formatDate(summary.period_end)}` : `Last ${days} days`}</span></div>
      {errors.summary && <p role="alert" className="mb-2 text-sm text-amber-800">Summary refresh failed: {errors.summary}. Previous period data may be old.</p>}
      {summary && summaryDays !== days && <p className="mb-2 text-sm font-semibold text-amber-800">Showing the previous period until the selected period loads.</p>}
      {loading.summary && !summary && <p role="status" className="text-sm text-slate-600">Loading token summary…</p>}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <AggregateCard title="All stages" value={summary?.token_accounting.overall ?? null} />
        {STAGES.map((stage) => <AggregateCard key={stage} title={label(stage)} value={stages.find((item) => item.stage === stage) ?? null} />)}
        {stages.filter((item) => !STAGES.includes(item.stage ?? "")).map((item) =>
          <AggregateCard key={item.stage} title={label(item.stage ?? "Other")} value={item} />)}
      </div>
      <p className="mt-2 text-xs text-slate-600">Totals cover reported turn usage in this period, including retries. Unavailable executions are counted but contribute no token total. Token totals do not represent quota percentages.</p>
    </div>
    {errors.trend && <p role="alert" className="text-sm text-amber-800">Trend refresh failed: {errors.trend}. Previously loaded trend may be old.</p>}
    {trend && <>{trend.days !== days && <p className="text-xs font-semibold text-amber-800">Trend shows the previous period until refresh completes.</p>}
      <Trend records={trend.items} total={trend.total} periodDays={trend.days} /></>}
    <ConfigPanel items={configuration} error={errors.configuration ?? null} loading={!!loading.configuration} />
  </section>;
}

export function CardUsage({ cardId, summary, summaryCurrent, onRefresh }: {
  cardId: string;
  summary: CodexSummary | null;
  summaryCurrent: boolean;
  onRefresh: () => void;
}) {
  const [rows, setRows] = useState<CodexExecution[]>([]);
  const [total, setTotal] = useState(0);
  const [loadedPeriod, setLoadedPeriod] = useState<string | null>(null);
  const requestId = useRef(0);
  const retriedMismatch = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const period = summary ? `${summary.period_start}/${summary.period_end}` : null;
  const aggregate = summary?.token_accounting.by_card.find((item) => item.card_id === cardId);
  const load = useCallback(async (offset: number) => {
    if (!summary) return;
    const currentRequest = ++requestId.current;
    setLoading(true);
    try {
      const next: CodexExecution[] = [];
      let pageTotal = 0;
      do {
        const page = await getCodexExecutions(summary.period_start, summary.period_end, cardId, offset + next.length, 100);
        pageTotal = page.total;
        next.push(...page.items);
        if (page.items.length === 0 || offset > 0) break;
      } while (next.length < MAX_INITIAL_CARD_RECORDS && offset + next.length < pageTotal);
      if (currentRequest !== requestId.current) return;
      setRows((old) => offset === 0 ? next : [...old, ...next]);
      setTotal(pageTotal);
      setLoadedPeriod(`${summary.period_start}/${summary.period_end}`);
      setError(null);
    } catch (caught) {
      if (currentRequest === requestId.current) setError(caught instanceof Error ? caught.message : "Request failed");
    } finally { if (currentRequest === requestId.current) setLoading(false); }
  }, [cardId, summary]);
  useEffect(() => {
    if (period) void load(0);
    return () => { requestId.current += 1; };
  }, [period, cardId, load]);
  const allExecutionsLoaded = rows.length === total;
  const availableRows = rows.filter((row) => row.usage_status === "available");
  const reportedTokens = availableRows.reduce((sum, row) => sum + (row.total_tokens ?? 0), 0);
  const inconsistent = !!summary && summaryCurrent && loadedPeriod === period && !loading && (
    rows.length > total ||
    (aggregate?.execution_count ?? 0) !== total ||
    (allExecutionsLoaded && !!aggregate && (
      aggregate.available_count !== availableRows.length ||
      (aggregate.total_tokens == null ? availableRows.length > 0 : aggregate.total_tokens !== reportedTokens)
    ))
  );
  useEffect(() => {
    if (inconsistent && !retriedMismatch.current) {
      retriedMismatch.current = true;
      onRefresh();
    }
  }, [inconsistent, onRefresh]);
  const breakdown = useMemo(() => STAGES.map((stage) => ({ stage, rows: rows.filter((row) => row.stage === stage) })), [rows]);
  return <section className="rounded-md border border-slate-200 bg-white p-4 shadow-sm" aria-label="Card Codex usage">
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-lg font-black text-slate-950">Card Codex usage</h2>
      <button type="button" onClick={onRefresh} disabled={loading || !summary}
        className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-bold text-slate-700 disabled:opacity-50">Refresh</button></div>
    <p className="mt-1 text-xs text-slate-600">{summary ? `${formatDate(summary.period_start)} – ${formatDate(summary.period_end)} · Period total, not lifetime usage` : "Loading usage period…"}</p>
    {summary && !summaryCurrent && <p className="mt-2 text-xs font-semibold text-amber-800">Showing the last loaded summary period while updated card usage is requested.</p>}
    {error && <p role="alert" className="mt-2 text-sm text-amber-800">Execution refresh failed: {error}. Previously loaded details may be old.</p>}
    {loadedPeriod && loadedPeriod !== period && <p className="mt-2 text-xs font-semibold text-amber-800">Execution details are from a previous selected period until refresh succeeds.</p>}
    {inconsistent && <p role="alert" className="mt-2 text-xs font-semibold text-amber-800">Summary and execution usage differ. The total may be incomplete; refresh to reconcile them.</p>}
    <p className="mt-3 text-xl font-black tabular-nums text-slate-950">{inconsistent ? "Total awaiting reconciliation" : aggregate ? `${formatNumber(aggregate.total_tokens)} tokens` : summaryCurrent ? "Usage unavailable" : "Total unavailable in last summary"}</p>
    {aggregate && <p className="text-xs text-slate-600">{number.format(aggregate.execution_count)} executions · {number.format(aggregate.unavailable_count)} without token reports · Input {formatNumber(aggregate.input_tokens)} · Output {formatNumber(aggregate.output_tokens)} · Cached input {formatNumber(aggregate.cached_input_tokens)}</p>}
    {loading && <p role="status" className="mt-2 text-xs text-slate-600">Loading execution details…</p>}
    {rows.length > 0 && <>
      <div className="mt-3 grid gap-2 sm:grid-cols-3">{breakdown.map(({ stage, rows: stageRows }) => <div key={stage} className="rounded-md bg-slate-50 p-2 text-xs">
        <div className="font-bold text-slate-900">{label(stage)}</div><div className="mt-1 tabular-nums">{stageRows.some((row) => row.usage_status === "available") ? `${number.format(stageRows.reduce((sum, row) => sum + (row.usage_status === "available" ? row.total_tokens ?? 0 : 0), 0))} reported tokens` : "Usage unavailable"}</div>
        <div className="text-slate-500">{stageRows.length} loaded executions</div>
      </div>)}</div>
      {rows.length < total && <p className="mt-2 text-xs font-semibold text-amber-800">Breakdown is partial: {number.format(rows.length)} of {number.format(total)} executions loaded.</p>}
      <button type="button" aria-expanded={detailsOpen} onClick={() => setDetailsOpen(!detailsOpen)} className="mt-3 text-sm font-bold text-blue-700 hover:underline">{detailsOpen ? "Hide" : "Show"} execution details</button>
      {detailsOpen && <div className="mt-2 max-h-96 space-y-2 overflow-y-auto">{rows.map((row) => <div key={`${row.run_id}-${row.attempt_count}-${row.turn_id}`} className="rounded-md border border-slate-200 p-2 text-xs text-slate-700">
        <div className="font-bold text-slate-900">{label(row.stage)} · Revision {row.card_revision} · Attempt {row.attempt_count}{row.review_attempt != null && ` · Review attempt ${row.review_attempt}`}</div>
        <div>Run {row.run_id} · Turn {row.turn_id} · {formatDate(row.recorded_at)}</div>
        <div>Model: {row.actual_model || "Unavailable"}{!row.actual_model && row.configured_model && ` · Configured: ${row.configured_model}`} · Reasoning: {row.reasoning_effort || "Unavailable"}</div>
        <div>{row.usage_status === "available" ? `Total ${formatNumber(row.total_tokens)} · Input ${formatNumber(row.input_tokens)} · Output ${formatNumber(row.output_tokens)} · Cached input ${formatNumber(row.cached_input_tokens)}` : "Token usage unavailable"}</div>
      </div>)}</div>}
      {rows.length < total && <button type="button" disabled={loading || rows.length >= 100_000} onClick={() => void load(rows.length)} className="mt-3 rounded-md border border-slate-300 px-3 py-2 text-xs font-bold text-slate-700 disabled:opacity-50">Load more executions</button>}
    </>}
    {summary && !loading && rows.length === 0 && !error && <p className="mt-2 text-sm text-slate-600">No execution telemetry is available for this card in the selected period.</p>}
  </section>;
}

import { useCallback, useEffect, useRef, useState } from "react";
import { financeApi } from "./api/financeApi";
import type { FinanceAccount } from "./api/financeApi";
import { accountTotals, assessmentGuidance, correctiveAction, nextSnapshot, snapshotOnlyConditions, sortedAccounts } from "./financeHealth";
import type { AccountHealth, Assessment, Health, Monitoring, Readiness } from "./financeHealth";

const tone: Record<Readiness | Health, string> = {
  READY: "border-emerald-200 bg-emerald-50 text-emerald-900",
  HEALTHY: "border-emerald-200 bg-emerald-50 text-emerald-900",
  WARNING: "border-amber-200 bg-amber-50 text-amber-900",
  BLOCKED: "border-rose-200 bg-rose-50 text-rose-900",
  UNKNOWN: "border-slate-300 bg-slate-100 text-slate-800",
};
const title: Record<Readiness | Health, string> = {
  READY: "Ready by current evidence", HEALTHY: "Healthy", WARNING: "Needs review",
  BLOCKED: "Verified blocker", UNKNOWN: "Evidence unknown",
};
const sourceNames: Record<AccountHealth["source_type"], string> = {
  plaid: "Plaid connection", manual: "Manual account", vehicle: "Vehicle valuation",
  valuation: "Provider valuation",
};
const panel = "rounded-xl border border-slate-200 bg-white p-5 shadow-sm";
const link = "font-semibold text-blue-700 underline-offset-2 hover:underline focus-visible:rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-700";
const dateTime = (value: string | null | undefined) => {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unavailable" : new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium", timeStyle: value.includes("T") ? "short" : undefined,
    ...(value.includes("T") ? {} : { timeZone: "UTC" }),
  }).format(date);
};
const calendarDate = (value: Date) => new Intl.DateTimeFormat("en-US", {
  month: "long", day: "numeric", year: "numeric", timeZone: "UTC",
}).format(value);
const message = (error: unknown) => error instanceof Error ? error.message : "Request failed. Try again.";
const issueName = (code: string) => ({
  PLAID_REAUTH_REQUIRED: "Connection needs reauthorization",
  PLAID_CONNECTION_MISSING: "Connection missing",
  PLAID_ACCOUNT_MISSING: "Account missing from collection",
  PLAID_BALANCE_MISSING: "Balance unavailable",
  PLAID_DATA_STALE: "Collection evidence is stale",
  MANUAL_VALUE_MISSING: "Value unavailable",
  MANUAL_REVIEW_DUE: "Value review due",
  VALUATION_FALLBACK_INVALID: "Fallback value unavailable",
  VALUATION_VALUE_MISSING: "Provider value unavailable",
  EVIDENCE_UNAVAILABLE: "Evidence unavailable",
  COMPLETENESS_UNAVAILABLE: "Completeness unavailable",
} as Record<string, string>)[code] ?? "Account review needed";

function Badge({ state }: { state: Readiness | Health }) {
  const icon = state === "BLOCKED" ? "!" : state === "WARNING" ? "!" : state === "UNKNOWN" ? "?" : "✓";
  return <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-bold ${tone[state]}`}>
    <span aria-hidden="true">{icon}</span>{title[state]}
  </span>;
}

function AccountCard({ account, directory }: { account: AccountHealth; directory?: FinanceAccount }) {
  const action = correctiveAction(account, !!directory);
  const identity = account.source_type === "plaid" && directory?.institution_label
    ? `${directory.institution_label}${directory.mask ? ` · ••••${directory.mask}` : ""}`
    : sourceNames[account.source_type];
  return <article className={`${panel} min-w-0 ${!account.included_in_net_worth ? "bg-slate-50" : ""}`}>
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 flex-1">
        <h3 className="break-words text-base font-bold text-slate-950">{account.display_name}</h3>
        <p className="mt-1 text-sm text-slate-600">{identity} · {account.category?.replace(/_/g, " ") ?? "Category unavailable"}</p>
      </div>
      <Badge state={account.health} />
    </div>
    <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs font-medium text-slate-600">
      <span>{account.included_in_net_worth ? "Included in net worth" : "Excluded · informational"}</span>
      <span>Freshness: {account.freshness.toLowerCase()}</span>
      <span>Last successful evidence: {dateTime(account.last_successful_update)}</span>
    </div>
    {account.issues.length > 0 && <p className="mt-3 text-sm text-slate-800">{account.issues[0].description}</p>}
    <details className="mt-3 border-t border-slate-100 pt-3 text-sm">
      <summary className="cursor-pointer font-semibold text-blue-800 focus-visible:rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-700">
        {account.issues.length ? `Review ${account.issues.length} ${account.issues.length === 1 ? "issue" : "issues"}` : "View evidence"}
      </summary>
      <div className="mt-3 space-y-3 text-slate-700">
        {account.issues.length ? account.issues.map((issue, index) => <div key={`${issue.code}:${index}`}>
          <h4 className="font-semibold">{issueName(issue.code)}</h4>
          <p>{issue.description}</p>
          <p className="mt-1 text-xs text-slate-600">{account.health === "BLOCKED" && account.included_in_net_worth
            ? "Verified blocker for current snapshot completeness."
            : account.health === "UNKNOWN" ? "Stored evidence cannot confirm current readiness."
              : account.included_in_net_worth ? "Review before the next collection; future success is not assured."
                : "Excluded from net worth; this does not block snapshot completeness."}</p>
          {issue.action && <p className="mt-1 text-xs">Recommended: {issue.action}</p>}
        </div>) : <p>{account.explanation}</p>}
        {action ? <a className={link} href={action.href}>{action.label} →</a>
          : account.issues.length > 0 && <p className="text-xs text-slate-600">No direct portal repair is available for this source. Review the existing Finance account or operator workflow; run preflight after it is resolved.</p>}
        {account.issues.some((issue) => issue.code === "PLAID_REAUTH_REQUIRED") &&
          <p className="text-xs text-slate-600">Plaid reauthorization must use the existing authorized operator process. Account metadata editing does not reconnect a bank.</p>}
      </div>
    </details>
  </article>;
}

export default function FinanceHealthScreen() {
  const [assessment, setAssessment] = useState<Assessment | null>(null);
  const [monitoring, setMonitoring] = useState<Monitoring | null>(null);
  const [directory, setDirectory] = useState<FinanceAccount[]>([]);
  const [preflightError, setPreflightError] = useState<string | null>(null);
  const [monitorError, setMonitorError] = useState<string | null>(null);
  const [directoryError, setDirectoryError] = useState(false);
  const [preflightBusy, setPreflightBusy] = useState(false);
  const [monitorBusy, setMonitorBusy] = useState(false);
  const [manualAt, setManualAt] = useState<string | null>(null);
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const preflightLock = useRef(false);
  const monitorLock = useRef(false);
  const runPreflight = useCallback(async (manual: boolean) => {
    if (preflightLock.current) return;
    preflightLock.current = true;
    setPreflightBusy(true); setPreflightError(null);
    try {
      const result = await financeApi.preflight();
      setAssessment(result);
      if (manual) setManualAt(result.assessed_at);
    } catch (error) { setPreflightError(message(error)); }
    finally { preflightLock.current = false; setPreflightBusy(false); }
  }, []);
  const loadMonitoring = useCallback(async () => {
    if (monitorLock.current) return;
    monitorLock.current = true;
    setMonitorBusy(true); setMonitorError(null);
    try { setMonitoring(await financeApi.monitoring()); }
    catch (error) { setMonitoring(null); setMonitorError(message(error)); }
    finally { monitorLock.current = false; setMonitorBusy(false); }
  }, []);
  useEffect(() => {
    void runPreflight(false);
    void loadMonitoring();
    void financeApi.accounts().then(setDirectory).catch(() => setDirectoryError(true));
  }, [runPreflight, loadMonitoring]);

  const totals = assessment ? accountTotals(assessment.accounts) : null;
  const next = nextSnapshot(new Date());
  const accounts = sortedAccounts(assessment?.accounts ?? []).filter((account) => {
    const matches = filter === "all" || filter === account.source_type ||
      filter === "attention" && account.included_in_net_worth && ["BLOCKED", "WARNING"].includes(account.health) ||
      filter === "healthy" && account.health === "HEALTHY" ||
      filter === "unknown" && account.health === "UNKNOWN";
    return matches && `${account.display_name} ${account.category ?? ""} ${account.source_type}`.toLowerCase().includes(search.toLowerCase());
  });
  const guidance = assessment ? assessmentGuidance(assessment) : null;
  const snapshotConditions = assessment ? snapshotOnlyConditions(assessment) : [];
  return <div className="space-y-5">
    <section className={`${panel} border-slate-300 bg-gradient-to-br from-white to-slate-50`}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div><p className="text-xs font-bold uppercase tracking-widest text-slate-500">Finance · current evidence</p>
          <h2 className="mt-1 text-2xl font-black">Account health</h2>
          <p className="mt-2 max-w-2xl text-sm text-slate-600">Review stored account evidence before the next monthly snapshot. A successful snapshot this month does not guarantee future collection.</p></div>
        <button type="button" className="button focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 disabled:opacity-50" disabled={preflightBusy} onClick={() => void runPreflight(true)}>{preflightBusy ? "Checking evidence…" : "Run Preflight"}</button>
      </div>
      <p className="mt-2 text-xs text-slate-600">Checks existing account evidence without refreshing financial connections.</p>
      {preflightError && <div role="alert" className="mt-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">Preflight unavailable: {preflightError} <button type="button" className="ml-2 font-bold underline" onClick={() => void runPreflight(true)}>Retry</button></div>}
      {assessment ? <div className="mt-5 space-y-4">
        <div className="flex flex-wrap items-center gap-3"><Badge state={assessment.readiness} /><p className="text-sm text-slate-800">{guidance?.overview}</p></div>
        {snapshotConditions.length > 0 && <div className="rounded-lg border border-slate-200 bg-white p-3 text-sm text-slate-800"><h3 className="font-bold">Snapshot-level findings</h3><ul className="mt-2 space-y-2">{snapshotConditions.map((entry, index) => <li key={`${entry.code}:${index}`}><span className="font-semibold">{entry.kind === "blocker" ? "Verified blocker" : "Review concern"}:</span> {entry.description}{entry.action && <span className="text-slate-600"> Recommended: {entry.action}</span>}</li>)}</ul></div>}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{[
          ["Healthy", totals?.healthy], ["Need attention", totals?.attention],
          ["Verified blockers", totals?.blocked], ["Insufficient evidence", totals?.unknown],
        ].map(([name, count]) => <div key={name} className="rounded-lg border border-slate-200 bg-white p-3"><p className="text-xs font-semibold text-slate-600">{name} · included accounts</p><p className="mt-1 text-2xl font-black tabular-nums">{count}</p></div>)}</div>
        <p className="text-xs text-slate-600">Current preflight assessed {dateTime(assessment.assessed_at)} · Latest manual preflight: {dateTime(manualAt)} · Current-period snapshot evidence: {dateTime(assessment.data_as_of)}</p>
        {!assessment.accounts.length && <p className="text-sm text-slate-700">No evaluated accounts were returned. Readiness cannot be inferred from an empty list.</p>}
      </div> : !preflightError && <p role="status" className="mt-5 text-sm text-slate-600">Checking account evidence…</p>}
    </section>

    <div className="grid gap-4 lg:grid-cols-2">
      <section className={panel}><p className="text-xs font-bold uppercase tracking-widest text-slate-500">Next snapshot</p>
        <h2 className="mt-1 text-lg font-bold">{calendarDate(next.target)}</h2>
        <p className="mt-2 text-sm text-slate-700">{guidance?.next ?? "Account concerns are unavailable until preflight completes."}</p>
        <p className="mt-2 text-xs text-slate-600">Conditional Android reminder windows: {calendarDate(next.threeDays)} (three days before) and {calendarDate(next.oneDay)} (one day before), if actionable problems remain. Delivery is not implied.</p>
      </section>
      <section className={panel}><div className="flex items-start justify-between gap-2"><div><p className="text-xs font-bold uppercase tracking-widest text-slate-500">Automated monitoring</p><h2 className="mt-1 text-lg font-bold">Persisted assessment</h2></div><button className={link} type="button" disabled={monitorBusy} onClick={() => void loadMonitoring()}>{monitorBusy ? "Loading…" : "Refresh"}</button></div>
        {monitorError ? <p role="alert" className="mt-3 text-sm text-rose-800">Monitoring unavailable: {monitorError}. Current preflight results, if shown, remain separate.</p>
          : monitoring?.assessed_at ? <div className="mt-3 space-y-2 text-sm"><Badge state={monitoring.readiness ?? "UNKNOWN"} /><p>Last successful automated assessment: {dateTime(monitoring.assessed_at)}</p><p>{monitoring.outstanding_issues.length} active issue {monitoring.outstanding_issues.length === 1 ? "record" : "records"}. An account can have multiple issues.</p>{monitoring.outstanding_issues.length > 0 && <ul className="list-inside list-disc text-slate-700">{[...new Set(monitoring.outstanding_issues.map((item) => issueName(item.code)))].slice(0, 3).map((name) => <li key={name}>{name}</li>)}</ul>}{monitoring.previous_readiness && <p className="text-slate-600">Previous readiness: {monitoring.previous_readiness}</p>}</div>
            : <p className="mt-3 text-sm text-slate-600">{monitorBusy ? "Loading monitoring status…" : "Not yet assessed automatically."}</p>}
      </section>
    </div>

    <section aria-labelledby="account-health-heading" className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3"><div><h2 id="account-health-heading" className="text-xl font-bold">Accounts and valuations</h2><p className="text-sm text-slate-600">Affected included accounts appear first. Excluded records are informational.</p></div><span className="text-xs text-slate-500">{accounts.length} shown</span></div>
      <div className="flex flex-wrap gap-3"><label className="grid min-w-[11rem] flex-1 gap-1 text-xs font-semibold text-slate-600">Filter<select className="input" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">All accounts</option><option value="attention">Needs attention</option><option value="unknown">Unknown</option><option value="healthy">Healthy</option><option value="plaid">Plaid</option><option value="manual">Manual</option><option value="vehicle">Vehicle</option><option value="valuation">Other valuations</option></select></label><label className="grid min-w-[11rem] flex-1 gap-1 text-xs font-semibold text-slate-600">Search<input className="input" type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Account name or source" /></label></div>
      {directoryError && <p className="text-xs text-slate-600">Account directory details are unavailable. Health assessments remain available.</p>}
      {assessment && accounts.length === 0 && <p className={`${panel} text-sm text-slate-600`}>{assessment.accounts.length ? "No accounts match these filters." : "No accounts have been configured or evaluated."}</p>}
      <div className="grid gap-3 lg:grid-cols-2">{accounts.map((account, index) => <AccountCard key={`${account.source_type}:${account.account_id}:${index}`} account={account} directory={directory.find((row) => row.id === account.account_id && row.source === "plaid")} />)}</div>
    </section>

    {monitoring && monitoring.history.length > 0 && <section className={panel}><details><summary className="cursor-pointer text-lg font-bold focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-700">Recent monitoring activity</summary><ol className="mt-3 divide-y divide-slate-100 text-sm">{monitoring.history.slice(0, 12).map((event, index) => <li className="flex flex-wrap justify-between gap-2 py-2" key={`${event.created_at}:${index}`}><span>{event.event_type === "opened" ? "Issue detected or reopened" : event.event_type === "resolved" ? "Issue resolved" : event.event_type === "reminder" ? "Reminder recorded" : "Reminder suppressed"}{event.target_month ? ` · ${event.target_month}` : ""}</span><time className="text-slate-600">{dateTime(event.created_at)}</time></li>)}</ol><p className="mt-2 text-xs text-slate-600">Activity records are historical. Only the active issue count above reflects unresolved monitoring issues.</p></details></section>}
  </div>;
}

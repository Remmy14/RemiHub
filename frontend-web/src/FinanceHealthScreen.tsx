import { useCallback, useEffect, useRef, useState } from "react";
import { financeApi } from "./api/financeApi";
import type { FinanceAccount, FinanceSummary } from "./api/financeApi";
import { accountTotals, assessmentGuidance, correctiveAction, HealthRefreshCoordinator, nextSnapshot, snapshotOnlyConditions, sortedAccounts } from "./financeHealth";
import type { AccountHealth, Assessment, Health, Monitoring, Readiness } from "./financeHealth";
import { VerificationRunner, runNotFoundMessage } from "./financeVerification";
import type { VerificationExecution } from "./financeVerification";

const savedRunKey = "finance-health-verification-run";

const tone: Record<Readiness | Health, string> = {
  READY: "border-emerald-200 bg-emerald-50 text-emerald-900",
  HEALTHY: "border-emerald-200 bg-emerald-50 text-emerald-900",
  WARNING: "border-amber-200 bg-amber-50 text-amber-900",
  BLOCKED: "border-rose-200 bg-rose-50 text-rose-900",
  UNKNOWN: "border-slate-300 bg-slate-100 text-slate-800",
};
const title: Record<Readiness | Health, string> = {
  READY: "Ready by current evidence", HEALTHY: "Healthy by available evidence", WARNING: "Needs review",
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
const verificationMethod = (method: string | null | undefined) => method === "accounts_get"
  ? "Cached account inventory" : method === "item_get+accounts_get"
    ? "Item status and cached account inventory" : method === "item_get" ? "Item status" : "Unavailable";
const verificationCondition = (status: string) => ({
  VERIFIED: "Supported connection check passed", AUTH_REQUIRED: "Authentication required",
  UNKNOWN: "Connection status unknown", AVAILABLE: "Available in cached inventory",
  MISSING: "Missing from cached inventory", VALID: "Cached balance valid",
  INVALID: "Cached balance invalid", TRANSIENT: "Technical check incomplete",
} as Record<string, string>)[status] ?? "Unknown";
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
  const mask = directory?.mask && /^\d{1,4}$/.test(directory.mask) ? directory.mask : null;
  const identity = account.source_type === "plaid" && directory?.institution_label
    ? `${directory.institution_label}${mask ? ` · ••••${mask}` : ""}`
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
      {account.source_type === "plaid" ? <span>Last monthly collection: {dateTime(account.last_monthly_balance_collection)}</span>
        : <span>Last successful evidence: {dateTime(account.last_successful_update)}</span>}
    </div>
    {account.source_type === "plaid" && <div className="mt-3 grid gap-1 text-xs text-slate-700 sm:grid-cols-2">
      <span>Last active verification: {dateTime(account.verification?.attempted_at)}</span>
      <span>Connection: {verificationCondition(account.verification?.connection_status ?? "UNKNOWN")}</span>
      <span>Account inventory: {verificationCondition(account.verification?.inventory_status ?? "UNKNOWN")}</span>
      <span>Verification method: {verificationMethod(account.verification?.method)}</span>
      <span>Live balance collection: {account.verification?.live_balance_tested ? "Tested" : "Unverified"}</span>
      <span>Evidence: {account.verification?.outcome === "TRANSIENT" ? "Technical check incomplete" : account.verification?.collection_readiness === "CACHED_INVENTORY_AVAILABLE" ? "Cached inventory available; future collection unverified" : account.verification?.collection_readiness === "BLOCKED" ? "Confirmed problem" : "Insufficient evidence"}</span>
    </div>}
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
  const [snapshot, setSnapshot] = useState<FinanceSummary | null>(null);
  const [snapshotError, setSnapshotError] = useState(false);
  const [verification, setVerification] = useState<VerificationExecution | null>(null);
  const [verificationBusy, setVerificationBusy] = useState(false);
  const [verificationError, setVerificationError] = useState<string | null>(null);
  const [dashboardOutdated, setDashboardOutdated] = useState(false);
  const [dashboardRefreshing, setDashboardRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState(false);
  const [preflightError, setPreflightError] = useState<string | null>(null);
  const [monitorError, setMonitorError] = useState<string | null>(null);
  const [directoryError, setDirectoryError] = useState(false);
  const [preflightBusy, setPreflightBusy] = useState(false);
  const [monitorBusy, setMonitorBusy] = useState(false);
  const [manualAt, setManualAt] = useState<string | null>(null);
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const preflightRequest = useRef(0);
  const monitorRequest = useRef(0);
  const directoryRequest = useRef(0);
  const snapshotRequest = useRef(0);
  const refreshCoordinator = useRef<HealthRefreshCoordinator>(new HealthRefreshCoordinator());
  const runner = useRef<VerificationRunner | null>(null);
  const runPreflight = useCallback(async (manual: boolean) => {
    const request = ++preflightRequest.current;
    setPreflightBusy(true); setPreflightError(null);
    try {
      const result = await financeApi.preflight();
      const current = request === preflightRequest.current;
      if (current) {
        setAssessment(result);
        if (manual) setManualAt(result.assessed_at);
      }
      return current;
    } catch (error) { if (request === preflightRequest.current) setPreflightError(message(error)); return false; }
    finally { if (request === preflightRequest.current) setPreflightBusy(false); }
  }, []);
  const loadMonitoring = useCallback(async () => {
    const request = ++monitorRequest.current;
    setMonitorBusy(true); setMonitorError(null);
    try { const result = await financeApi.monitoring(); const current = request === monitorRequest.current; if (current) setMonitoring(result); return current; }
    catch (error) { if (request === monitorRequest.current) setMonitorError(message(error)); return false; }
    finally { if (request === monitorRequest.current) setMonitorBusy(false); }
  }, []);
  const loadDirectory = useCallback(async () => {
    const request = ++directoryRequest.current;
    try { const result = await financeApi.accounts(); const current = request === directoryRequest.current; if (current) { setDirectory(result); setDirectoryError(false); } return current; }
    catch { if (request === directoryRequest.current) setDirectoryError(true); return false; }
  }, []);
  const loadSnapshot = useCallback(async () => {
    const request = ++snapshotRequest.current;
    try { const result = await financeApi.summary(); const current = request === snapshotRequest.current; if (current) { setSnapshot(result); setSnapshotError(false); } return current; }
    catch { if (request === snapshotRequest.current) setSnapshotError(true); return false; }
  }, []);
  const refreshDashboard = useCallback(async () => {
    setDashboardRefreshing(true);
    await refreshCoordinator.current.run([
      () => runPreflight(false), loadMonitoring, loadDirectory, loadSnapshot,
    ], (allUpdated) => {
      setRefreshError(!allUpdated); setDashboardOutdated(!allUpdated);
      setDashboardRefreshing(false);
    });
  }, [runPreflight, loadMonitoring, loadDirectory, loadSnapshot]);
  useEffect(() => {
    void runPreflight(false);
    void loadMonitoring();
    void loadDirectory();
    void loadSnapshot();
    const instance = new VerificationRunner(financeApi, (state) => {
      setVerification(state.execution); setVerificationBusy(state.busy); setVerificationError(state.error);
      if (state.execution?.execution_id && !["completed", "partially_completed", "failed"].includes(state.execution.status)) {
        sessionStorage.setItem(savedRunKey, state.execution.execution_id);
      }
      if (state.execution && ["completed", "partially_completed", "failed"].includes(state.execution.status)) {
        sessionStorage.removeItem(savedRunKey);
      }
      if (state.error === runNotFoundMessage) sessionStorage.removeItem(savedRunKey);
    }, () => { void refreshDashboard(); });
    runner.current = instance;
    const saved = sessionStorage.getItem(savedRunKey);
    if (saved) { refreshCoordinator.current.invalidate(); setDashboardOutdated(true); instance.resume(saved); }
    return () => {
      instance.cancel(); runner.current = null;
      refreshCoordinator.current.invalidate();
      preflightRequest.current++; monitorRequest.current++; directoryRequest.current++; snapshotRequest.current++;
    };
  }, [runPreflight, loadMonitoring, loadDirectory, loadSnapshot, refreshDashboard]);
  const startVerification = () => {
    if (verificationBusy) return;
    refreshCoordinator.current.invalidate();
    setDashboardOutdated(true); setRefreshError(false);
    setDashboardRefreshing(false);
    const saved = sessionStorage.getItem(savedRunKey);
    if (saved) runner.current?.resume(saved);
    else void runner.current?.start();
  };

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
        <div className="flex flex-wrap gap-2">
          <button type="button" className="button focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 disabled:opacity-50" disabled={preflightBusy} onClick={() => void runPreflight(true)}>{preflightBusy ? "Checking evidence…" : "Run Preflight"}</button>
          <button type="button" className="button focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 disabled:opacity-50" disabled={verificationBusy} onClick={startVerification}>{verificationBusy ? "Verifying connections…" : sessionStorage.getItem(savedRunKey) ? "Check Verification Status" : "Verify Connections Now"}</button>
        </div>
      </div>
      <p className="mt-2 text-xs text-slate-600">Run Preflight checks existing account evidence. Verify Connections Now actively checks supported financial connections without creating a snapshot or collecting live balances.</p>
      {(verificationBusy || verificationError || verification) && <div className="mt-4 rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-slate-900" aria-live="polite">
        {verificationBusy && <p role="status" className="font-semibold">{verification?.reused ? "Connection verification is already running." : "Verifying financial connections…"}{verification?.results.connections.length ? ` ${verification.results.connections.length} connection results received.` : ""}</p>}
        {verificationError && <p role="alert" className="text-rose-800">{verificationError}</p>}
        {!verificationBusy && verificationError && sessionStorage.getItem(savedRunKey) && <button type="button" className={`${link} mt-2`} onClick={() => runner.current?.resume(sessionStorage.getItem(savedRunKey)!)}>Check saved run status</button>}
        {verification && ["completed", "partially_completed", "failed"].includes(verification.status) && <div className="space-y-2">
          <h3 className="font-bold">{verification.status === "completed" ? "Verification completed" : verification.status === "partially_completed" ? "Verification partially completed" : "Verification failed"}</h3>
          {verification.completed_at && <p>Completed {dateTime(verification.completed_at)}</p>}
          {verification.summary.connections_checked !== undefined && <p>{verification.summary.connections_checked} connections checked.</p>}
          {verification.summary.included_accounts !== undefined && <p>{verification.summary.included_accounts} included accounts evaluated.</p>}
          {verification.summary.connections_with_problems !== undefined && <p>{verification.summary.connections_with_problems} connections with confirmed problems.</p>}
          {verification.summary.accounts_with_actionable_issues !== undefined && <p>{verification.summary.accounts_with_actionable_issues} accounts with reported issues or insufficient evidence.</p>}
          {verification.summary.connections_uncertain !== undefined && <p>{verification.summary.connections_uncertain} connections with incomplete or unknown results.</p>}
          {verification.results.connections.length > 0 && <p>{verification.results.connections.filter((row) => row.outcome === "VERIFIED" && !row.issue_code && row.missing_accounts === 0 && row.invalid_cached_balances === 0).length} connections had no detected problem in supported checks.</p>}
          {verification.summary.methods_used?.length ? <p>Methods used: {verification.summary.methods_used.map(verificationMethod).join(", ")}.</p> : null}
          <p>{verification.summary.live_balance_tested === true ? "Live balance retrieval was tested." : verification.summary.live_balance_tested === false ? "Live balance collection was not performed; future collection remains unverified." : "Live balance testing status is unavailable."}</p>
          {verification.error_code && <p>{verification.error_code === "EXECUTION_INTERRUPTED" ? "The verification was interrupted." : "The verification encountered an execution error."}</p>}
          {verification.status !== "completed" && <p>Some checks did not finish. Successful results below remain available.</p>}
          {verification.results.connections.length > 0 && <ul className="mt-2 space-y-2 border-t border-blue-200 pt-2">{verification.results.connections.map((row, index) => <li key={`${row.label}:${index}`} className="break-words"><strong>{row.label}</strong>: {row.issue_code ? verificationCondition(row.connection_status === "AUTH_REQUIRED" ? "AUTH_REQUIRED" : row.inventory_status === "MISSING" ? "MISSING" : "INVALID") : row.outcome === "TRANSIENT" ? "Technical check incomplete" : row.outcome === "UNKNOWN" ? "Evidence unknown" : "Supported check passed"}. {row.explanation}{row.recommended_action ? ` Next: ${row.recommended_action}` : ""}</li>)}</ul>}
        </div>}
      </div>}
      {dashboardOutdated && <div role="status" className="mt-3 text-xs text-amber-900">Dashboard assessments shown below may predate the latest verification.{refreshError ? " One or more dashboard sections could not be refreshed; verification results above remain valid." : dashboardRefreshing ? " Refreshing dashboard sections now." : verificationBusy ? " Verification is still in progress." : " Refresh these assessments after verification completes."}{!verificationBusy && !dashboardRefreshing && <button type="button" className="ml-2 font-semibold underline" onClick={() => void refreshDashboard()}>Refresh dashboard</button>}</div>}
      {preflightError && <div role="alert" className="mt-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">Preflight unavailable: {preflightError} <button type="button" className="ml-2 font-bold underline" onClick={() => void runPreflight(true)}>Retry</button></div>}
      {assessment ? <div className="mt-5 space-y-4">
        <div className="flex flex-wrap items-center gap-3"><Badge state={assessment.readiness} /><p className="text-sm text-slate-800">{guidance?.overview}</p></div>
        <p className="text-xs text-slate-600">Snapshot evidence and future collection readiness are separate. An UNKNOWN account lacks enough evidence; an older monthly collection alone is not a confirmed connection failure.</p>
        <p className="text-xs text-slate-600">{snapshot?.status === "success" && snapshot.snapshot_month.slice(0, 7) === assessment.snapshot.snapshot_month.slice(0, 7)
          ? `Snapshot complete for ${snapshot.snapshot_month.slice(0, 7)}. This does not verify the next monthly collection.`
          : snapshotError ? "Snapshot completion status unavailable." : "No successful current-period snapshot is confirmed by the available summary."}</p>
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
      <section className={panel}><div className="flex items-start justify-between gap-2"><div><p className="text-xs font-bold uppercase tracking-widest text-slate-500">Monitoring</p><h2 className="mt-1 text-lg font-bold">Persisted assessment</h2></div><button className={link} type="button" disabled={monitorBusy} onClick={() => void loadMonitoring()}>{monitorBusy ? "Loading…" : "Refresh"}</button></div>
        {monitorError ? <p role="alert" className="mt-3 text-sm text-rose-800">Monitoring unavailable: {monitorError}. Current preflight results, if shown, remain separate.</p>
          : monitoring?.assessed_at ? <div className="mt-3 space-y-2 text-sm"><Badge state={monitoring.readiness ?? "UNKNOWN"} /><p>Last persisted monitoring assessment: {dateTime(monitoring.assessed_at)}</p><p className="text-xs text-slate-600">This may reflect a scheduled assessment or reconciliation after manual verification.</p><p>{monitoring.outstanding_issues.length} active issue {monitoring.outstanding_issues.length === 1 ? "record" : "records"}. An account can have multiple issues.</p>{monitoring.outstanding_issues.length > 0 && <ul className="list-inside list-disc text-slate-700">{[...new Set(monitoring.outstanding_issues.map((item) => issueName(item.code)))].slice(0, 3).map((name) => <li key={name}>{name}</li>)}</ul>}{monitoring.previous_readiness && <p className="text-slate-600">Previous readiness: {monitoring.previous_readiness}</p>}</div>
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

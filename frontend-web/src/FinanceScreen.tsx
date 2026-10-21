import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";

import { financeApi } from "./api/financeApi";
import type {
  FinanceAccount, FinanceHistoryPoint, ManualAccount,
  ManualAccountCreate, SnapshotItem,
} from "./api/financeApi";

const base = "/portal/finance";
const categories = ["cash", "brokerage", "retirement", "hsa", "education", "home", "vehicle", "other", "liability"];
const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

function money(value: number | string | null | undefined) {
  if (value === null || value === undefined || value === "") return "Unavailable";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? currency.format(parsed) : "Unavailable";
}

function label(value: string) {
  return value === "hsa" ? "HSA" : value.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Finance request failed. Try again.";
}

function dateText(value: string | null | undefined) {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function Panel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`rounded-xl border border-slate-200 bg-white p-5 shadow-sm ${className}`}>{children}</section>;
}

function Notice({ children, kind = "info" }: { children: ReactNode; kind?: "info" | "error" | "warning" }) {
  const color = kind === "error" ? "border-red-200 bg-red-50 text-red-800" :
    kind === "warning" ? "border-amber-200 bg-amber-50 text-amber-900" :
      "border-blue-200 bg-blue-50 text-blue-900";
  return <div role={kind === "error" ? "alert" : "status"} className={`rounded-lg border p-3 text-sm ${color}`}>{children}</div>;
}

function useLoad<T>(load: () => Promise<T>, dependencies: unknown[]) {
  const [value, setValue] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setValue(await load());
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  // Each caller passes stable inputs; refresh when its route identifier changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, dependencies);
  useEffect(() => { void refresh(); }, [refresh]);
  return { value, loading, error, refresh };
}

function SnapshotItems({ items }: { items: SnapshotItem[] }) {
  if (!items.length) return <p className="text-sm text-slate-600">No included sources in this snapshot.</p>;
  return <div className="divide-y divide-slate-100">
    {items.map((item, index) => <div className="flex flex-wrap items-start justify-between gap-2 py-3" key={`${item.source}:${item.source_account_id ?? index}`}>
      <div>
        <div className="font-semibold">{item.label}</div>
        <div className="text-xs text-slate-600">{item.institution_label || label(item.source)} · {label(item.asset_category)} · {label(item.confidence)}</div>
      </div>
      <div className="font-semibold tabular-nums">{money(item.value)}</div>
    </div>)}
  </div>;
}

function chartCurrency(value: number) {
  if (value === 0) return "$0";
  const magnitude = Math.abs(value);
  const unit = magnitude >= 1_000_000 ? 1_000_000 : magnitude >= 1_000 ? 1_000 : 1;
  const suffix = unit === 1_000_000 ? "M" : unit === 1_000 ? "K" : "";
  return `${value < 0 ? "-" : ""}$${Number((magnitude / unit).toFixed(2))}${suffix}`;
}

function chartScale(values: number[]) {
  const minimum = Math.min(0, ...values);
  const maximum = Math.max(0, ...values);
  const roughStep = (maximum - minimum) / 5 || 1;
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const step = [1, 2, 2.5, 5, 10].map((factor) => factor * magnitude).find((candidate) => candidate >= roughStep) ?? 10 * magnitude;
  const first = Math.floor(minimum / step);
  const last = Math.max(first + 1, Math.ceil(maximum / step));
  return {
    minimum: first * step,
    maximum: last * step,
    ticks: Array.from({ length: last - first + 1 }, (_, index) => Number(((first + index) * step).toPrecision(12))),
  };
}

function HistoryChart({ points }: { points: FinanceHistoryPoint[] }) {
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(320);
  useEffect(() => {
    if (!container.current) return;
    const element = container.current;
    const measure = () => setWidth(Math.max(280, Math.floor(element.getBoundingClientRect().width)));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [points]);
  if (points.length < 2) return <p className="text-sm text-slate-600">At least two persisted snapshots are needed for a trend chart.</p>;
  const sorted = [...points].sort((a, b) => a.snapshot_month.localeCompare(b.snapshot_month));
  const monthNumber = (month: string) => {
    const match = /^(\d{4})-(\d{2})/.exec(month);
    return match ? Number(match[1]) * 12 + Number(match[2]) : NaN;
  };
  const months = sorted.map((point) => monthNumber(point.snapshot_month));
  if (months.some((month) => !Number.isFinite(month))) return <Notice kind="warning">Historical dates are unavailable for a chart. Use the table below.</Notice>;
  const values = sorted.flatMap((point) => [point.total_assets, point.total_liabilities, point.net_worth]);
  const scale = chartScale(values);
  const left = 62;
  const right = width - 16;
  const top = 24;
  const bottom = 210;
  const x = (index: number) => left + (months[index] - months[0]) / (months[months.length - 1] - months[0] || 1) * (right - left);
  const y = (value: number) => bottom - (value - scale.minimum) / (scale.maximum - scale.minimum) * (bottom - top);
  const monthLabel = (month: string) => {
    const [year, number] = month.split("-").map(Number);
    return `${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][number - 1]} '${String(year).slice(-2)}`;
  };
  const labelIndexes = [0];
  for (let index = 1; index < sorted.length - 1; index += 1) {
    if (x(index) - x(labelIndexes[labelIndexes.length - 1]) >= 72 && x(sorted.length - 1) - x(index) >= 72) labelIndexes.push(index);
  }
  if (x(sorted.length - 1) - x(labelIndexes[labelIndexes.length - 1]) >= 72) labelIndexes.push(sorted.length - 1);
  const series: { key: "total_assets" | "total_liabilities" | "net_worth"; title: string; color: string }[] = [
    { key: "total_assets", title: "Assets", color: "#2563eb" },
    { key: "total_liabilities", title: "Liabilities", color: "#dc2626" },
    { key: "net_worth", title: "Net worth", color: "#059669" },
  ];
  return <div ref={container}>
    <div className="mb-2 flex flex-wrap gap-4 text-xs font-semibold">{series.map((line) => <span key={line.key} style={{ color: line.color }}>● {line.title}</span>)}</div>
    <svg role="img" aria-label="Persisted monthly assets, liabilities, and net worth in dollars. Missing months are left blank." viewBox={`0 0 ${width} 250`} className="w-full" preserveAspectRatio="xMidYMid meet">
      {scale.ticks.map((tick) => <g key={tick}>
        <line x1={left} x2={right} y1={y(tick)} y2={y(tick)} stroke="currentColor" opacity="0.13" />
        <line x1={left - 4} x2={left} y1={y(tick)} y2={y(tick)} stroke="currentColor" opacity="0.5" />
        <text x={left - 8} y={y(tick)} dy="0.32em" textAnchor="end" fontSize="11" fill="currentColor" opacity="0.7">{chartCurrency(tick)}</text>
      </g>)}
      <line x1={left} x2={left} y1={top} y2={bottom} stroke="currentColor" opacity="0.3" />
      <line x1={left} x2={right} y1={bottom} y2={bottom} stroke="currentColor" opacity="0.3" />
      {series.flatMap((line) => sorted.slice(1).map((point, index) => {
        if (months[index + 1] - months[index] !== 1) return null;
        return <line key={`${line.key}:${point.snapshot_id}`} x1={x(index)} x2={x(index + 1)} y1={y(sorted[index][line.key])} y2={y(point[line.key])} stroke={line.color} strokeWidth="2" />;
      }))}
      {series.flatMap((line) => sorted.map((point, index) => <circle key={`${line.key}:${point.snapshot_id}`} cx={x(index)} cy={y(point[line.key])} r="3.5" fill={line.color}><title>{`${point.snapshot_month}: ${line.title} ${money(point[line.key])}${point.status !== "success" ? ` (${point.status})` : ""}`}</title></circle>))}
      {labelIndexes.map((index) => <text key={sorted[index].snapshot_id} x={x(index)} y="239" textAnchor={index === 0 ? "start" : index === sorted.length - 1 ? "end" : "middle"} fontSize="11" fill="currentColor" opacity="0.7">{monthLabel(sorted[index].snapshot_month)}</text>)}
    </svg>
    <p className="text-xs text-slate-500">Points are persisted snapshots. Lines stop across missing months.</p>
  </div>;
}

function Dashboard() {
  const summary = useLoad(financeApi.summary, []);
  const history = useLoad(() => financeApi.history(), []);
  if (summary.loading && !summary.value) return <Panel>Loading financial summary…</Panel>;
  if (summary.error && !summary.value) return <Notice kind={summary.error.includes("No finance snapshots") ? "info" : "error"}>{summary.error}</Notice>;
  const current = summary.value;
  if (!current) return <Notice>No financial snapshot is available.</Notice>;
  const assets = current.categories.filter((category) => category.asset_category !== "liability");
  const liabilities = current.categories.filter((category) => category.asset_category === "liability");
  const successful = history.value?.filter((point) => point.status === "success") ?? [];
  const lastSuccess = successful[successful.length - 1];
  return <div className="space-y-5">
    {current.status !== "success" && <Notice kind="warning">Latest snapshot is {label(current.status)}. Totals may be incomplete, especially liabilities. Use care when interpreting net worth.</Notice>}
    <div className="grid gap-4 md:grid-cols-3">
      {([ ["Total assets", current.total_assets], ["Total liabilities", current.total_liabilities], ["Net worth", current.net_worth] ] as const).map(([title, value]) =>
        <Panel key={title}><div className="text-sm font-semibold text-slate-600">{title}</div><div className="mt-2 text-2xl font-black tabular-nums">{money(value)}</div></Panel>)}
    </div>
    <Panel><h2 className="text-lg font-bold">Snapshot freshness</h2><p className="mt-2 text-sm">Latest snapshot: {current.snapshot_month} · Status: {label(current.status)}</p><p className="text-sm">Last successful snapshot: {history.loading ? "Loading…" : lastSuccess?.snapshot_month ?? (history.error ? "Unavailable" : "None in loaded history")}. Exact update time is not supplied by the API.</p>{history.error && <p className="text-sm text-red-700">History unavailable: {history.error}</p>}</Panel>
    <div className="grid gap-4 lg:grid-cols-2">
      <Panel><h2 className="mb-2 text-lg font-bold">Asset categories</h2>{assets.length ? assets.map((category) => <div className="flex justify-between gap-3 py-2 text-sm" key={category.asset_category}><span>{label(category.asset_category)}</span><strong>{money(category.value)}</strong></div>) : <p className="text-sm text-slate-600">No asset categories in this snapshot.</p>}</Panel>
      <Panel><h2 className="mb-2 text-lg font-bold">Liabilities</h2>{liabilities.length ? liabilities.map((category) => <div className="flex justify-between gap-3 py-2 text-sm" key={category.asset_category}><span>{label(category.asset_category)}</span><strong>{money(category.value)}</strong></div>) : <p className="text-sm text-slate-600">No liability categories in this snapshot.</p>}</Panel>
    </div>
    <Panel><h2 className="mb-3 text-lg font-bold">Net worth history</h2>{history.loading && !history.value ? "Loading history…" : history.value ? <HistoryChart points={history.value} /> : <Notice kind="error">{history.error}</Notice>}<a href={`${base}/history`} className="mt-3 inline-block text-sm font-bold text-blue-700 hover:underline">View full history</a></Panel>
    <div className="grid gap-4 lg:grid-cols-2"><Panel><h2 className="text-lg font-bold">Included assets</h2><SnapshotItems items={current.items.filter((item) => item.asset_category !== "liability")} /></Panel><Panel><h2 className="text-lg font-bold">Included liabilities</h2><SnapshotItems items={current.items.filter((item) => item.asset_category === "liability")} /></Panel></div>
  </div>;
}

function History() {
  const { value, loading, error, refresh } = useLoad(() => financeApi.history(), []);
  return <div className="space-y-4">
    <div className="flex items-center justify-between"><h2 className="text-2xl font-bold">Financial history</h2><button type="button" onClick={() => void refresh()} className="text-sm font-bold text-blue-700">Refresh</button></div>
    {loading && !value && <Panel>Loading persisted snapshots…</Panel>}
    {error && <Notice kind="error">{error}</Notice>}
    {value && !value.length && <Notice>No financial snapshots are available.</Notice>}
    {value && value.length > 0 && <><Panel><HistoryChart points={value} /></Panel><Panel className="overflow-x-auto"><table className="w-full min-w-[560px] text-left text-sm"><thead><tr className="border-b text-slate-600"><th className="p-2">Month</th><th className="p-2">Assets</th><th className="p-2">Liabilities</th><th className="p-2">Net worth</th><th className="p-2">Status</th></tr></thead><tbody>{[...value].reverse().map((point) => <tr className="border-b border-slate-100" key={point.snapshot_id}><td className="p-2">{point.snapshot_month}</td><td className="p-2 tabular-nums">{money(point.total_assets)}</td><td className="p-2 tabular-nums">{money(point.total_liabilities)}</td><td className="p-2 tabular-nums">{money(point.net_worth)}</td><td className="p-2">{point.status === "success" ? "Complete" : <span className="font-semibold text-amber-800">{label(point.status)} · may be incomplete</span>}</td></tr>)}</tbody></table></Panel><p className="text-sm text-slate-600">Historical account components are not provided by this API. The Dashboard shows components of the latest snapshot only.</p></>}
  </div>;
}

function Accounts() {
  const accounts = useLoad(financeApi.accounts, []);
  const summary = useLoad(financeApi.summary, []);
  const [role, setRole] = useState<string | null>(null);
  useEffect(() => { void financeApi.role().then((user) => setRole(user.role)).catch(() => setRole(null)); }, []);
  const manual = useLoad<ManualAccount[]>(
    () => role === "admin" ? financeApi.manualAccounts(true) : Promise.resolve([]),
    [role],
  );
  const snapshotItems = summary.value?.items ?? [];
  const accountValue = (account: FinanceAccount) => snapshotItems.find((item) => item.source === account.source && item.source_account_id === account.source_account_id)?.value;
  const knownAccounts = new Set(accounts.value?.map((account) => `${account.source}:${account.source_account_id}`));
  const knownManual = new Set(manual.value?.map((account) => account.id));
  const valuationItems = snapshotItems.filter((item) =>
    !knownAccounts.has(`${item.source}:${item.source_account_id}`) &&
    !(item.source === "manual" && item.source_account_id && knownManual.has(item.source_account_id)));
  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-2xl font-bold">Account directory</h2>{role === "admin" && <a className="font-bold text-blue-700" href={`${base}/manual`}>Manage manual accounts</a>}</div>
    <Notice>Balances below are from the latest included monthly snapshot, when an exact account match exists. A missing snapshot balance is unavailable, not zero. Connection status and live refresh are not supplied by the API.</Notice>
    {summary.value && <Panel><h3 className="font-bold">Other included snapshot sources · {summary.value.snapshot_month}</h3><p className="text-xs text-slate-600">Automated valuations and sources without a matching directory record</p><SnapshotItems items={valuationItems} /></Panel>}
    {summary.error && <Notice kind="warning">Snapshot values unavailable: {summary.error}</Notice>}
    {accounts.loading && !accounts.value && <Panel>Loading configured accounts…</Panel>}
    {accounts.error && <Notice kind="error">{accounts.error}</Notice>}
    {accounts.value && !accounts.value.length && <Notice>No configured financial accounts were returned.</Notice>}
    {accounts.value && <div className="grid gap-3 md:grid-cols-2">{accounts.value.map((account) => <Panel key={account.id}><div className="flex justify-between gap-2"><h3 className="font-bold">{account.display_name || account.name}</h3><span className="font-semibold tabular-nums">{money(accountValue(account))}</span></div><p className="mt-1 text-sm text-slate-600">{account.institution_label || label(account.source)} · {label(account.asset_category)}{account.mask ? ` · ••••${account.mask}` : ""}</p><p className="text-xs text-slate-600">{label(account.source)} · {account.include_in_net_worth ? "Included in future snapshots" : "Excluded from future snapshots"} · Metadata updated {dateText(account.updated_at)}</p><a href={`${base}/accounts/${encodeURIComponent(account.id)}`} className="mt-3 inline-block text-sm font-bold text-blue-700">View and edit metadata</a></Panel>)}</div>}
    {role === "admin" && <><h3 className="text-lg font-bold">Manual records</h3>{manual.error && <Notice kind="error">{manual.error}</Notice>}{manual.loading && !manual.value && <Panel>Loading manual records…</Panel>}<div className="grid gap-3 md:grid-cols-2">{manual.value?.map((account) => {
      const published = snapshotItems.find((item) => item.source === "manual" && item.source_account_id === account.id);
      return <Panel key={account.id}><div className="flex justify-between gap-2"><h4 className="font-bold">{account.name}</h4><span className="font-semibold tabular-nums">{money(account.current_balance)}</span></div><p className="text-sm text-slate-600">Manual record · {label(account.classification)} · {label(account.asset_category)} · {account.active ? "Active" : "Archived"}</p><p className="text-xs text-slate-600">As of {account.as_of_date} · {published ? `Included in ${summary.value?.snapshot_month} snapshot: ${money(published.value)}` : "No matching included snapshot item"}</p><a className="mt-3 inline-block text-sm font-bold text-blue-700" href={`${base}/manual/${account.id}`}>View and edit</a></Panel>;
    })}</div><p className="text-sm text-slate-600">Current manual balances are editable records and may differ from the published snapshot. Provider valuations and manual fallbacks follow the backend's snapshot selection; directory values are never added together.</p></>}
  </div>;
}

function AccountDetail({ id }: { id: string }) {
  const state = useLoad(financeApi.accounts, []);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const account = state.value?.find((entry) => entry.id === id);
  const [draft, setDraft] = useState<FinanceAccount | null>(null);
  useEffect(() => { if (account) setDraft(account); }, [account]);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!draft || !account) return;
    setError(null); setSuccess(false);
    if ((account.display_name && !draft.display_name) ||
        (account.display_order !== null && draft.display_order === null)) {
      setError("The current API cannot clear a display name or order. Enter a replacement value.");
      return;
    }
    const patch = {
      ...(draft.include_in_net_worth !== account.include_in_net_worth ? { include_in_net_worth: draft.include_in_net_worth } : {}),
      ...(draft.asset_category !== account.asset_category ? { asset_category: draft.asset_category } : {}),
      ...(draft.display_name !== account.display_name && draft.display_name ? { display_name: draft.display_name } : {}),
      ...(draft.display_order !== account.display_order && draft.display_order !== null ? { display_order: draft.display_order } : {}),
    };
    if (!Object.keys(patch).length) {
      setError("No changes to save.");
      return;
    }
    try {
      await financeApi.updateAccount(id, patch);
      await state.refresh();
      setSuccess(true);
    } catch (caught) { setError(errorMessage(caught)); }
  };
  return <div className="space-y-4"><a className="text-sm font-bold text-blue-700" href={`${base}/accounts`}>← Accounts</a><h2 className="text-2xl font-bold">Configured account</h2>{state.loading && !state.value && <Panel>Loading account…</Panel>}{state.error && <Notice kind="error">{state.error}</Notice>}{state.value && !account && <Notice kind="error">Account not found.</Notice>}{draft && <Panel><h3 className="font-bold">{account?.name}</h3><p className="text-sm text-slate-600">{account?.institution_label || label(account?.source || "unknown")} · {account?.type || "Type unavailable"} {account?.subtype || ""}</p><form onSubmit={(event) => void submit(event)} className="mt-4 grid max-w-lg gap-3"><Field title="Display name"><input className="input" value={draft.display_name ?? ""} onChange={(event) => setDraft({ ...draft, display_name: event.target.value })} /></Field><Field title="Backend category"><input className="input" value={draft.asset_category} onChange={(event) => setDraft({ ...draft, asset_category: event.target.value })} required /></Field><Field title="Display order"><input className="input" type="number" value={draft.display_order ?? ""} onChange={(event) => setDraft({ ...draft, display_order: event.target.value === "" ? null : Number(event.target.value) })} /></Field><label className="flex gap-2 text-sm"><input type="checkbox" checked={draft.include_in_net_worth} onChange={(event) => setDraft({ ...draft, include_in_net_worth: event.target.checked })} />Include in future snapshots</label>{error && <Notice kind="error">{error}</Notice>}{success && <Notice>Account metadata saved. Published snapshots refresh on their normal schedule.</Notice>}<button className="button" type="submit">Save account metadata</button></form></Panel>}</div>;
}

function Field({ title, children }: { title: string; children: ReactNode }) {
  return <label className="grid gap-1 text-sm font-semibold text-slate-700"><span>{title}</span>{children}</label>;
}

function ManualList() {
  const state = useLoad(() => financeApi.manualAccounts(true), []);
  const [showArchived, setShowArchived] = useState(false);
  return <div className="space-y-4"><div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-2xl font-bold">Manual accounts</h2><a className="button" href={`${base}/manual/new`}>Add manual account</a></div><Notice>These are current editable records. Updating one does not recalculate a published net-worth snapshot.</Notice><label className="flex gap-2 text-sm"><input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} />Show archived</label>{state.loading && !state.value && <Panel>Loading manual accounts…</Panel>}{state.error && <Notice kind="error">{state.error}</Notice>}{state.value && !state.value.filter((account) => showArchived || account.active).length && <Notice>No manual accounts in this view.</Notice>}<div className="grid gap-3 md:grid-cols-2">{state.value?.filter((account) => showArchived || account.active).map((account) => <Panel key={account.id}><div className="flex justify-between gap-2"><h3 className="font-bold">{account.name}</h3><strong>{money(account.current_balance)}</strong></div><p className="text-sm text-slate-600">{label(account.classification)} · {label(account.asset_category)} · As of {account.as_of_date}</p><p className="text-xs text-slate-600">{account.active ? "Active" : "Archived"} · {account.include_in_net_worth ? "Included in future snapshots" : "Excluded from future snapshots"}</p><a className="mt-3 inline-block text-sm font-bold text-blue-700" href={`${base}/manual/${account.id}`}>View and edit</a></Panel>)}</div></div>;
}

const newManual: ManualAccountCreate = {
  name: "", classification: "asset", asset_category: "other", current_balance: "",
  as_of_date: new Date().toISOString().slice(0, 10), include_in_net_worth: true, notes: null,
};

function ManualEditor({ id }: { id?: string }) {
  const state = useLoad(() => id ? financeApi.manualAccount(id) : Promise.resolve(null), [id]);
  const [draft, setDraft] = useState<ManualAccountCreate>(newManual);
  const [active, setActive] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  useEffect(() => { if (state.value) { setDraft(state.value); setActive(state.value.active); } }, [state.value]);
  const save = async (event: FormEvent) => {
    event.preventDefault(); setError(null); setSuccess(null);
    try {
      const result = id ? await financeApi.updateManualAccount(id, { ...draft, active }) : await financeApi.createManualAccount(draft);
      if (id) await state.refresh();
      else { window.location.assign(`${base}/manual/${result.id}`); return; }
      setSuccess("Account saved. The published net-worth snapshot has not been recalculated; it updates on its normal schedule.");
    } catch (caught) { setError(errorMessage(caught)); }
  };
  const changeClassification = (classification: "asset" | "liability") => setDraft({ ...draft, classification, asset_category: classification === "liability" ? "liability" : "other" });
  return <div className="space-y-4"><a className="text-sm font-bold text-blue-700" href={`${base}/manual`}>← Manual accounts</a><h2 className="text-2xl font-bold">{id ? "Manual account detail" : "New manual account"}</h2>{state.loading && id && <Panel>Loading account…</Panel>}{state.error && <Notice kind="error">{state.error}</Notice>}{(!id || state.value) && <Panel><form className="grid max-w-xl gap-4" onSubmit={(event) => void save(event)}><Field title="Name"><input className="input" required maxLength={200} value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></Field><Field title="Classification"><select className="input" value={draft.classification} onChange={(event) => changeClassification(event.target.value as "asset" | "liability")}><option value="asset">Asset</option><option value="liability">Liability</option></select></Field><Field title="Category"><select className="input" value={draft.asset_category} onChange={(event) => setDraft({ ...draft, asset_category: event.target.value })}>{categories.filter((category) => draft.classification === "liability" ? category === "liability" : category !== "liability").map((category) => <option key={category} value={category}>{label(category)}</option>)}</select></Field><Field title="Current balance (nonnegative USD)"><input className="input" required inputMode="decimal" pattern="[0-9]+(\.[0-9]{1,2})?" value={draft.current_balance} onChange={(event) => setDraft({ ...draft, current_balance: event.target.value })} /></Field><Field title="As-of date"><input className="input" required type="date" max={new Date().toISOString().slice(0, 10)} value={draft.as_of_date} onChange={(event) => setDraft({ ...draft, as_of_date: event.target.value })} /></Field><Field title="Notes"><textarea className="input" maxLength={2000} value={draft.notes ?? ""} onChange={(event) => setDraft({ ...draft, notes: event.target.value || null })} /></Field><label className="flex gap-2 text-sm"><input type="checkbox" checked={draft.include_in_net_worth} onChange={(event) => setDraft({ ...draft, include_in_net_worth: event.target.checked })} />Include in future snapshots</label>{id && <label className="flex gap-2 text-sm"><input type="checkbox" checked={active} onChange={(event) => setActive(event.target.checked)} />Active (uncheck to archive)</label>}{state.value && <p className="text-sm text-slate-600">Updated {dateText(state.value.updated_at)} · ID: {state.value.id}</p>}{error && <Notice kind="error">{error}</Notice>}{success && <Notice>{success}</Notice>}<button className="button" type="submit">{id ? "Save changes" : "Create account"}</button></form></Panel>}</div>;
}

export default function FinanceScreen() {
  const path = window.location.pathname.replace(/\/$/, "");
  const relative = path.slice(base.length);
  const [role, setRole] = useState<string | null>(null);
  const [roleError, setRoleError] = useState<string | null>(null);
  useEffect(() => { void financeApi.role().then((user) => setRole(user.role)).catch((caught) => setRoleError(errorMessage(caught))); }, []);
  const manual = relative === "/manual" || relative.startsWith("/manual/");
  const navigation = [{ title: "Dashboard", href: base }, { title: "History", href: `${base}/history` }, { title: "Accounts", href: `${base}/accounts` }];
  if (role === "admin") navigation.push({ title: "Manual accounts", href: `${base}/manual` });
  return <main className="mx-auto max-w-7xl space-y-5 px-4 py-8"><header><h1 className="text-3xl font-black">Finance</h1><p className="mt-1 text-sm text-slate-600">Backend-published financial snapshots and account records</p></header><nav aria-label="Finance" className="flex flex-wrap gap-2">{navigation.map((entry) => <a key={entry.href} href={entry.href} aria-current={path === entry.href ? "page" : undefined} className={`rounded-lg px-3 py-2 text-sm font-semibold ${path === entry.href ? "bg-slate-950 text-white" : "bg-white text-slate-700 hover:bg-slate-200"}`}>{entry.title}</a>)}</nav>{roleError && <Notice kind="error">{roleError}</Notice>}{manual && role === null && !roleError && <Panel>Checking administrator access…</Panel>}{manual && role !== null && role !== "admin" && <Notice kind="error">Administrator access is required to manage manual accounts.</Notice>}{manual && role === "admin" && (relative === "/manual" ? <ManualList /> : <ManualEditor id={relative === "/manual/new" ? undefined : decodeURIComponent(relative.slice("/manual/".length))} />)}{!manual && (relative === "/history" ? <History /> : relative === "/accounts" ? <Accounts /> : relative.startsWith("/accounts/") ? <AccountDetail id={decodeURIComponent(relative.slice("/accounts/".length))} /> : <Dashboard />)}</main>;
}

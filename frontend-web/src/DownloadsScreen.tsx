import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";

import { downloadsApi } from "./api/downloadsApi";
import type { DownloadRequest, SubmissionResponse } from "./api/downloadsApi";

const PAGE_SIZE = 25;
const MAX_OFFSET = 10000;
const labels: Record<string, string> = {
  submitted: "Submitted", discovering: "Discovering links", queued: "Queued",
  downloading: "Downloading", download_complete: "Download complete",
  waiting_for_extraction: "Waiting for extraction", extracting: "Extracting",
  queued_for_plex: "Waiting for Plex", processing: "Moving to Plex",
  completed: "Completed", failed: "Failed", unavailable: "Status unavailable",
};

function safeText(value: unknown): string {
  if (typeof value !== "string") return "";
  return value
    .replace(/\b(?:https?|ftp):\/\/\S+|magnet:\?\S+/gi, "[link hidden]")
    .replace(/\b(?:password|token|secret|apikey|api_key)\s*[:=]\s*\S+/gi, "[sensitive detail hidden]");
}

function errorText(error: unknown): string {
  return safeText(error instanceof Error ? error.message : "Request failed. Try again.");
}

function dateText(value: string | null | undefined): string {
  if (!value) return "Unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
}

function sizeText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value < 0) return "Unknown";
  if (value === 0) return "0 B";
  const unit = Math.min(Math.floor(Math.log(value) / Math.log(1024)), 4);
  return `${(value / 1024 ** unit).toFixed(unit ? 1 : 0)} ${["B", "KiB", "MiB", "GiB", "TiB"][unit]}`;
}

function etaText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value < 0) return "Unknown";
  const seconds = Math.round(value);
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
}

function percent(item: { bytes_downloaded: number | null; total_bytes: number | null }): number | null {
  const { bytes_downloaded: loaded, total_bytes: total } = item;
  if (loaded === null || total === null || !Number.isFinite(loaded) || !Number.isFinite(total) || loaded < 0 || total <= 0) return null;
  return Math.min(100, Math.max(0, loaded / total * 100));
}

function Progress({ item }: { item: { bytes_downloaded: number | null; total_bytes: number | null } }) {
  const value = percent(item);
  return <div className="space-y-1">
    <div className="flex justify-between gap-2 text-xs text-slate-600">
      <span>{sizeText(item.bytes_downloaded)} / {sizeText(item.total_bytes)}</span>
      <span>{value === null ? "Size unknown" : `${value.toFixed(1)}%`}</span>
    </div>
    <div role="progressbar" aria-label="Download progress" aria-valuemin={value === null ? undefined : 0} aria-valuemax={value === null ? undefined : 100} aria-valuenow={value === null ? undefined : value} aria-valuetext={value === null ? "Total size unknown" : `${value.toFixed(1)} percent`} className="h-2.5 overflow-hidden rounded-full bg-slate-200">
      {value === null ? <div className="h-full w-1/3 rounded-full bg-slate-400" /> : <div className="h-full rounded-full bg-blue-600" style={{ width: `${value}%` }} />}
    </div>
  </div>;
}

function Panel({ title, count, children }: { title: string; count: number; children: ReactNode }) {
  return <section className="space-y-3" aria-label={title}>
    <h2 className="text-xl font-bold text-slate-950">{title} <span className="text-sm font-medium text-slate-500">({count})</span></h2>
    {count ? children : <p className="rounded-xl border border-slate-200 bg-white p-5 text-sm text-slate-600">No requests in this section.</p>}
  </section>;
}

function group(item: DownloadRequest): "active" | "processing" | "completed" | "attention" {
  if (item.legacy || item.state === "completed") return "completed";
  if (item.submission_outcome === "failed" || item.submission_outcome === "unknown" || item.state === "failed" || item.state === "unavailable") return "attention";
  if (["download_complete", "waiting_for_extraction", "extracting", "queued_for_plex", "processing"].includes(item.state)) return "processing";
  return "active";
}

function destinationNames(item: DownloadRequest): string[] {
  const paths = item.processing_files.filter((file) => file.state === "completed").map((file) => file.destination_path).filter((path): path is string => !!path);
  if (item.final_plex_destination) paths.push(item.final_plex_destination);
  return [...new Set(paths)].map((path) => path.split("/").filter(Boolean).pop() || path);
}

function RequestCard({ item, expanded, detail, detailLoading, detailError, onToggle }: {
  item: DownloadRequest;
  expanded: boolean;
  detail: DownloadRequest | null;
  detailLoading: boolean;
  detailError: string | null;
  onToggle: () => void;
}) {
  const shown = expanded && detail?.request_id === item.request_id ? detail : item;
  const destinations = destinationNames(shown);
  const state = shown.legacy ? "Historical status unknown" : labels[shown.state] || "Status unavailable";
  return <article className="min-w-0 rounded-xl border border-slate-200 bg-white p-4 shadow-sm md:p-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 flex-1">
        <h3 className="break-words text-base font-bold text-slate-950">{safeText(shown.name)}</h3>
        <p className="mt-1 text-sm text-slate-600">{shown.category === "TV" ? "TV" : "Movies"} · Submitted {dateText(shown.requested_at)}</p>
      </div>
      <span className={`rounded-full px-2.5 py-1 text-xs font-bold ${group(shown) === "attention" ? "bg-red-100 text-red-800" : group(shown) === "completed" ? "bg-green-100 text-green-800" : "bg-blue-100 text-blue-800"}`}>{state}</span>
    </div>
    {group(shown) === "active" && <div className="mt-4"><Progress item={shown} /><div className="mt-2 grid grid-cols-2 gap-2 text-xs text-slate-600 sm:grid-cols-4"><span>Speed: {shown.download_speed === null ? "Unknown" : `${sizeText(shown.download_speed)}/s`}</span><span>ETA: {etaText(shown.eta)}</span><span>Files found: {shown.discovered_files}</span><span>Files downloaded: {shown.finished_files}</span></div></div>}
    {group(shown) === "processing" && <p className="mt-3 text-sm text-amber-900">Transfer may be complete; Plex placement is awaiting verification.{shown.state === "waiting_for_extraction" && " Archive outputs are not yet resolved."}</p>}
    {group(shown) === "completed" && <p className="mt-3 text-sm text-slate-700">{shown.legacy ? "Legacy request; final outcome unknown." : `Plex completed ${dateText(shown.completed_at)} · ${shown.completed_file_count} processed file(s).`} {destinations.length > 0 && `Destinations: ${destinations.join(", ")}`}</p>}
    {(shown.failure || shown.submission_outcome === "unknown" || shown.truncated) && <p role="alert" className="mt-3 break-words rounded-md bg-amber-50 p-2 text-sm text-amber-900">{shown.failure ? safeText(shown.failure) : shown.submission_outcome === "unknown" ? "Submission outcome is uncertain. Check JDownloader before submitting again." : "JD2 results were truncated; live status may be incomplete."}</p>}
    <button type="button" aria-expanded={expanded} onClick={onToggle} className="mt-4 rounded-md border border-slate-300 px-3 py-2 text-sm font-semibold text-blue-700 hover:bg-slate-50">{expanded ? "Hide details" : "View details and files"}</button>
    {expanded && <div className="mt-4 space-y-4 border-t border-slate-200 pt-4 text-sm text-slate-700">
      {detailLoading && <p role="status">Refreshing request details…</p>}
      {detailError && <p role="alert" className="text-red-800">{detailError}</p>}
      <dl className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        <div><dt className="font-semibold">Request ID</dt><dd className="break-all">{shown.request_id}</dd></div>
        <div><dt className="font-semibold">Submission</dt><dd>{shown.submission_outcome || "Legacy / unknown"}</dd></div>
        <div><dt className="font-semibold">JD2 job ID</dt><dd>{shown.jd2_job_id ?? "Unavailable"}</dd></div>
        <div><dt className="font-semibold">Extraction</dt><dd>{shown.extraction_status.length ? shown.extraction_status.map(safeText).join(", ") : "No verified status"}</dd></div>
        <div><dt className="font-semibold">Plex processing</dt><dd>{shown.processing_status || "Not yet reported"}</dd></div>
        <div><dt className="font-semibold">JD2 entries</dt><dd>{shown.jd2_entries_removed ? "Removed" : "No removal recorded"}</dd></div>
        <div><dt className="font-semibold">Cleanup</dt><dd>{safeText(shown.cleanup_reason)}</dd></div>
      </dl>
      <div><h4 className="font-bold">JD2 files ({shown.files.length})</h4>{shown.files.length ? <ul className="mt-2 space-y-2">{shown.files.map((file, index) => <li key={`${file.id ?? index}`} className="min-w-0 rounded-lg bg-slate-50 p-3"><div className="flex flex-wrap justify-between gap-2"><span className="break-all font-medium">{safeText(file.name) || "Unnamed file"}</span><span>{file.finished ? "Downloaded" : safeText(file.status)}</span></div><div className="mt-2"><Progress item={file} /></div><p className="mt-1 text-xs">Speed: {file.download_speed === null ? "Unknown" : `${sizeText(file.download_speed)}/s`} · ETA: {etaText(file.eta)} · Extraction: {safeText(file.extraction_status) || "Unknown"}</p></li>)}</ul> : <p className="mt-1 text-slate-500">No live file details available.</p>}</div>
      <div><h4 className="font-bold">Plex files ({shown.processing_files.length})</h4>{shown.processing_files.length ? <ul className="mt-2 space-y-2">{shown.processing_files.map((file) => <li key={file.id} className="min-w-0 rounded-lg bg-slate-50 p-3"><p className="font-semibold">{safeText(file.destination_path?.split("/").pop() || file.source_path?.split("/").pop()) || "Unnamed file"} · {safeText(file.state)}</p><p>Completed: {dateText(file.completed_at)}</p>{file.destination_path && <p className="break-all">Destination: {safeText(file.destination_path)}</p>}{file.error && <p role="alert" className="break-words text-red-800">{safeText(file.error)}</p>}</li>)}</ul> : <p className="mt-1 text-slate-500">No verified Plex file results yet.</p>}</div>
    </div>}
  </article>;
}

export default function DownloadsScreen() {
  const [pages, setPages] = useState<Map<number, DownloadRequest[]>>(new Map());
  const pagesRef = useRef<Map<number, DownloadRequest[]>>(new Map());
  const [available, setAvailable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<DownloadRequest | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [showForm, setShowForm] = useState(false);
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [category, setCategory] = useState<"Movies" | "TV">("Movies");
  const [submitting, setSubmitting] = useState(false);
  const [submission, setSubmission] = useState<SubmissionResponse | null>(null);
  const [submittedCount, setSubmittedCount] = useState(0);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [sectionFilter, setSectionFilter] = useState("all");
  const [historyPage, setHistoryPage] = useState(0);
  const inFlight = useRef(false);
  const listController = useRef<AbortController | null>(null);
  const pollCount = useRef(0);

  const refresh = useCallback(async (full = false) => {
    if (inFlight.current) return;
    if (document.hidden) return;
    inFlight.current = true;
    const controller = new AbortController();
    listController.current = controller;
    setRefreshing(true);
    setError(null);
    try {
      const old = pagesRef.current;
      const next = full || old.size === 0 ? new Map<number, DownloadRequest[]>() : new Map(old);
      const offsets = full || old.size === 0 ? null : [...new Set([0, ...[...old.entries()].filter(([, rows]) => rows.some((row) => ["active", "processing", "attention"].includes(group(row)))).map(([offset]) => offset)])].sort((a, b) => a - b);
      let offset = 0;
      let jd2Available = true;
      while (offsets ? offset < offsets.length : offset <= MAX_OFFSET) {
        const pageOffset = offsets ? offsets[offset] : offset;
        const response = await downloadsApi.list(pageOffset, controller.signal);
        jd2Available = jd2Available && response.jd2_available;
        next.set(pageOffset, response.data);
        if (!offsets && response.data.length < PAGE_SIZE) break;
        offset = offsets ? offset + 1 : offset + PAGE_SIZE;
      }
      if (controller.signal.aborted) return;
      pagesRef.current = next;
      setPages(next);
      setAvailable(jd2Available);
      setLastUpdated(new Date());
      setVersion((current) => current + 1);
    } catch (caught) {
      if (!controller.signal.aborted) setError(errorText(caught));
    } finally {
      if (listController.current === controller) {
        inFlight.current = false;
        listController.current = null;
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    void refresh(true);
    const visible = () => { if (!document.hidden) window.setTimeout(() => { void refresh(true); }, 0); else listController.current?.abort(); };
    document.addEventListener("visibilitychange", visible);
    return () => { document.removeEventListener("visibilitychange", visible); listController.current?.abort(); listController.current = null; inFlight.current = false; };
  }, [refresh]);

  const requests = useMemo(() => {
    const unique = new Map<string, DownloadRequest>();
    for (const [, rows] of [...pages.entries()].sort(([a], [b]) => a - b)) {
      for (const row of rows) if (!unique.has(row.request_id)) unique.set(row.request_id, row);
    }
    return [...unique.values()];
  }, [pages]);
  const hasWork = requests.some((item) => ["active", "processing"].includes(group(item)));
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      pollCount.current += 1;
      void refresh(pollCount.current % 6 === 0 || !hasWork);
    }, hasWork ? 10000 : 30000);
    return () => window.clearInterval(timer);
  }, [hasWork, refresh]);

  useEffect(() => {
    if (!expanded) { setDetail(null); return; }
    const controller = new AbortController();
    setDetailLoading(true);
    setDetailError(null);
    void downloadsApi.detail(expanded, controller.signal).then((response) => {
      if (!controller.signal.aborted) setDetail(response.data);
    }).catch((caught: unknown) => {
      if (!controller.signal.aborted) setDetailError(errorText(caught));
    }).finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [expanded, version]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting || !url.trim() || !name.trim()) return;
    setSubmitting(true);
    setSubmission(null);
    setSubmissionError(null);
    const urlCount = url.split(/\r?\n/).filter((line) => line.trim()).length;
    setSubmittedCount(urlCount);
    try {
      const result = await downloadsApi.submit({ url: url.trim(), category, name: name.trim() });
      setSubmission(result);
      if (result.success && result.count === urlCount && result.requests.every((request) => request.submission_outcome === "succeeded")) {
        setUrl("");
      }
      listController.current?.abort();
      window.setTimeout(() => { void refresh(true); }, 0);
    } catch (caught) {
      setSubmissionError(`${errorText(caught)} If the outcome is uncertain, check JDownloader before submitting again.`);
    } finally {
      setSubmitting(false);
    }
  };

  const filtered = requests.filter((item) => categoryFilter === "all" || item.category === categoryFilter);
  const sections = {
    active: filtered.filter((item) => group(item) === "active"),
    processing: filtered.filter((item) => group(item) === "processing"),
    completed: filtered.filter((item) => group(item) === "completed"),
    attention: filtered.filter((item) => group(item) === "attention"),
  };
  const completedPages = Math.ceil(sections.completed.length / PAGE_SIZE);
  const shownHistoryPage = Math.min(historyPage, Math.max(0, completedPages - 1));
  const renderCards = (items: DownloadRequest[]) => <div className="grid gap-3 lg:grid-cols-2">{items.map((item) => <RequestCard key={item.request_id} item={item} expanded={expanded === item.request_id} detail={detail} detailLoading={detailLoading} detailError={detailError} onToggle={() => { setDetail(null); setExpanded(expanded === item.request_id ? null : item.request_id); }} />)}</div>;

  return <main className="mx-auto max-w-7xl space-y-7 px-4 py-7 text-slate-950 md:px-6">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="text-3xl font-black">Downloads</h1><p className="mt-1 text-sm text-slate-600">Follow each request from submission to verified Plex placement.</p></div><div className="flex flex-wrap gap-2"><button type="button" onClick={() => setShowForm((value) => !value)} className="rounded-md bg-blue-700 px-4 py-2 text-sm font-bold text-white hover:bg-blue-800">{showForm ? "Close form" : "Add Download"}</button><button type="button" disabled={refreshing} onClick={() => void refresh(true)} className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-bold disabled:opacity-50">{refreshing ? "Refreshing…" : "Refresh"}</button></div></div>
    <div aria-live="polite" className="text-sm text-slate-600">{loading ? "Loading downloads…" : lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : "No refresh completed yet"}</div>
    {!available && <p role="status" className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">Live JD2 information is unavailable. Persisted request and Plex history remain visible; missing metrics are unknown.</p>}
    {error && <p role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</p>}
    {showForm && <section aria-label="Add Download" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm md:p-5"><h2 className="mb-4 text-xl font-bold">Add Download</h2><form onSubmit={submit} className="grid gap-4 md:grid-cols-2"><label className="md:col-span-2"><span className="block text-sm font-semibold">Download URL (one per line)</span><textarea required rows={4} value={url} onChange={(event) => setUrl(event.target.value)} className="mt-1 w-full rounded-md border border-slate-300 p-3 text-base" /></label><label><span className="block text-sm font-semibold">Category</span><select value={category} onChange={(event) => setCategory(event.target.value as "Movies" | "TV")} className="mt-1 w-full rounded-md border border-slate-300 p-3 text-base"><option value="Movies">Movies</option><option value="TV">TV</option></select></label><label><span className="block text-sm font-semibold">Name</span><input required value={name} onChange={(event) => setName(event.target.value)} className="mt-1 w-full rounded-md border border-slate-300 p-3 text-base" /></label><div className="md:col-span-2"><button type="submit" disabled={submitting || !url.trim() || !name.trim()} className="rounded-md bg-blue-700 px-4 py-2.5 text-sm font-bold text-white disabled:opacity-50">{submitting ? "Submitting…" : "Submit"}</button></div></form>{submissionError && <p role="alert" className="mt-4 rounded-md bg-red-50 p-3 text-sm text-red-800">{submissionError}</p>}{submission && <div role="status" className={`mt-4 rounded-md p-3 text-sm ${submission.success && submission.count === submittedCount && submission.requests.every((item) => item.submission_outcome === "succeeded") ? "bg-green-50 text-green-900" : "bg-amber-50 text-amber-900"}`}><strong>{submission.success && submission.count === submittedCount && submission.requests.every((item) => item.submission_outcome === "succeeded") ? "Submission confirmed" : submission.requests.some((item) => item.submission_outcome === "succeeded") ? "Partial submission" : "Submission not confirmed"}</strong><p>{safeText(submission.message)} {submission.count} of {submittedCount} URL(s) recorded.</p><ul className="mt-2 space-y-1">{submission.requests.map((item) => <li key={item.request_id} className="break-words">Request {item.request_id} · {item.submission_outcome} · JD2 job {item.jd2_job_id ?? "unknown"} · {safeText(item.message)}</li>)}</ul>{submission.requests.some((item) => item.submission_outcome === "unknown") && <p className="mt-2 font-semibold">An unknown outcome may already have reached JDownloader. Investigate before submitting again.</p>}{!submission.success && <p className="mt-2 font-semibold">Some URLs may already be submitted. Review the results before using this form again.</p>}</div>}</section>}
    <div className="flex flex-wrap gap-3"><label className="text-sm font-semibold">Category <select value={categoryFilter} onChange={(event) => { setCategoryFilter(event.target.value); setHistoryPage(0); }} className="ml-2 rounded-md border border-slate-300 bg-white px-3 py-2"><option value="all">All</option><option value="Movies">Movies</option><option value="TV">TV</option></select></label><label className="text-sm font-semibold">Section <select value={sectionFilter} onChange={(event) => setSectionFilter(event.target.value)} className="ml-2 rounded-md border border-slate-300 bg-white px-3 py-2"><option value="all">All</option><option value="active">Active</option><option value="processing">Processing</option><option value="completed">Completed</option><option value="attention">Attention</option></select></label></div>
    {!loading && requests.length === 0 && !error && <p className="rounded-xl border border-slate-200 bg-white p-5 text-sm text-slate-600">No download requests yet. Use Add Download to submit one.</p>}
    {(sectionFilter === "all" || sectionFilter === "active") && <Panel title="Active Downloads" count={sections.active.length}>{renderCards(sections.active)}</Panel>}
    {(sectionFilter === "all" || sectionFilter === "processing") && <Panel title="Processing Queue" count={sections.processing.length}>{renderCards(sections.processing)}</Panel>}
    {(sectionFilter === "all" || sectionFilter === "attention") && <Panel title="Failed or Attention Required" count={sections.attention.length}>{renderCards(sections.attention)}</Panel>}
    {(sectionFilter === "all" || sectionFilter === "completed") && <Panel title="Completed Downloads and History" count={sections.completed.length}>{renderCards(sections.completed.slice(shownHistoryPage * PAGE_SIZE, (shownHistoryPage + 1) * PAGE_SIZE))}{completedPages > 1 && <nav aria-label="History pages" className="flex items-center gap-3"><button type="button" disabled={shownHistoryPage === 0} onClick={() => setHistoryPage(shownHistoryPage - 1)} className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm disabled:opacity-50">Previous</button><span className="text-sm">Page {shownHistoryPage + 1} of {completedPages}</span><button type="button" disabled={shownHistoryPage + 1 >= completedPages} onClick={() => setHistoryPage(shownHistoryPage + 1)} className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm disabled:opacity-50">Next</button></nav>}</Panel>}
  </main>;
}

import type { DownloadFile, DownloadRequest } from "./api/downloadsApi";

export type GroupSection = "active" | "processing" | "completed" | "attention" | "legacy";
export type GroupEntry = { file: DownloadFile; requests: DownloadRequest[] };
export type LogicalDownload = {
  key: string;
  name: string;
  category: string;
  entries: GroupEntry[];
  requests: DownloadRequest[];
  section: GroupSection;
  status: string;
  bytes_downloaded: number | null;
  total_bytes: number | null;
  percentage_complete: number | null;
  download_speed: number | null;
  eta: number | null;
  completed_at: string | null;
  truncated: boolean;
};

function validBytes(value: number | null): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function failureStatus(status: string): boolean {
  return /\b(error|failed|failure)\b/i.test(status);
}

function pausedStatus(status: string): boolean {
  return /\b(pause|paused|disabled)\b/i.test(status);
}

function attributableFailure(entry: GroupEntry): boolean {
  if (failureStatus(entry.file.status)) return true;
  if (entry.file.id === null) return false;
  return entry.requests.some((request) => request.processing_files?.some((result) =>
    result.state === "failed" && result.link_id !== null &&
    String(result.link_id) === String(entry.file.id)));
}

function crossPackageArchiveKeys(requests: DownloadRequest[]): Map<DownloadFile, string> {
  type Candidate = { file: DownloadFile; part: number; packageId: string; requestedAt: number };
  const sets = new Map<string, Candidate[]>();
  for (const request of requests) {
    const requestedAt = Date.parse(request.requested_at || "");
    if (request.legacy || !Number.isFinite(requestedAt) || !request.name.trim()) continue;
    for (const file of request.files) {
      if (file.source === "persisted" || file.id === null || file.job_id === null ||
          String(file.job_id) !== String(request.jd2_job_id) || file.package_id === null ||
          !file.save_to || !file.name) continue;
      const match = /^(.+?)\.part0*([1-9]\d*)\.rar$/i.exec(file.name);
      if (!match || !match[1].trim() || !Number.isSafeInteger(Number(match[2]))) continue;
      const key = JSON.stringify([request.category, request.name.trim().toLocaleLowerCase(),
        match[1].trim().toLocaleLowerCase(), file.save_to]);
      const candidates = sets.get(key) || [];
      candidates.push({ file, part: Number(match[2]), packageId: String(file.package_id), requestedAt });
      sets.set(key, candidates);
    }
  }
  const keys = new Map<DownloadFile, string>();
  for (const [key, candidates] of sets) {
    const parts = new Set<number>();
    const packages = new Set<string>();
    let earliest = Infinity;
    let latest = -Infinity;
    let largestPart = 0;
    for (const candidate of candidates) {
      parts.add(candidate.part);
      packages.add(candidate.packageId);
      earliest = Math.min(earliest, candidate.requestedAt);
      latest = Math.max(latest, candidate.requestedAt);
      largestPart = Math.max(largestPart, candidate.part);
    }
    if (candidates.length < 2 || packages.size < 2 ||
        latest - earliest > 10 * 60 * 1000 || parts.size !== candidates.length ||
        !parts.has(1) || largestPart !== candidates.length) continue;
    for (const candidate of candidates) keys.set(candidate.file, `multipart:${key}`);
  }
  return keys;
}

function groupKey(request: DownloadRequest, file: DownloadFile, crossKeys?: Map<DownloadFile, string>): string {
  return `${request.legacy ? "legacy" : "modern"}:${request.category}:${crossKeys?.get(file) || file.logical_group_key || `request:${request.request_id}`}`;
}

function completionTime(entry: GroupEntry, key: string, crossKeys: Map<DownloadFile, string>): string | null {
  const times: string[] = [];
  for (const request of entry.requests) {
    const results = entry.file.id === null ? [] : (request.processing_files || []).filter((result) =>
      result.link_id !== null && String(result.link_id) === String(entry.file.id));
    if (results.length) {
      if (results.every((result) => result.state === "completed" && result.completed_at &&
          Number.isFinite(Date.parse(result.completed_at)))) {
        times.push(...results.map((result) => result.completed_at as string));
      }
    } else if (request.state === "completed" && request.completed_at &&
        Number.isFinite(Date.parse(request.completed_at)) && request.files.length > 0 &&
        request.files.every((file) => groupKey(request, file, crossKeys) === key)) {
      // A request event only proves completion when it has no other logical card.
      times.push(request.completed_at);
    }
  }
  return times.length ? times.reduce((latest, value) =>
    Date.parse(value) > Date.parse(latest) ? value : latest) : null;
}

export function groupDownloads(requests: DownloadRequest[]): LogicalDownload[] {
  const groups = new Map<string, LogicalDownload>();
  const crossKeys = crossPackageArchiveKeys(requests);
  for (const request of requests) {
    for (const file of request.files) {
      // An older API response keeps its request card rather than merging unrelated files.
      const key = groupKey(request, file, crossKeys);
      let group = groups.get(key);
      if (!group) {
        group = {
          key, name: file.logical_group_name || request.name, category: request.category,
          entries: [], requests: [], section: "active", status: "Queued",
          bytes_downloaded: null, total_bytes: null, percentage_complete: null,
          download_speed: null, eta: null, completed_at: null, truncated: false,
        };
        groups.set(key, group);
      }
      group.entries.push({ file, requests: [request] });
      if (!group.requests.some((row) => row.request_id === request.request_id)) group.requests.push(request);
    }
  }
  for (const group of groups.values()) {
    // A JD2 link UUID identifies one download even if it appears in two request snapshots.
    const seen = new Map<string, GroupEntry>();
    const unique: GroupEntry[] = [];
    for (const entry of group.entries) {
      if (entry.file.id === null) {
        unique.push(entry); // No link identity: equal names are not proof of duplication.
        continue;
      }
      const key = String(entry.file.id);
      const existing = seen.get(key);
      if (existing) {
        const request = entry.requests[0];
        if (!existing.requests.some((row) => row.request_id === request.request_id)) existing.requests.push(request);
      } else {
        seen.set(key, entry);
        unique.push(entry);
      }
    }
    group.entries = unique;
    const files = group.entries.map(({ file }) => file);
    group.truncated = group.requests.some((request) => request.truncated || request.jd2_unavailable) ||
      files.some((file) => file.source === "persisted");
    const allKnown = !group.truncated && files.every((file) =>
      validBytes(file.bytes_downloaded) && validBytes(file.total_bytes) &&
      file.total_bytes > 0 && file.bytes_downloaded <= file.total_bytes);
    group.bytes_downloaded = !group.truncated && files.every((file) => validBytes(file.bytes_downloaded))
      ? files.reduce((sum, file) => sum + (file.bytes_downloaded || 0), 0) : null;
    group.total_bytes = allKnown ? files.reduce((sum, file) => sum + (file.total_bytes || 0), 0) : null;
    group.percentage_complete = group.total_bytes && group.bytes_downloaded !== null
      ? group.bytes_downloaded / group.total_bytes * 100 : null;
    group.download_speed = !group.truncated && files.some((file) => validBytes(file.download_speed))
      ? files.reduce((sum, file) => sum + (validBytes(file.download_speed) ? file.download_speed : 0), 0) : null;
    const failed = group.entries.some(attributableFailure);
    const finished = files.every((file) => file.finished);
    const completionTimes = group.entries.map((entry) => completionTime(entry, group.key, crossKeys));
    const completed = finished && completionTimes.every((value) => value !== null);
    const legacy = group.requests.every((request) => request.legacy);
    const isPaused = (file: DownloadFile) => file.enabled === false || pausedStatus(file.status);
    const paused = !finished && files.every((file) => file.finished || isPaused(file));
    const anyPaused = files.some((file) => !file.finished && isPaused(file));
    const downloading = !finished && files.some((file) => validBytes(file.download_speed) && file.download_speed > 0);
    group.section = legacy ? "legacy" : failed ? "attention" : completed ? "completed" : group.truncated ? "attention" : finished ? "processing" : "active";
    group.status = legacy ? "Historical outcome unknown" : failed ? "Needs attention" : completed ? "Plex completed" : group.truncated ? "Status incomplete" :
      finished ? "Download complete · Plex pending" : paused ? "Paused / disabled" : downloading ? "Downloading" : "Queued";
    group.completed_at = completed && !legacy ? (completionTimes as string[]).reduce((latest, value) =>
      Date.parse(value) > Date.parse(latest) ? value : latest) : null;
    const allSpeedsKnown = files.every((file) => file.finished || validBytes(file.download_speed));
    group.eta = !failed && !anyPaused && !finished && allKnown && allSpeedsKnown && group.download_speed && group.download_speed > 0 &&
      group.total_bytes !== null && group.bytes_downloaded !== null
      ? Math.max(0, (group.total_bytes - group.bytes_downloaded) / group.download_speed) : null;
  }
  return [...groups.values()];
}

import { apiRequest } from "./authenticatedApi";

export type DownloadFile = {
  id: number | string | null;
  name: string | null;
  source: string;
  status: string;
  finished: boolean;
  bytes_downloaded: number | null;
  total_bytes: number | null;
  download_speed: number | null;
  eta: number | null;
  extraction_status: string | null;
  package_id: number | string | null;
  job_id: number | string | null;
  save_to?: string | null;
};

export type ProcessingFile = {
  id: string | number;
  link_id: string | number | null;
  source_path: string | null;
  destination_path: string | null;
  state: string;
  outcome: string | null;
  error: string | null;
  completed_at: string | null;
};

export type DownloadRequest = {
  request_id: string;
  jd2_job_id: string | number | null;
  name: string;
  category: string;
  requested_at: string | null;
  submission_outcome: string | null;
  state: string;
  lifecycle_state: string;
  discovered_files: number;
  finished_files: number;
  bytes_downloaded: number | null;
  total_bytes: number | null;
  percentage_complete: number | null;
  download_speed: number | null;
  eta: number | null;
  truncated: boolean;
  extraction_status: string[];
  files: DownloadFile[];
  file_count: number | null;
  completed_file_count: number;
  processing_status: string | null;
  final_plex_destination: string | null;
  completed_at: string | null;
  failure: string | null;
  cleanup_eligible: boolean;
  cleanup_reason: string;
  jd2_entries_removed: boolean;
  processing_files: ProcessingFile[];
  legacy: boolean;
};

export type DownloadList = {
  success: boolean;
  data: DownloadRequest[];
  limit: number;
  offset: number;
  jd2_available: boolean;
};

export type DownloadDetail = {
  success: boolean;
  data: DownloadRequest;
  jd2_available: boolean;
};

export type SubmissionResult = {
  request_id: string;
  jd2_job_id: string | number | null;
  submission_outcome: "succeeded" | "failed" | "unknown" | string;
  message: string;
};

export type SubmissionResponse = {
  success: boolean;
  message: string;
  count: number;
  requests: SubmissionResult[];
};

export const downloadsApi = {
  list: async (offset: number, signal?: AbortSignal) => {
    const result = await apiRequest<DownloadList>(
      `/plex/downloads?limit=25&offset=${offset}`,
      { cache: "no-store", signal },
    );
    if (!result.success || !Array.isArray(result.data)) throw new Error("Unable to load downloads.");
    return result;
  },
  detail: async (requestId: string, signal?: AbortSignal) => {
    const result = await apiRequest<DownloadDetail>(
      `/plex/downloads/${encodeURIComponent(requestId)}`,
      { cache: "no-store", signal },
    );
    if (!result.success || !result.data) throw new Error("Unable to load download details.");
    return result;
  },
  submit: (request: { url: string; category: "Movies" | "TV"; name: string }) =>
    apiRequest<SubmissionResponse>("/plex/addDownload", {
      method: "POST",
      body: JSON.stringify(request),
    }),
};

export type VerificationStatus = "pending" | "running" | "completed" | "partially_completed" | "failed";
export type ConnectionResult = {
  label: string; outcome: "VERIFIED" | "AUTH_REQUIRED" | "TRANSIENT" | "UNKNOWN";
  connection_status: "VERIFIED" | "AUTH_REQUIRED" | "UNKNOWN";
  inventory_status: "AVAILABLE" | "MISSING" | "UNKNOWN";
  method: string; checked_at: string; included_accounts: number;
  available_accounts: number; missing_accounts: number; invalid_cached_balances: number;
  live_balance_tested: boolean; issue_code: string | null;
  explanation: string; recommended_action: string | null;
};
export type VerificationSummary = Partial<{
  enabled_connections: number; connections_checked: number; connections_with_problems: number;
  connections_uncertain: number; included_accounts: number; accounts_with_actionable_issues: number;
  methods_used: string[]; live_balance_tested: boolean; complete: boolean;
}>;
export type VerificationExecution = {
  execution_id: string | null; trigger: "manual" | "scheduled"; status: VerificationStatus;
  requested_at: string | null; started_at: string | null; completed_at: string | null;
  reused: boolean; retry_at: string | null; summary: VerificationSummary;
  results: { connections: ConnectionResult[]; accounts: unknown[] }; error_code: string | null;
};
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string";
const nullable = (value: unknown): value is string | null => value === null || text(value);
const count = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const optionalCount = (value: unknown) => value === undefined || count(value);
const isConnection = (value: unknown): value is ConnectionResult => object(value) &&
  text(value.label) && ["VERIFIED", "AUTH_REQUIRED", "TRANSIENT", "UNKNOWN"].includes(String(value.outcome)) &&
  ["VERIFIED", "AUTH_REQUIRED", "UNKNOWN"].includes(String(value.connection_status)) &&
  ["AVAILABLE", "MISSING", "UNKNOWN"].includes(String(value.inventory_status)) &&
  text(value.method) && text(value.checked_at) &&
  [value.included_accounts, value.available_accounts, value.missing_accounts, value.invalid_cached_balances].every(count) &&
  typeof value.live_balance_tested === "boolean" && nullable(value.issue_code) &&
  text(value.explanation) && nullable(value.recommended_action);

export function parseVerification(value: unknown): VerificationExecution {
  if (!object(value) || value.success !== true || !object(value.data)) throw new Error("Verification response is incomplete.");
  const row = value.data;
  if (!nullable(row.execution_id) || !["manual", "scheduled"].includes(String(row.trigger)) ||
      !["pending", "running", "completed", "partially_completed", "failed"].includes(String(row.status)) ||
      !nullable(row.requested_at) || !nullable(row.started_at) || !nullable(row.completed_at) ||
      typeof row.reused !== "boolean" || !nullable(row.retry_at) || !nullable(row.error_code) ||
      !object(row.summary) || !object(row.results)) throw new Error("Verification response is incomplete.");
  const summary = row.summary;
  if (![summary.enabled_connections, summary.connections_checked, summary.connections_with_problems,
        summary.connections_uncertain, summary.included_accounts, summary.accounts_with_actionable_issues].every(optionalCount) ||
      !(summary.methods_used === undefined || Array.isArray(summary.methods_used) && summary.methods_used.every(text)) ||
      !(summary.live_balance_tested === undefined || typeof summary.live_balance_tested === "boolean") ||
      !(summary.complete === undefined || typeof summary.complete === "boolean") ||
      !(row.results.connections === undefined || Array.isArray(row.results.connections) && row.results.connections.every(isConnection)) ||
      !(row.results.accounts === undefined || Array.isArray(row.results.accounts))) throw new Error("Verification response is incomplete.");
  return { ...row, summary, results: {
    connections: (row.results.connections ?? []) as ConnectionResult[],
    accounts: (row.results.accounts ?? []) as unknown[],
  } } as VerificationExecution;
}

export const terminal = (status: VerificationStatus) =>
  status === "completed" || status === "partially_completed" || status === "failed";
export const runNotFoundMessage = "The saved verification run is unavailable. You can request another run when the server allows it.";

export function verificationMessage(error: unknown): string {
  if (error instanceof Error && error.name === "ApiCooldownError") {
    const retry = "retryAt" in error && typeof error.retryAt === "string" ? error.retryAt : null;
    return retry && !Number.isNaN(Date.parse(retry))
      ? `Please wait before starting another verification. Retry after ${new Date(retry).toLocaleString()}.`
      : "Please wait before starting another verification.";
  }
  if (error instanceof Error && ["ApiAuthenticationError", "ApiForbiddenError"].includes(error.name)) return error.message;
  if (error instanceof Error && error.name === "ApiHttpError" && "status" in error && error.status === 404) return runNotFoundMessage;
  if (error instanceof Error && error.message === "Verification response is incomplete.") return error.message;
  if (error instanceof DOMException && error.name === "AbortError") return "Verification request timed out. Check this run's status before retrying.";
  return "Connection verification is unavailable. Check your connection and try again. If a run was accepted, use its saved status before starting another.";
}

type VerificationApi = {
  startVerification: (signal: AbortSignal) => Promise<VerificationExecution>;
  verificationRun: (id: string, signal: AbortSignal) => Promise<VerificationExecution>;
};
type RunnerState = { execution: VerificationExecution | null; busy: boolean; error: string | null };
export class VerificationRunner {
  private api: VerificationApi;
  private changed: (state: RunnerState) => void;
  private finished: () => void;
  private pollInterval: number;
  private requestTimeout: number;
  private active = false;
  private controller: AbortController | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private generation = 0;
  readonly state: RunnerState = { execution: null, busy: false, error: null };
  constructor(api: VerificationApi, changed: (state: RunnerState) => void,
              finished: () => void, pollInterval = 3000, requestTimeout = 20_000) {
    this.api = api; this.changed = changed; this.finished = finished;
    this.pollInterval = pollInterval; this.requestTimeout = requestTimeout;
  }
  private emit() { this.changed({ ...this.state }); }
  private async request<T>(call: (signal: AbortSignal) => Promise<T>): Promise<T> {
    const controller = new AbortController();
    this.controller = controller;
    const timeout = setTimeout(() => controller.abort(), this.requestTimeout);
    try { return await call(controller.signal); }
    finally { clearTimeout(timeout); if (this.controller === controller) this.controller = null; }
  }
  private received(row: VerificationExecution, generation: number) {
    if (!this.active || this.generation !== generation) return;
    this.state.execution = row; this.state.error = null; this.emit();
    if (terminal(row.status)) {
      this.active = false; this.state.busy = false; this.emit(); this.finished();
    } else if (!row.execution_id) {
      this.active = false; this.state.busy = false;
      this.state.error = "Connection verification is already running on the scheduled worker. Its execution status is unavailable here; refresh the dashboard later.";
      this.emit();
    } else {
      this.timer = setTimeout(() => void this.poll(row.execution_id!, generation), this.pollInterval);
    }
  }
  private async poll(id: string, generation: number) {
    if (!this.active || this.generation !== generation) return;
    try { this.received(await this.request((signal) => this.api.verificationRun(id, signal)), generation); }
    catch (error) { this.failed(error, generation); }
  }
  private failed(error: unknown, generation: number) {
    if (!this.active || this.generation !== generation) return;
    this.active = false; this.state.busy = false;
    if (error instanceof Error && error.name === "ApiHttpError" && "status" in error && error.status === 404) this.state.execution = null;
    this.state.error = verificationMessage(error); this.emit();
  }
  async start() {
    if (this.active) return;
    this.active = true; const generation = ++this.generation;
    this.state.busy = true; this.state.error = null; this.state.execution = null; this.emit();
    try { this.received(await this.request((signal) => this.api.startVerification(signal)), generation); }
    catch (error) { this.failed(error, generation); }
  }
  resume(id: string) {
    if (this.active || !/^[0-9a-f-]{36}$/i.test(id)) return;
    this.active = true; const generation = ++this.generation;
    this.state.busy = true; this.state.error = null; this.emit();
    void this.poll(id, generation);
  }
  cancel() {
    this.active = false; this.generation++;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null; this.controller?.abort(); this.controller = null;
  }
}

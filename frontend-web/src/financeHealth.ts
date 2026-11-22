export type Readiness = "READY" | "WARNING" | "BLOCKED" | "UNKNOWN";
export type Health = "HEALTHY" | "WARNING" | "BLOCKED" | "UNKNOWN";
export type Issue = { code: string; description: string; action: string | null };
export type AccountHealth = {
  account_id: string; display_name: string; category: string | null;
  source_type: "plaid" | "manual" | "vehicle" | "valuation";
  included_in_net_worth: boolean; health: Health;
  freshness: "CURRENT" | "STALE" | "UNKNOWN";
  last_successful_update: string | null; issues: Issue[];
  explanation: string; recommended_action: string | null;
};
export type Assessment = {
  readiness: Readiness; assessed_at: string; data_as_of: string | null;
  sufficient_evidence: boolean;
  counts: { healthy: number; warning: number; blocked: number; unknown: number };
  actionable_issues: Issue[]; accounts: AccountHealth[];
  snapshot: { readiness: Readiness; snapshot_month: string;
    blocking_conditions: Issue[]; warnings: Issue[]; explanation: string };
};
export type MonitorIssue = { issue_key: string; source_type: string; code: string;
  severity: "critical" | "warning"; occurrence: number; opened_at: string; last_seen_at: string };
export type MonitorEvent = { event_type: "opened" | "resolved" | "reminder" | "suppressed";
  issue_key: string | null; target_month: string | null; days_before: number | null; created_at: string };
export type Monitoring = { assessed_on: string | null; assessed_at: string | null;
  readiness: Readiness | null; previous_readiness: Readiness | null;
  summary: Record<string, unknown>; outstanding_issues: MonitorIssue[]; history: MonitorEvent[] };

const record = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const string = (value: unknown): value is string => typeof value === "string";
const nullableString = (value: unknown): value is string | null => value === null || string(value);
const integer = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const readiness = (value: unknown): value is Readiness =>
  value === "READY" || value === "WARNING" || value === "BLOCKED" || value === "UNKNOWN";
const health = (value: unknown): value is Health => value === "HEALTHY" || readiness(value) && value !== "READY";
const issue = (value: unknown): value is Issue => record(value) && string(value.code) &&
  string(value.description) && nullableString(value.action);
const issues = (value: unknown): value is Issue[] => Array.isArray(value) && value.every(issue);

export function parseAssessment(value: unknown): Assessment {
  if (!record(value) || value.success !== true || !record(value.data)) throw new Error("Preflight response is incomplete.");
  const data = value.data;
  const counts = data.counts;
  const snapshot = data.snapshot;
  if (!readiness(data.readiness) || !string(data.assessed_at) ||
      !nullableString(data.data_as_of) || typeof data.sufficient_evidence !== "boolean" ||
      !record(counts) || ![counts.healthy, counts.warning, counts.blocked, counts.unknown].every(integer) ||
      !issues(data.actionable_issues) || !Array.isArray(data.accounts) ||
      !data.accounts.every((row: unknown) => record(row) && string(row.account_id) &&
        string(row.display_name) && nullableString(row.category) &&
        ["plaid", "manual", "vehicle", "valuation"].includes(String(row.source_type)) &&
        typeof row.included_in_net_worth === "boolean" && health(row.health) &&
        ["CURRENT", "STALE", "UNKNOWN"].includes(String(row.freshness)) &&
        nullableString(row.last_successful_update) && issues(row.issues) &&
        string(row.explanation) && nullableString(row.recommended_action)) ||
      !record(snapshot) || !readiness(snapshot.readiness) || !string(snapshot.snapshot_month) ||
      !issues(snapshot.blocking_conditions) || !issues(snapshot.warnings) || !string(snapshot.explanation)) {
    throw new Error("Preflight response is incomplete.");
  }
  return data as unknown as Assessment;
}

export function parseMonitoring(value: unknown): Monitoring {
  if (!record(value) || value.success !== true || !record(value.data)) throw new Error("Monitoring response is incomplete.");
  const data = value.data;
  if (!nullableString(data.assessed_on) || !nullableString(data.assessed_at) ||
      !(data.readiness === null || readiness(data.readiness)) ||
      !(data.previous_readiness === null || readiness(data.previous_readiness)) ||
      !record(data.summary) || !Array.isArray(data.outstanding_issues) ||
      !data.outstanding_issues.every((row: unknown) => record(row) && string(row.issue_key) &&
        string(row.source_type) && string(row.code) && ["critical", "warning"].includes(String(row.severity)) &&
        integer(row.occurrence) && string(row.opened_at) && string(row.last_seen_at)) ||
      !Array.isArray(data.history) || !data.history.every((row: unknown) => record(row) &&
        ["opened", "resolved", "reminder", "suppressed"].includes(String(row.event_type)) &&
        nullableString(row.issue_key) && nullableString(row.target_month) &&
        (row.days_before === null || integer(row.days_before)) && string(row.created_at))) {
    throw new Error("Monitoring response is incomplete.");
  }
  return data as unknown as Monitoring;
}

export function accountTotals(accounts: AccountHealth[]) {
  const included = accounts.filter((account) => account.included_in_net_worth);
  return {
    healthy: included.filter((account) => account.health === "HEALTHY").length,
    attention: included.filter((account) => account.health === "WARNING" || account.health === "BLOCKED").length,
    blocked: included.filter((account) => account.health === "BLOCKED").length,
    unknown: included.filter((account) => account.health === "UNKNOWN").length,
  };
}

export function snapshotOnlyConditions(assessment: Assessment) {
  const accountIssues = assessment.accounts.filter((account) => account.included_in_net_worth)
    .flatMap((account) => account.issues);
  return [
    ...assessment.snapshot.blocking_conditions.map((entry) => ({ ...entry, kind: "blocker" as const })),
    ...assessment.snapshot.warnings.map((entry) => ({ ...entry, kind: "warning" as const })),
  ].filter((entry) => !accountIssues.some((accountIssue) =>
    accountIssue.code === entry.code && accountIssue.description === entry.description));
}

export function assessmentGuidance(assessment: Assessment) {
  const totals = accountTotals(assessment.accounts);
  const extra = snapshotOnlyConditions(assessment);
  const condition = assessment.readiness === "BLOCKED"
    ? extra.find((entry) => entry.kind === "blocker") ?? assessment.snapshot.blocking_conditions[0]
    : extra[0] ?? assessment.snapshot.warnings[0] ?? assessment.snapshot.blocking_conditions[0];
  const affected = totals.attention > 0
    ? `${totals.attention} included ${totals.attention === 1 ? "account needs" : "accounts need"} attention. ` : "";
  if (assessment.readiness === "BLOCKED") {
    const detail = condition?.description ?? assessment.snapshot.explanation;
    return {
      overview: `Current evidence has a verified snapshot blocker. ${detail}`,
      next: `${affected}A verified current-evidence blocker needs review before the next snapshot. ${detail}`,
    };
  }
  if (assessment.readiness === "UNKNOWN") {
    const detail = condition?.description;
    return {
      overview: `Current evidence is insufficient to confirm readiness.${detail ? ` ${detail}` : ""}`,
      next: `${affected}Current evidence is insufficient to confirm readiness.${detail ? ` ${detail}` : ""}`,
    };
  }
  if (assessment.readiness === "WARNING") {
    const detail = condition?.description ?? assessment.snapshot.explanation;
    return {
      overview: `Current evidence has a review concern. ${detail}`,
      next: `${affected}Current evidence has a concern to review before the next snapshot. ${detail}`,
    };
  }
  return {
    overview: "Current evidence shows no known account problems.",
    next: "Current evidence shows no known account problems; future collection is not guaranteed.",
  };
}

const rank: Record<Health, number> = { BLOCKED: 0, WARNING: 1, UNKNOWN: 2, HEALTHY: 3 };
export function sortedAccounts(accounts: AccountHealth[]) {
  return [...accounts].sort((a, b) =>
    Number(!a.included_in_net_worth) - Number(!b.included_in_net_worth) ||
    rank[a.health] - rank[b.health] || a.display_name.localeCompare(b.display_name));
}

export function nextSnapshot(now: Date) {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York",
    year: "numeric", month: "numeric", day: "numeric" }).formatToParts(now);
  const part = (type: string) => Number(parts.find((item) => item.type === type)?.value);
  const year = part("year"); const month = part("month");
  const target = new Date(Date.UTC(year, month, 1));
  const threeDays = new Date(target.getTime() - 3 * 86_400_000);
  const oneDay = new Date(target.getTime() - 86_400_000);
  return { target, threeDays, oneDay };
}

export function correctiveAction(account: AccountHealth, hasPlaidDirectory = false) {
  const codes = account.issues.map((entry) => entry.code);
  if ((account.source_type === "manual" || account.source_type === "vehicle") &&
      /^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(account.account_id) &&
      codes.some((code) => code.startsWith("MANUAL_") || code === "VALUATION_FALLBACK_INVALID")) {
    return { label: "Review value", href: `/portal/finance/manual/${encodeURIComponent(account.account_id)}` };
  }
  if (account.source_type === "plaid" && hasPlaidDirectory && codes.length) {
    return { label: "Review account", href: `/portal/finance/accounts/${encodeURIComponent(account.account_id)}` };
  }
  return null;
}

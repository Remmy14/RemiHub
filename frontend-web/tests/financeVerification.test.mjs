import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parseVerification, VerificationRunner, verificationMessage, runNotFoundMessage } from "../src/financeVerification.ts";
import { parseAssessment, accountTotals } from "../src/financeHealth.ts";

const id = "11111111-1111-4111-8111-111111111111";
const connection = (overrides = {}) => ({
  label: "Bank", outcome: "VERIFIED", connection_status: "UNKNOWN", inventory_status: "AVAILABLE",
  method: "accounts_get", checked_at: "2026-11-22T12:00:00Z", included_accounts: 2,
  available_accounts: 1, missing_accounts: 1, invalid_cached_balances: 0,
  live_balance_tested: false, issue_code: "PLAID_ACCOUNT_MISSING",
  explanation: "An included account is absent from cached inventory.",
  recommended_action: "Review the institution's account access.", ...overrides,
});
const execution = (status = "pending", overrides = {}) => ({ success: true, data: {
  execution_id: id, trigger: "manual", status, requested_at: "2026-11-22T12:00:00Z",
  started_at: null, completed_at: null, reused: false, retry_at: null,
  summary: {}, results: { connections: [], accounts: [] }, error_code: null, ...overrides,
} });
const delay = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));

test("parses deployed execution fields without inventing progress or completion", () => {
  const pending = parseVerification(execution());
  assert.deepEqual(pending.summary, {});
  assert.deepEqual(pending.results.connections, []);
  const partial = parseVerification(execution("partially_completed", {
    summary: { connections_checked: 1, connections_uncertain: 1, complete: false, live_balance_tested: false },
    results: { connections: [connection()], accounts: [] }, error_code: "VERIFICATION_EXECUTION_FAILED",
  }));
  assert.equal(partial.status, "partially_completed");
  assert.equal(partial.results.connections[0].missing_accounts, 1);
  assert.throws(() => parseVerification(execution("completed", { summary: { connections_checked: "0" } })), /incomplete/);
  assert.throws(() => parseVerification(execution("completed", { results: { connections: [{ label: "Bank" }] } })), /incomplete/);
  assert.throws(() => parseVerification({ success: true, data: {} }), /incomplete/);
});

test("runner prevents duplicate starts, polls sequentially, and retains partial results", async () => {
  let starts = 0; let polls = 0; let finishes = 0; const states = [];
  const runner = new VerificationRunner({
    startVerification: async () => { starts++; return parseVerification(execution()); },
    verificationRun: async () => {
      polls++;
      return parseVerification(execution(polls === 1 ? "running" : "partially_completed", {
        results: { connections: [connection()], accounts: [] },
        summary: polls === 1 ? {} : { connections_checked: 1, complete: false, live_balance_tested: false },
      }));
    },
  }, (state) => states.push(state), () => { finishes++; }, 1);
  await Promise.all([runner.start(), runner.start()]);
  await delay();
  assert.equal(starts, 1);
  assert.equal(polls, 2);
  assert.equal(finishes, 1);
  assert.equal(runner.state.execution.status, "partially_completed");
  assert.equal(runner.state.execution.results.connections.length, 1);
  assert.ok(states.some((state) => state.busy));
  assert.equal(runner.state.busy, false);
});

test("resume checks an existing ID without POST and cancellation stops polling", async () => {
  let starts = 0; let polls = 0;
  const runner = new VerificationRunner({
    startVerification: async () => { starts++; return parseVerification(execution()); },
    verificationRun: async () => { polls++; return parseVerification(execution("running")); },
  }, () => {}, () => {}, 20);
  runner.resume(id);
  await delay(5);
  runner.cancel();
  await delay(35);
  assert.equal(starts, 0);
  assert.equal(polls, 1);
});

test("scheduled reuse and cooldown do not produce a second client run", async () => {
  let polls = 0; const messages = [];
  const runner = new VerificationRunner({
    startVerification: async () => parseVerification(execution("running", { execution_id: null, trigger: "scheduled", reused: true })),
    verificationRun: async () => { polls++; return parseVerification(execution()); },
  }, (state) => messages.push(state.error), () => {}, 1);
  await runner.start();
  assert.equal(polls, 0);
  assert.match(messages.at(-1), /scheduled worker/);
  const cooldown = Object.assign(new Error("raw"), { name: "ApiCooldownError", retryAt: "2026-11-22T12:30:00Z" });
  assert.match(verificationMessage(cooldown), /Retry after/);
  assert.doesNotMatch(verificationMessage(new Error("internal credentials")), /credentials/);
  assert.match(verificationMessage(new DOMException("aborted", "AbortError")), /timed out/);
});

test("network timeout and failed execution are distinct from a healthy completion", async () => {
  const timeoutRunner = new VerificationRunner({
    startVerification: (signal) => new Promise((_, reject) => signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")))),
    verificationRun: async () => { throw new Error("unexpected poll"); },
  }, () => {}, () => {}, 1, 5);
  await timeoutRunner.start();
  assert.match(timeoutRunner.state.error, /timed out/);
  assert.equal(timeoutRunner.state.execution, null);
  let finished = 0;
  const failedRunner = new VerificationRunner({
    startVerification: async () => parseVerification(execution("failed", { error_code: "EXECUTION_INTERRUPTED", completed_at: "2026-11-22T12:01:00Z" })),
    verificationRun: async () => { throw new Error("unexpected poll"); },
  }, () => {}, () => { finished++; }, 1);
  await failedRunner.start();
  assert.equal(failedRunner.state.execution.status, "failed");
  assert.equal(finished, 1);
});

test("an unavailable saved run is cleared from runner state", async () => {
  const missing = Object.assign(new Error("not found"), { name: "ApiHttpError", status: 404 });
  const runner = new VerificationRunner({
    startVerification: async () => { throw new Error("unexpected POST"); },
    verificationRun: async () => { throw missing; },
  }, () => {}, () => {});
  runner.resume(id);
  await delay(5);
  assert.equal(runner.state.error, runNotFoundMessage);
  assert.equal(runner.state.execution, null);
});

test("per-account verification preserves a healthy sibling and UNKNOWN without live balance claims", () => {
  const row = (account_id, health, inventory_status) => ({
    account_id, display_name: `Bank ${account_id}`, category: "cash", source_type: "plaid",
    included_in_net_worth: true, health, freshness: "STALE", last_successful_update: "2026-11-01T00:00:00Z",
    last_monthly_balance_collection: "2026-11-01T00:00:00Z", issues: [], explanation: "Evidence only.", recommended_action: null,
    verification: { connection_status: "UNKNOWN", inventory_status, item_inventory_status: "MISSING",
      attempted_at: "2026-11-22T12:00:00Z", succeeded_at: "2026-11-22T12:00:00Z", method: "accounts_get",
      outcome: "VERIFIED", live_balance_tested: false, cached_balance_status: inventory_status === "AVAILABLE" ? "VALID" : "UNKNOWN",
      collection_readiness: inventory_status === "AVAILABLE" ? "CACHED_INVENTORY_AVAILABLE" : "UNKNOWN" },
  });
  const healthy = row("a", "HEALTHY", "AVAILABLE");
  const unknown = row("b", "UNKNOWN", "UNKNOWN");
  const assessment = parseAssessment({ success: true, data: {
    readiness: "READY", assessed_at: "2026-11-22T12:05:00Z", data_as_of: "2026-11-01T00:00:00Z",
    sufficient_evidence: true, counts: { healthy: 1, warning: 0, blocked: 0, unknown: 1 },
    actionable_issues: [], accounts: [healthy, unknown], snapshot: {
      readiness: "READY", snapshot_month: "2026-11-01", blocking_conditions: [], warnings: [], explanation: "Current snapshot evidence.",
    },
  } });
  assert.deepEqual(accountTotals(assessment.accounts), { healthy: 1, attention: 0, blocked: 0, unknown: 1 });
  assert.equal(assessment.accounts[0].verification.live_balance_tested, false);
  assert.equal(assessment.accounts[1].verification.inventory_status, "UNKNOWN");
  assert.notEqual(assessment.accounts[0].last_monthly_balance_collection, assessment.accounts[0].verification.attempted_at);
});

test("verification action only uses approved API routes and empty body", () => {
  const source = readFileSync(new URL("../src/api/financeApi.ts", import.meta.url), "utf8");
  const start = source.slice(source.indexOf("startVerification:"), source.indexOf("verificationRun:"));
  assert.match(start, /"\/finance\/verification\/run"/);
  assert.match(start, /method: "POST", body: "\{\}"/);
  assert.doesNotMatch(start, /snapshot|balance|notification|force|method.*accounts/);
});

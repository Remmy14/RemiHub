import assert from "node:assert/strict";
import test from "node:test";
import { accountTotals, assessmentGuidance, correctiveAction, nextSnapshot, parseAssessment, parseMonitoring, snapshotOnlyConditions, sortedAccounts } from "../src/financeHealth.ts";

const issue = (code = "MANUAL_REVIEW_DUE") => ({ code, description: "Review needed.", action: "Review value." });
const account = (overrides = {}) => ({
  account_id: "b0f18a35-eec0-4bea-ab71-a93cc080087e", display_name: "Long account name",
  category: "vehicle", source_type: "manual", included_in_net_worth: true,
  health: "WARNING", freshness: "STALE", last_successful_update: "2026-11-01",
  issues: [issue()], explanation: "Review needed.", recommended_action: "Review value.", ...overrides,
});
const assessment = (accounts, readiness = "WARNING") => ({ success: true, data: {
  readiness, assessed_at: "2026-11-21T12:00:00Z", data_as_of: "2026-11-01T12:00:00Z",
  sufficient_evidence: readiness !== "UNKNOWN",
  counts: { healthy: 0, warning: accounts.length, blocked: 0, unknown: 0 },
  actionable_issues: accounts.flatMap((row) => row.issues), accounts,
  snapshot: { readiness, snapshot_month: "2026-11-01", blocking_conditions: [],
    warnings: [], explanation: "Stored evidence only." },
}});

test("accepts all backend readiness states without treating UNKNOWN as healthy", () => {
  for (const state of ["READY", "WARNING", "BLOCKED", "UNKNOWN"]) {
    const row = account({ health: state === "READY" ? "HEALTHY" : state });
    assert.equal(parseAssessment(assessment([row], state)).readiness, state);
  }
  assert.equal(accountTotals([account({ health: "UNKNOWN" })]).unknown, 1);
  assert.equal(accountTotals([account({ health: "UNKNOWN" })]).healthy, 0);
});

test("counts distinct included accounts, not issue records or excluded issues", () => {
  const blocked = account({ health: "BLOCKED", issues: [issue("MANUAL_VALUE_MISSING"), issue()] });
  const excluded = account({ account_id: "excluded", included_in_net_worth: false, health: "BLOCKED" });
  const healthy = account({ account_id: "healthy", health: "HEALTHY", issues: [] });
  const rows = [healthy, excluded, blocked];
  assert.deepEqual(accountTotals(rows), { healthy: 1, attention: 1, blocked: 1, unknown: 0 });
  assert.equal(sortedAccounts(rows)[0], blocked);
  assert.equal(sortedAccounts(rows).at(-1), excluded);
});

test("snapshot-level blockers remain visible when all account rows look healthy", () => {
  const response = assessment([account({ health: "HEALTHY", issues: [] })], "BLOCKED");
  response.data.snapshot.blocking_conditions = [{
    code: "SNAPSHOT_INCOMPLETE", description: "Configured valuation is missing from current evidence.",
    action: "Review account inventory and valuations.",
  }];
  const current = parseAssessment(response);
  assert.equal(accountTotals(current.accounts).attention, 0);
  assert.deepEqual(snapshotOnlyConditions(current).map((entry) => entry.code), ["SNAPSHOT_INCOMPLETE"]);
  assert.match(assessmentGuidance(current).overview, /verified snapshot blocker.*Configured valuation/);
  assert.match(assessmentGuidance(current).next, /verified current-evidence blocker.*Configured valuation/);
  assert.doesNotMatch(assessmentGuidance(current).next, /no known account problems/);
});

test("zero account-level attention never turns WARNING or UNKNOWN into an all-clear", () => {
  for (const state of ["WARNING", "UNKNOWN"]) {
    const response = assessment([account({ health: "HEALTHY", issues: [] })], state);
    response.data.snapshot.warnings = [{ code: "CURRENT_COLLECTION_UNVERIFIED",
      description: "Current collection cannot be verified.", action: "Review after collection." }];
    const guidance = assessmentGuidance(parseAssessment(response));
    assert.match(guidance.overview, /Current collection cannot be verified/);
    assert.match(guidance.next, /Current collection cannot be verified/);
    assert.doesNotMatch(guidance.next, /no known account problems/);
  }
});

test("rejects incomplete assessment instead of displaying an empty success", () => {
  const response = assessment([account()]);
  delete response.data.counts;
  assert.throws(() => parseAssessment(response), /incomplete/);
  assert.throws(() => parseAssessment({ success: true, data: { accounts: [] } }), /incomplete/);
});

test("manual editor is addressable, Plaid metadata is not presented as reauthorization", () => {
  assert.equal(correctiveAction(account()).href,
    "/portal/finance/manual/b0f18a35-eec0-4bea-ab71-a93cc080087e");
  assert.equal(correctiveAction(account({ source_type: "plaid", issues: [issue("PLAID_REAUTH_REQUIRED")] }), true).label,
    "Review account");
  assert.equal(correctiveAction(account({ source_type: "plaid", issues: [issue("PLAID_REAUTH_REQUIRED")] })), null);
  assert.equal(correctiveAction(account({ source_type: "valuation", issues: [issue("VALUATION_VALUE_MISSING")] })), null);
});

test("monitoring contract retains nullable assessment and historical events", () => {
  const response = { success: true, data: { assessed_on: null, assessed_at: null,
    readiness: null, previous_readiness: null, summary: {}, outstanding_issues: [],
    history: [{ event_type: "resolved", issue_key: "manual:x:MANUAL_REVIEW_DUE",
      target_month: null, days_before: null, created_at: "2026-11-20T12:00:00Z" }] } };
  assert.equal(parseMonitoring(response).assessed_at, null);
  assert.equal(parseMonitoring(response).history[0].event_type, "resolved");
  assert.throws(() => parseMonitoring({ success: true, data: { ...response.data, history: null } }), /incomplete/);
});

test("New York target and reminders cross December, February, and UTC midnight", () => {
  const dates = [
    ["2026-12-31T23:30:00Z", "2027-01-01", "2026-12-29", "2026-12-31"],
    ["2027-01-31T23:00:00Z", "2027-02-01", "2027-01-29", "2027-01-31"],
    ["2027-02-28T23:00:00Z", "2027-03-01", "2027-02-26", "2027-02-28"],
    ["2027-03-01T02:00:00Z", "2027-03-01", "2027-02-26", "2027-02-28"],
  ];
  for (const [now, target, three, one] of dates) {
    const result = nextSnapshot(new Date(now));
    assert.equal(result.target.toISOString().slice(0, 10), target);
    assert.equal(result.threeDays.toISOString().slice(0, 10), three);
    assert.equal(result.oneDay.toISOString().slice(0, 10), one);
  }
});

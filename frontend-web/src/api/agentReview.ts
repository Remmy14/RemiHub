import type { AgentRun, AgentLatestRun } from "./agentApi";

export function reviewRunStatus(run: AgentRun | AgentLatestRun): string {
  if (run.phase !== "review") return run.status;
  if (run.status === "failed") return "review_execution_failed";
  const review = run.independent_review;
  if (run.status !== "succeeded" || !review) return run.status === "succeeded" ? "review_execution_finished" : run.status;
  const decision = review.decision ?? review.verdict;
  if (decision === "approve" || decision === "pass") return "review_approved";
  if (decision === "request_changes" || decision === "changes_requested") {
    const attempt = Number(review.review_attempt ?? 0);
    const allowance = Number(review.correction_allowance ?? 2);
    return attempt > allowance ? "automatic_corrections_paused" : "changes_requested";
  }
  return review.failure_type ? "review_execution_failed" : "historical_review_blocked";
}


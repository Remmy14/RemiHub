import { apiRequest } from "./authenticatedApi";

export const AgentActions = {
  ADD_FOLLOW_UP: "add_follow_up",
  APPROVE_IMPLEMENTATION: "approve_implementation",
  APPROVE_DEPLOYMENT: "approve_deployment",
  CONTINUE_CORRECTIONS: "continue_corrections",
  RETRY: "retry",
  RETURN_TO_IMPLEMENTATION: "return_to_implementation",
  RETRY_GITHUB_SYNC: "retry_github_sync",
  RETRY_REVIEW: "retry_review",
  CANCEL: "cancel",
  CLOSE: "close",
} as const;

export type AgentAction = (typeof AgentActions)[keyof typeof AgentActions];

export type BacklogPriority = "High" | "Medium" | "Low";
export type BacklogItem = {
  id: string;
  title: string;
  prompt: string;
  priority: BacklogPriority;
  created_by: string;
  promoted_card_id: string | null;
  promoted_at: string | null;
  created_at: string;
  updated_at: string;
  sort_order: number | null;
};
type BacklogItemResponse = { success: boolean; data: BacklogItem };
type BacklogListResponse = { success: boolean; data: BacklogItem[]; revision: number };
type BacklogPromotionResponse = { success: boolean; data: { backlog_item_id: string; card_id: string } };

export type BacklogOrder = "custom" | "priority" | "created_at";

export async function listBacklogItems(order: BacklogOrder = "custom"): Promise<BacklogListResponse> {
  const response = await apiRequest<BacklogListResponse>(`/agent/backlog?order=${order}`);
  return response;
}

export async function reorderBacklogItems(itemIds: string[], revision: number): Promise<number> {
  const response = await apiRequest<{ success: boolean; revision: number }>("/agent/backlog/order", {
    method: "PUT", body: JSON.stringify({ item_ids: itemIds, revision }),
  });
  return response.revision;
}

export async function getBacklogItem(id: string): Promise<BacklogItem> {
  const response = await apiRequest<BacklogItemResponse>(`/agent/backlog/${id}`);
  return response.data;
}

export async function saveBacklogItem(
  data: { title: string; prompt: string; priority: BacklogPriority }, id?: string,
): Promise<BacklogItem> {
  const response = await apiRequest<BacklogItemResponse>(id ? `/agent/backlog/${id}` : "/agent/backlog", {
    method: id ? "PUT" : "POST", body: JSON.stringify(data),
  });
  return response.data;
}

export async function deleteBacklogItem(id: string): Promise<void> {
  await apiRequest<void>(`/agent/backlog/${id}`, { method: "DELETE" });
}

export async function sendBacklogToPlanning(id: string): Promise<string> {
  const response = await apiRequest<BacklogPromotionResponse>(
    `/agent/backlog/${id}/send-to-planning`, { method: "POST" },
  );
  return response.data.card_id;
}

export type AgentLatestRun = {
  independent_review?: Record<string, unknown> | null;
  id: string;
  phase: string;
  status: string;
  card_revision: number;
  attempt_count: number;
  blocked_reason: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type AgentDeploymentRecovery = {
  github_sync_status: string;
  retryable: boolean;
  blocker_code: string | null;
  last_error: string | null;
  candidate_commit: string | null;
  deployment_run_id: string;
  production_deployed: boolean;
};

export type AgentCardSummary = {
  id: string;
  title: string;
  description: string;
  status: string;
  repository_scope: string;
  revision: number;
  base_branch: string;
  feature_branch: string | null;
  worktree_path: string | null;
  codex_thread_id: string | null;
  resume_status: string | null;
  blocked_reason: string | null;
  blocked_until: string | null;
  created_by: string;
  closed_at: string | null;
  created_at: string;
  updated_at: string;
  latest_run: AgentLatestRun | null;
  deployment_recovery: AgentDeploymentRecovery | null;
  allowed_actions: AgentAction[];
};

export type AgentMessage = {
  id: string;
  card_id: string;
  author_type: string;
  content: string;
  created_by: string | null;
  client_message_id: string | null;
  created_at: string;
};

export type AgentRun = {
  id: string;
  card_id: string;
  phase: string;
  status: string;
  card_revision: number;
  input_message_id: string | null;
  requested_by: string;
  worker_id: string | null;
  lease_expires_at: string | null;
  attempt_count: number;
  last_heartbeat_at: string | null;
  available_at: string;
  blocked_reason: string | null;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  result_message_id: string | null;
  result_metadata: Record<string, unknown>;
  independent_review?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
};

export type AgentApproval = {
  id: string;
  card_id: string;
  approval_type: string;
  decision: string;
  card_revision: number;
  decided_by: string;
  notes: string | null;
  created_at: string;
};

export type AgentEvent = {
  id: string;
  card_id: string;
  event_type: string;
  actor_type: string;
  actor_user_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
};

export type AgentCardDetail = AgentCardSummary & {
  messages: AgentMessage[];
  runs: AgentRun[];
  approvals: AgentApproval[];
  events: AgentEvent[];
};

export type CodexUsageWindow = {
  name: string;
  used_percent: number | null;
  remaining_percent: number | null;
  duration_minutes: number | null;
  reset_at: string | null;
  status: "available" | "unavailable";
};

export type CodexQuota = {
  status: "available" | "unavailable" | "stale" | "collection_failed";
  source: string;
  collected_at: string | null;
  last_attempt_at: string | null;
  freshness_seconds: number | null;
  error_code: string | null;
  account_id: string | null;
  buckets: {
    limit_id: string | null;
    limit_name: string | null;
    model_slug: string | null;
    plan_type: string | null;
    windows: CodexUsageWindow[];
    credits: { balance?: string | null; has_credits?: boolean | null; unlimited?: boolean | null } | null;
  }[];
  ordinary_usage_allowed: boolean | null;
  available_reset_credits: number | null;
  metric_statuses: Record<string, string>;
};

export type CodexTokenAggregate = {
  execution_count: number;
  available_count: number;
  unavailable_count: number;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  reasoning_tokens: number | null;
  total_tokens: number | null;
  stage: string | null;
  card_id: string | null;
  actual_model: string | null;
  reasoning_effort: string | null;
};

export type CodexSummary = {
  period_start: string;
  period_end: string;
  token_accounting: {
    overall: CodexTokenAggregate;
    by_stage: CodexTokenAggregate[];
    by_card: CodexTokenAggregate[];
    by_model: CodexTokenAggregate[];
  };
};

export type CodexExecution = {
  run_id: string;
  attempt_count: number;
  turn_id: string;
  card_id: string;
  card_revision: number;
  stage: string;
  review_attempt: number | null;
  configured_model: string | null;
  actual_model: string | null;
  reasoning_effort: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  reasoning_tokens: number | null;
  total_tokens: number | null;
  usage_status: string;
  usage_source: string;
  started_at: string | null;
  finished_at: string | null;
  recorded_at: string;
};

export type CodexExecutionList = {
  total: number;
  items: CodexExecution[];
  limit: number;
  offset: number;
};

export type CodexStageConfiguration = {
  stage: string;
  configured_model: string | null;
  configured_reasoning_effort: string | null;
  last_reported_configured_model: string | null;
  last_reported_reasoning_effort: string | null;
  configuration_source: string | null;
  configuration_reported_at: string | null;
  actual_model_last_execution: string | null;
  reasoning_effort_last_execution: string | null;
  observed_at: string | null;
  actual_model_observed_at: string | null;
  status: string;
};

type UsageResponse<T> = { success: boolean; data: T };

export async function getCodexQuota(): Promise<CodexQuota> {
  return (await apiRequest<UsageResponse<CodexQuota>>("/agent/usage/quota")).data;
}

export async function getCodexSummary(fromTime: string, toTime: string): Promise<CodexSummary> {
  const query = new URLSearchParams({ from_time: fromTime, to_time: toTime });
  return (await apiRequest<UsageResponse<CodexSummary>>(`/agent/usage/summary?${query}`)).data;
}

export async function getCodexExecutions(
  fromTime: string, toTime: string, cardId?: string, offset = 0, limit = 100,
): Promise<CodexExecutionList> {
  const query = new URLSearchParams({ from_time: fromTime, to_time: toTime,
    offset: String(offset), limit: String(limit) });
  if (cardId) query.set("card_id", cardId);
  return (await apiRequest<UsageResponse<CodexExecutionList>>(`/agent/usage/executions?${query}`)).data;
}

export async function getCodexConfiguration(): Promise<CodexStageConfiguration[]> {
  return (await apiRequest<UsageResponse<CodexStageConfiguration[]>>("/agent/usage/configuration")).data;
}

type AgentCardListResponse = {
  success: true;
  data: AgentCardSummary[];
};

type AgentCardResponse = {
  success: true;
  data: AgentCardDetail;
};

type AgentDecisionRequest = {
  notes?: string;
};

function decisionBody(notes: string | null | undefined): string {
  const trimmed = notes?.trim();
  const request: AgentDecisionRequest = trimmed ? { notes: trimmed } : {};
  return JSON.stringify(request);
}

export async function listAgentCards(
  includeClosed: boolean,
): Promise<AgentCardSummary[]> {
  const params = new URLSearchParams({
    include_closed: includeClosed ? "true" : "false",
  });
  const response = await apiRequest<AgentCardListResponse>(
    `/agent/cards?${params.toString()}`,
  );
  return response.data;
}

export async function getAgentCard(cardId: string): Promise<AgentCardDetail> {
  const response = await apiRequest<AgentCardResponse>(`/agent/cards/${cardId}`);
  return response.data;
}

export async function createAgentCard(
  title: string,
  description: string,
): Promise<AgentCardDetail> {
  const response = await apiRequest<AgentCardResponse>("/agent/cards", {
    method: "POST",
    body: JSON.stringify({
      title,
      description,
      client_message_id: crypto.randomUUID(),
    }),
  });
  return response.data;
}

export async function addAgentFollowUp(
  cardId: string,
  content: string,
): Promise<AgentCardDetail> {
  const response = await apiRequest<AgentCardResponse>(
    `/agent/cards/${cardId}/messages`,
    {
      method: "POST",
      body: JSON.stringify({
        content,
        client_message_id: crypto.randomUUID(),
      }),
    },
  );
  return response.data;
}

export type ReviewRevision = {
  review_run_id: string;
  candidate_digest: string;
  finding_indexes?: number[];
};

export function reviewRevisionForCard(
  card: AgentCardDetail,
  included: Record<number, boolean>,
  notes: string,
): ReviewRevision | null {
  const run = card.runs.find((item) => item.id === card.latest_run?.id);
  const review = run?.independent_review;
  const findings = review?.findings;
  if (!card.allowed_actions.includes(AgentActions.CONTINUE_CORRECTIONS) ||
      !run || run.phase !== "review" || run.status !== "succeeded" ||
      typeof review?.candidate_digest !== "string" || !/^[a-f0-9]{64}$/.test(review.candidate_digest) ||
      !Array.isArray(findings) || !findings.length) return null;
  const selected = findings.map((_, index) => index).filter((index) => included[index] !== false);
  if (!selected.length || (selected.length !== findings.length && !notes.trim())) return null;
  return { review_run_id: run.id, candidate_digest: review.candidate_digest,
           finding_indexes: selected };
}

export async function performAgentAction(
  card: AgentCardDetail,
  action: AgentAction,
  notes?: string | null,
  revision?: ReviewRevision,
): Promise<AgentCardDetail> {
  const correctionInstructions = notes?.trim() ?? "";
  const returningToImplementation =
    action === AgentActions.RETURN_TO_IMPLEMENTATION;

  if (returningToImplementation && !correctionInstructions) {
    throw new Error(
      "Correction instructions are required to return to implementation.",
    );
  }

  if (action === AgentActions.CONTINUE_CORRECTIONS && !revision) {
    throw new Error("The exact review candidate and correction findings are required.");
  }
  const latestReview = action === AgentActions.RETRY_REVIEW
    ? card.runs?.find((run) => run.id === card.latest_run?.id)
    : undefined;
  const source = latestReview?.result_metadata.review_source as Record<string, unknown> | undefined;
  const candidate = source?.implementation_metadata as Record<string, unknown> | undefined;
  const reviewDigest = latestReview?.independent_review?.candidate_digest ?? candidate?.candidate_digest;
  if (action === AgentActions.RETRY_REVIEW &&
      (!card.allowed_actions.includes(AgentActions.RETRY_REVIEW) ||
       latestReview?.phase !== "review" ||
       !["failed", "succeeded"].includes(latestReview.status) ||
       typeof reviewDigest !== "string" || !/^[0-9a-f]{64}$/.test(reviewDigest))) {
    throw new Error("A retryable review execution with a verified candidate digest is required.");
  }
  const commonOptions = {
    method: "POST",
    body: action === AgentActions.RETRY_REVIEW
      ? JSON.stringify({ candidate_digest: reviewDigest, notes: correctionInstructions || undefined })
      : action === AgentActions.CONTINUE_CORRECTIONS
      ? JSON.stringify({ candidate_digest: revision!.candidate_digest, finding_indexes: revision!.finding_indexes, notes: correctionInstructions || undefined })
      : returningToImplementation
      ? JSON.stringify({ content: correctionInstructions })
      : decisionBody(notes),
  };

  const path = (() => {
    switch (action) {
      case AgentActions.APPROVE_IMPLEMENTATION:
        return `/agent/cards/${card.id}/approve-implementation`;
      case AgentActions.APPROVE_DEPLOYMENT:
        return `/agent/cards/${card.id}/approve-deployment`;
      case AgentActions.CONTINUE_CORRECTIONS:
        return `/agent/cards/${card.id}/reviews/${revision!.review_run_id}/continue-corrections`;
      case AgentActions.RETRY:
        return `/agent/cards/${card.id}/retry`;
      case AgentActions.RETURN_TO_IMPLEMENTATION:
        return `/agent/cards/${card.id}/return-to-implementation`;
      case AgentActions.RETRY_REVIEW:
        return `/agent/cards/${card.id}/reviews/${latestReview!.id}/retry`;
      case AgentActions.RETRY_GITHUB_SYNC:
        if (!card.deployment_recovery?.deployment_run_id) {
          throw new Error("Deployment recovery details are no longer available.");
        }
        return `/agent/cards/${card.id}/deployments/${card.deployment_recovery.deployment_run_id}/retry-github-sync`;
      case AgentActions.CANCEL:
        return `/agent/cards/${card.id}/cancel`;
      case AgentActions.CLOSE:
        return `/agent/cards/${card.id}/close`;
      default:
        throw new Error("Unsupported Agent action.");
    }
  })();

  const response = await apiRequest<AgentCardResponse>(path, commonOptions);
  return response.data;
}

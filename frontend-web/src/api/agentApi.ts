import { apiRequest } from "./authenticatedApi";

export const AgentActions = {
  ADD_FOLLOW_UP: "add_follow_up",
  APPROVE_IMPLEMENTATION: "approve_implementation",
  APPROVE_DEPLOYMENT: "approve_deployment",
  REQUEST_REVISION: "request_revision",
  RETRY: "retry",
  RETURN_TO_IMPLEMENTATION: "return_to_implementation",
  RETRY_GITHUB_SYNC: "retry_github_sync",
  RETRY_REVIEW: "retry_review",
  CANCEL: "cancel",
  CLOSE: "close",
} as const;

export type AgentAction = (typeof AgentActions)[keyof typeof AgentActions];

export type AgentLatestRun = {
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
  decisions: { finding_index: number; decision: "fix" | "defer_ignore" }[];
};

// Build decisions only for the exact latest exhausted review displayed by the form.
export function reviewRevisionForCard(
  card: AgentCardDetail,
  decisions: Record<number, "fix" | "defer_ignore">,
  notes: string,
): ReviewRevision | null {
  const run = card.runs.find((item) => item.id === card.latest_run?.id);
  const review = run?.independent_review;
  const findings = review?.findings;
  if (!card.allowed_actions.includes(AgentActions.REQUEST_REVISION) || card.status !== "blocked" ||
      !run || run.phase !== "review" || run.status !== "succeeded" || run.card_revision !== card.revision ||
      review?.verdict !== "changes_requested" || review.review_attempt !== 3 ||
      typeof review.candidate_digest !== "string" || !/^[a-f0-9]{64}$/.test(review.candidate_digest) ||
      !Array.isArray(findings) || !findings.length || !notes.trim() ||
      Object.keys(decisions).length !== findings.length ||
      !findings.every((_, index) => decisions[index] === "fix" || decisions[index] === "defer_ignore")) {
    return null;
  }
  return {
    review_run_id: run.id,
    candidate_digest: review.candidate_digest,
    decisions: findings.map((_, finding_index) => ({ finding_index, decision: decisions[finding_index] })),
  };
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

  if (action === AgentActions.REQUEST_REVISION && (!revision || !correctionInstructions)) {
    throw new Error("Review decisions and operator notes are required.");
  }
  const latestReview = action === AgentActions.RETRY_REVIEW
    ? card.runs?.find((run) => run.id === card.latest_run?.id)
    : undefined;
  const reviewDigest = latestReview?.independent_review?.candidate_digest;
  if (action === AgentActions.RETRY_REVIEW &&
      (!card.allowed_actions.includes(AgentActions.RETRY_REVIEW) ||
       card.status !== "blocked" || latestReview?.phase !== "review" ||
       latestReview.status !== "succeeded" ||
       typeof reviewDigest !== "string" || !/^[0-9a-f]{64}$/.test(reviewDigest))) {
    throw new Error("An eligible blocked review with a verified candidate digest is required.");
  }
  const commonOptions = {
    method: "POST",
    body: action === AgentActions.RETRY_REVIEW
      ? JSON.stringify({ candidate_digest: reviewDigest, notes: correctionInstructions || undefined })
      : action === AgentActions.REQUEST_REVISION
      ? JSON.stringify({ ...revision, notes: correctionInstructions })
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
      case AgentActions.REQUEST_REVISION:
        return `/agent/cards/${card.id}/request-revision`;
      case AgentActions.RETRY:
        return `/agent/cards/${card.id}/retry`;
      case AgentActions.RETURN_TO_IMPLEMENTATION:
        return `/agent/cards/${card.id}/return-to-implementation`;
      case AgentActions.RETRY_REVIEW:
        return `/agent/cards/${card.id}/reviews/${latestReview!.id}/retry-infrastructure`;
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

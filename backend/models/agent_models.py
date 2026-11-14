from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

from backend.core.agent_state import CardStatus, RepositoryScope, RunPhase, RunStatus


class AgentRequestModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class AgentCardCreate(AgentRequestModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=20000)
    client_message_id: UUID | None = None


class AgentMessageCreate(AgentRequestModel):
    content: str = Field(min_length=1, max_length=20000)
    client_message_id: UUID | None = None


class AgentDecisionRequest(AgentRequestModel):
    notes: str | None = Field(default=None, max_length=2000)


class AgentFindingDecision(AgentRequestModel):
    model_config = ConfigDict(extra="forbid")
    finding_index: int = Field(ge=0, le=49, strict=True)
    decision: Literal["fix", "defer_ignore"]


class AgentInfrastructureReviewRetryRequest(AgentRequestModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    notes: str | None = Field(default=None, max_length=2000)


class AgentEvidenceReviewRetryRequest(AgentRequestModel):
    """Admin-requested repeat review of the same candidate with pinned Android evidence."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    android_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    notes: str = Field(min_length=1, max_length=2000)


class AgentReviewRevisionRequest(AgentRequestModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    review_run_id: UUID
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    notes: str = Field(min_length=1, max_length=2000)
    decisions: list[AgentFindingDecision] = Field(min_length=1, max_length=50)


class AgentGitHubSyncRetryRequest(AgentRequestModel):
    notes: str | None = Field(default=None, max_length=2000)


class AgentCardAction(str, Enum):
    ADD_FOLLOW_UP = "add_follow_up"
    APPROVE_IMPLEMENTATION = "approve_implementation"
    APPROVE_DEPLOYMENT = "approve_deployment"
    REQUEST_REVISION = "request_revision"
    RETRY = "retry"
    RETURN_TO_IMPLEMENTATION = "return_to_implementation"
    RETRY_GITHUB_SYNC = "retry_github_sync"
    RETRY_REVIEW = "retry_review"
    CANCEL = "cancel"
    CLOSE = "close"


class AgentErrorResponse(BaseModel):
    detail: str


class CodexUsageWindow(BaseModel):
    name: str
    used_percent: int | None = None
    remaining_percent: int | None = None
    duration_minutes: int | None = None
    reset_at: datetime | None = None
    status: Literal["available", "unavailable"]


class CodexUsageBucket(BaseModel):
    limit_id: str | None = None
    limit_name: str | None = None
    model_slug: str | None = None
    plan_type: str | None = None
    windows: list[CodexUsageWindow]
    credits: dict[str, Any] | None = None


class CodexQuotaData(BaseModel):
    status: Literal["available", "unavailable", "stale", "collection_failed"]
    source: str
    collected_at: datetime | None = None
    last_attempt_at: datetime | None = None
    freshness_seconds: int | None = None
    error_code: str | None = None
    account_id: str | None = None
    buckets: list[CodexUsageBucket]
    ordinary_usage_allowed: bool | None = None
    available_reset_credits: int | None = None
    metric_statuses: dict[str, str]


class CodexQuotaResponse(BaseModel):
    success: bool
    data: CodexQuotaData


class CodexTokenAggregate(BaseModel):
    execution_count: int
    available_count: int
    unavailable_count: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    stage: str | None = None
    card_id: UUID | None = None
    actual_model: str | None = None
    reasoning_effort: str | None = None


class CodexTokenAccounting(BaseModel):
    overall: CodexTokenAggregate
    by_stage: list[CodexTokenAggregate]
    by_card: list[CodexTokenAggregate]
    by_model: list[CodexTokenAggregate]


class CodexSummaryData(BaseModel):
    period_start: datetime
    period_end: datetime
    token_accounting: CodexTokenAccounting


class CodexSummaryResponse(BaseModel):
    success: bool
    data: CodexSummaryData


class CodexExecutionUsage(BaseModel):
    run_id: UUID
    attempt_count: int
    turn_id: str
    card_id: UUID
    card_revision: int
    stage: str
    review_attempt: int | None = None
    configured_model: str | None = None
    actual_model: str | None = None
    reasoning_effort: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    usage_status: str
    usage_source: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    recorded_at: datetime


class CodexExecutionList(BaseModel):
    total: int
    items: list[CodexExecutionUsage]
    limit: int
    offset: int


class CodexExecutionResponse(BaseModel):
    success: bool
    data: CodexExecutionList


class CodexStageConfiguration(BaseModel):
    stage: str
    configured_model: str | None = None
    configured_reasoning_effort: str | None = None
    last_reported_configured_model: str | None = None
    last_reported_reasoning_effort: str | None = None
    configuration_source: str | None = None
    configuration_reported_at: datetime | None = None
    actual_model_last_execution: str | None = None
    reasoning_effort_last_execution: str | None = None
    observed_at: datetime | None = None
    actual_model_observed_at: datetime | None = None
    status: str


class CodexConfigurationResponse(BaseModel):
    success: bool
    data: list[CodexStageConfiguration]


class AgentLatestRun(BaseModel):
    id: UUID
    phase: RunPhase
    status: RunStatus
    card_revision: int = Field(ge=1)
    attempt_count: int = Field(ge=0)
    blocked_reason: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class AgentDeploymentRecovery(BaseModel):
    github_sync_status: str
    retryable: bool
    blocker_code: str | None = None
    last_error: str | None = None
    candidate_commit: str | None = None
    deployment_run_id: UUID
    production_deployed: bool


class AgentCardSummary(BaseModel):
    id: UUID
    title: str
    description: str
    status: CardStatus
    repository_scope: RepositoryScope
    revision: int = Field(ge=1)
    base_branch: str
    feature_branch: str | None = None
    worktree_path: str | None = None
    codex_thread_id: str | None = None
    resume_status: CardStatus | None = None
    blocked_reason: str | None = None
    blocked_until: datetime | None = None
    created_by: UUID
    closed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    latest_run: AgentLatestRun | None = None
    deployment_recovery: AgentDeploymentRecovery | None = None
    allowed_actions: list[AgentCardAction] = Field(default_factory=list)


class AgentMessage(BaseModel):
    id: UUID
    card_id: UUID
    author_type: str
    content: str
    created_by: UUID | None = None
    client_message_id: UUID | None = None
    created_at: datetime


class AgentRun(BaseModel):
    id: UUID
    card_id: UUID
    phase: RunPhase
    status: RunStatus
    card_revision: int = Field(ge=1)
    input_message_id: UUID | None = None
    requested_by: UUID
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = Field(ge=0)
    last_heartbeat_at: datetime | None = None
    available_at: datetime
    blocked_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    result_message_id: UUID | None = None
    result_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


    @computed_field
    @property
    def independent_review(self) -> dict[str, Any] | None:
        if self.phase is not RunPhase.REVIEW:
            return None
        evidence = self.result_metadata.get("independent_review")
        if isinstance(evidence, dict):
            return evidence
        source = self.result_metadata.get("review_source", {})
        return {k: source.get(k) for k in ("review_cycle", "review_attempt", "implementation_run_id")}


class AgentApproval(BaseModel):
    id: UUID
    card_id: UUID
    approval_type: str
    decision: str
    card_revision: int = Field(ge=1)
    decided_by: UUID
    notes: str | None = None
    created_at: datetime


class AgentEvent(BaseModel):
    id: UUID
    card_id: UUID
    event_type: str
    actor_type: str
    actor_user_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AgentCardDetail(AgentCardSummary):
    messages: list[AgentMessage] = Field(default_factory=list)
    runs: list[AgentRun] = Field(default_factory=list)
    approvals: list[AgentApproval] = Field(default_factory=list)
    events: list[AgentEvent] = Field(default_factory=list)


class AgentCardResponse(BaseModel):
    success: Literal[True] = True
    data: AgentCardDetail


class AgentCardListResponse(BaseModel):
    success: Literal[True] = True
    data: list[AgentCardSummary]

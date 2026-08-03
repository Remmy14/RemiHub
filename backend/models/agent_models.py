from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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


class AgentGitHubSyncRetryRequest(AgentRequestModel):
    notes: str | None = Field(default=None, max_length=2000)


class AgentCardAction(str, Enum):
    ADD_FOLLOW_UP = "add_follow_up"
    APPROVE_IMPLEMENTATION = "approve_implementation"
    APPROVE_DEPLOYMENT = "approve_deployment"
    RETRY = "retry"
    RETRY_GITHUB_SYNC = "retry_github_sync"
    CANCEL = "cancel"
    CLOSE = "close"


class AgentErrorResponse(BaseModel):
    detail: str


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

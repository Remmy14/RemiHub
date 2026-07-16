from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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

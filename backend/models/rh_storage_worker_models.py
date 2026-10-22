from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


WorkerCount = Annotated[int, Field(strict=True, ge=1, le=16)]


class DefaultWorkersUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_workers: WorkerCount


class WorkerBoostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workers: WorkerCount
    duration_minutes: StrictInt

    @field_validator("duration_minutes")
    @classmethod
    def validate_duration(cls, value: int) -> int:
        if value not in (15, 60, 120, 240):
            raise ValueError("duration_minutes must be 15, 60, 120, or 240")
        return value

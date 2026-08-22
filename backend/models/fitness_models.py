from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FitnessRequestModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class WorkoutType(str, Enum):
    RUNNING = "RUNNING"
    LIFTING = "LIFTING"


class ScheduledWorkoutStatus(str, Enum):
    PLANNED = "PLANNED"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    RESCHEDULED = "RESCHEDULED"


class PlanInstanceStatus(str, Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"


class LiftingTemplateExercise(FitnessRequestModel):
    exercise_id: UUID
    display_order: int = Field(ge=0)


class WorkoutTemplateCreate(FitnessRequestModel):
    name: str = Field(min_length=1, max_length=160)
    type: WorkoutType
    notes: str | None = Field(default=None, max_length=4000)
    planned_distance_miles: Decimal | None = Field(default=None, ge=0, le=10000)
    exercises: list[LiftingTemplateExercise] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_type_specific_fields(self):
        if self.type is WorkoutType.RUNNING and self.planned_distance_miles is None:
            raise ValueError("planned_distance_miles is required for RUNNING templates")
        if self.type is WorkoutType.LIFTING and not self.exercises:
            raise ValueError("exercises are required for LIFTING templates")
        return self


class WorkoutTemplateUpdate(FitnessRequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    notes: str | None = Field(default=None, max_length=4000)
    planned_distance_miles: Decimal | None = Field(default=None, ge=0, le=10000)


class LiftingTemplateExercisesReplace(FitnessRequestModel):
    exercises: list[LiftingTemplateExercise] = Field(min_length=1)


class PlanTemplateItem(FitnessRequestModel):
    workout_template_id: UUID
    day_offset: int = Field(ge=0)
    display_order: int = Field(ge=0)


class PlanTemplateCreate(FitnessRequestModel):
    name: str = Field(min_length=1, max_length=160)
    notes: str | None = Field(default=None, max_length=4000)
    items: list[PlanTemplateItem] = Field(default_factory=list)


class PlanTemplateUpdate(FitnessRequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    notes: str | None = Field(default=None, max_length=4000)


class PlanTemplateItemsReplace(FitnessRequestModel):
    items: list[PlanTemplateItem] = Field(default_factory=list)


class PlanInstantiateRequest(FitnessRequestModel):
    start_date: date


class ScheduledWorkoutCreate(FitnessRequestModel):
    workout_template_id: UUID
    scheduled_date: date


class RunningCompletion(FitnessRequestModel):
    completed_distance_miles: Decimal = Field(ge=0, le=10000)
    duration_seconds: int = Field(ge=0, le=864000)
    notes: str | None = Field(default=None, max_length=4000)


class WorkoutCompleteRequest(FitnessRequestModel):
    running: RunningCompletion | None = None


class WorkoutRescheduleRequest(FitnessRequestModel):
    scheduled_date: date


class FitnessSuccessResponse(BaseModel):
    success: Literal[True] = True
    data: dict | list

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

FITNESS_RECURRENCE_MAX_WEEKS = 260
FITNESS_DURATION_SECONDS_MAX = 864000


class FitnessRequestModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class WorkoutType(str, Enum):
    RUNNING = "RUNNING"
    LIFTING = "LIFTING"
    CYCLING = "CYCLING"


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
    planned_duration_seconds: int | None = Field(default=None, gt=0, le=FITNESS_DURATION_SECONDS_MAX)
    exercises: list[LiftingTemplateExercise] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_type_specific_fields(self):
        if self.type is WorkoutType.RUNNING and self.planned_distance_miles is None:
            raise ValueError("planned_distance_miles is required for RUNNING templates")
        if self.type is WorkoutType.LIFTING and not self.exercises:
            raise ValueError("exercises are required for LIFTING templates")
        if self.type is WorkoutType.CYCLING and self.planned_duration_seconds is None:
            raise ValueError("planned_duration_seconds is required for CYCLING templates")
        return self


class WorkoutTemplateUpdate(FitnessRequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    notes: str | None = Field(default=None, max_length=4000)
    planned_distance_miles: Decimal | None = Field(default=None, ge=0, le=10000)
    planned_duration_seconds: int | None = Field(default=None, gt=0, le=FITNESS_DURATION_SECONDS_MAX)


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


class PlanInstanceRepeatWeekRequest(FitnessRequestModel):
    week_start: date
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)


class ScheduledWorkoutCreate(FitnessRequestModel):
    workout_template_id: UUID
    scheduled_date: date


class ScheduledWorkoutTemplateReplace(FitnessRequestModel):
    workout_template_id: UUID


class RunningCompletion(FitnessRequestModel):
    completed_distance_miles: Decimal = Field(ge=0, le=10000)
    duration_seconds: int = Field(ge=0, le=FITNESS_DURATION_SECONDS_MAX)
    notes: str | None = Field(default=None, max_length=4000)


class WorkoutCompleteRequest(FitnessRequestModel):
    running: RunningCompletion | None = None


class GarminActivitySelectionRequest(FitnessRequestModel):
    activity_id: str = Field(min_length=1, max_length=160)


class WorkoutRescheduleRequest(FitnessRequestModel):
    scheduled_date: date


class RecurringSeriesRequest(FitnessRequestModel):
    workout_template_id: UUID
    start_date: date
    weekdays: list[int] = Field(min_length=1, max_length=7)
    duration_weeks: int | None = Field(default=None, ge=1, le=FITNESS_RECURRENCE_MAX_WEEKS)
    end_date: date | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_rule(self):
        if not self.duration_weeks and not self.end_date:
            raise ValueError("duration_weeks or end_date is required")
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("weekdays must be unique")
        if any(day < 1 or day > 7 for day in self.weekdays):
            raise ValueError("weekdays must use ISO values 1 through 7")
        self.weekdays = sorted(self.weekdays)
        if self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class PlanInstanceCleanupRequest(FitnessRequestModel):
    from_date: date | None = None


class FitnessSuccessResponse(BaseModel):
    success: Literal[True] = True
    data: dict | list

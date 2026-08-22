from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WeightliftingRequestModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class WeightUnit(str, Enum):
    LB = "lb"
    KG = "kg"


class Weekday(str, Enum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


class WeightliftingDaySlot(BaseModel):
    slot: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=80)
    weekday: Weekday | None = None


def _validate_contiguous_slots(days: list[WeightliftingDaySlot]) -> None:
    slots = [day.slot for day in days]
    expected = list(range(1, len(slots) + 1))
    if sorted(slots) != expected:
        raise ValueError("days must contain unique contiguous slots starting at 1")


class WeightliftingSettings(BaseModel):
    weight_unit: WeightUnit
    default_weight_increment: Decimal = Field(ge=0, le=200)
    default_target_reps: int = Field(ge=1, le=500)
    default_sets: int | None = Field(default=None, ge=1, le=100)
    days: list[WeightliftingDaySlot] = Field(min_length=1)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WeightliftingSettingsUpdate(WeightliftingRequestModel):
    weight_unit: WeightUnit = WeightUnit.LB
    default_weight_increment: Decimal = Field(default=Decimal("5"), ge=0, le=200)
    default_target_reps: int = Field(default=12, ge=1, le=500)
    default_sets: int | None = Field(default=3, ge=1, le=100)
    days: list[WeightliftingDaySlot] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_slots(self):
        _validate_contiguous_slots(self.days)
        return self


class WeightliftingExerciseCreate(WeightliftingRequestModel):
    name: str = Field(min_length=1, max_length=160)
    display_order: int | None = None
    notes: str | None = Field(default=None, max_length=2000)
    target_reps: int | None = Field(default=None, ge=1, le=500)
    target_sets: int | None = Field(default=None, ge=1, le=100)
    weight_increment: Decimal | None = Field(default=None, ge=0, le=200)
    weight_unit: WeightUnit | None = None


class WeightliftingExerciseUpdate(WeightliftingRequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    display_order: int | None = None
    notes: str | None = Field(default=None, max_length=2000)
    target_reps: int | None = Field(default=None, ge=1, le=500)
    target_sets: int | None = Field(default=None, ge=1, le=100)
    weight_increment: Decimal | None = Field(default=None, ge=0, le=200)
    weight_unit: WeightUnit | None = None


class WeightliftingExerciseOrderItem(WeightliftingRequestModel):
    id: UUID
    display_order: int


class WeightliftingExerciseReorder(WeightliftingRequestModel):
    exercises: list[WeightliftingExerciseOrderItem] = Field(min_length=1)


class WeightliftingEntryUpsert(WeightliftingRequestModel):
    exercise_id: UUID
    week_start: date
    workout_day_slot: int = Field(ge=1)
    workout_date: date | None = None
    weight: Decimal = Field(ge=0, le=2000)
    reps: int = Field(ge=1, le=500)
    sets: int | None = Field(default=None, ge=1, le=100)
    notes: str | None = Field(default=None, max_length=2000)
    completed: bool = True
    fitness_scheduled_workout_id: UUID | None = None


class WeightliftingEntryUpdate(WeightliftingRequestModel):
    workout_date: date | None = None
    weight: Decimal | None = Field(default=None, ge=0, le=2000)
    reps: int | None = Field(default=None, ge=1, le=500)
    sets: int | None = Field(default=None, ge=1, le=100)
    notes: str | None = Field(default=None, max_length=2000)
    completed: bool | None = None
    fitness_scheduled_workout_id: UUID | None = None


class WeightliftingEntryClear(WeightliftingRequestModel):
    exercise_id: UUID
    week_start: date
    workout_day_slot: int = Field(ge=1)


class WeightliftingSuccessResponse(BaseModel):
    success: Literal[True] = True
    data: dict | list

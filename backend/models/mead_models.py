from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MeadRequestModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class MeadStage(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    AGING = "aging"
    BOTTLED = "bottled"
    ARCHIVED = "archived"


class MeadEventType(str, Enum):
    GRAVITY_READING = "gravity_reading"
    NOTE = "note"
    RACKING = "racking"
    NUTRIENT_ADDITION = "nutrient_addition"
    STAGE_CHANGE = "stage_change"
    OTHER = "other"


class MeadTaskType(str, Enum):
    CHECK_GRAVITY = "check_gravity"
    ADD_NUTRIENTS = "add_nutrients"
    CONSIDER_RACKING = "consider_racking"
    CHECK_CLARITY_TASTE = "check_clarity_taste"
    CONSIDER_BOTTLING = "consider_bottling"
    CUSTOM = "custom"


class MeadTaskStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MeadBatchBase(MeadRequestModel):
    name: str = Field(min_length=1, max_length=160)
    start_at: datetime
    stage: MeadStage = MeadStage.PRIMARY
    volume: Decimal = Field(gt=0, le=100000)
    volume_unit: str = Field(min_length=1, max_length=32)
    original_gravity: Decimal = Field(ge=Decimal("0.900"), le=Decimal("1.300"))
    target_final_gravity: Decimal | None = Field(
        default=None,
        ge=Decimal("0.900"),
        le=Decimal("1.300"),
    )
    notes: str | None = Field(default=None, max_length=4000)
    recipe_notes: str | None = Field(default=None, max_length=4000)
    tosna_enabled: bool = False
    tosna_nutrient_name: str | None = Field(default=None, max_length=120)
    tosna_total_amount: Decimal | None = Field(default=None, gt=0, le=100000)
    tosna_unit: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_tosna(self):
        if self.tosna_enabled:
            if self.tosna_total_amount is None:
                raise ValueError("tosna_total_amount is required when TOSNA is enabled")
            if not self.tosna_unit:
                raise ValueError("tosna_unit is required when TOSNA is enabled")
        return self


class MeadBatchCreate(MeadBatchBase):
    pass


class MeadBatchUpdate(MeadRequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    start_at: datetime | None = None
    stage: MeadStage | None = None
    volume: Decimal | None = Field(default=None, gt=0, le=100000)
    volume_unit: str | None = Field(default=None, min_length=1, max_length=32)
    original_gravity: Decimal | None = Field(
        default=None,
        ge=Decimal("0.900"),
        le=Decimal("1.300"),
    )
    target_final_gravity: Decimal | None = Field(
        default=None,
        ge=Decimal("0.900"),
        le=Decimal("1.300"),
    )
    notes: str | None = Field(default=None, max_length=4000)
    recipe_notes: str | None = Field(default=None, max_length=4000)
    tosna_enabled: bool | None = None
    tosna_nutrient_name: str | None = Field(default=None, max_length=120)
    tosna_total_amount: Decimal | None = Field(default=None, gt=0, le=100000)
    tosna_unit: str | None = Field(default=None, max_length=32)


class MeadRecipeItemCreate(MeadRequestModel):
    name: str = Field(min_length=1, max_length=160)
    amount: Decimal | None = Field(default=None, gt=0, le=100000)
    unit: str | None = Field(default=None, max_length=32)
    notes: str | None = Field(default=None, max_length=1000)
    display_order: int | None = None


class MeadRecipeItemUpdate(MeadRequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    amount: Decimal | None = Field(default=None, gt=0, le=100000)
    unit: str | None = Field(default=None, max_length=32)
    notes: str | None = Field(default=None, max_length=1000)
    display_order: int | None = None


class MeadRecipeItemsReplace(MeadRequestModel):
    items: list[MeadRecipeItemCreate] = Field(default_factory=list)


class MeadEventCreate(MeadRequestModel):
    event_at: datetime
    event_type: MeadEventType
    gravity: Decimal | None = Field(
        default=None,
        ge=Decimal("0.900"),
        le=Decimal("1.300"),
    )
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_gravity_event(self):
        if self.event_type is MeadEventType.GRAVITY_READING and self.gravity is None:
            raise ValueError("gravity is required for gravity readings")
        return self


class MeadGravityReadingCreate(MeadRequestModel):
    event_at: datetime
    gravity: Decimal = Field(ge=Decimal("0.900"), le=Decimal("1.300"))
    notes: str | None = Field(default=None, max_length=4000)


class MeadTaskCreate(MeadRequestModel):
    task_type: MeadTaskType = MeadTaskType.CUSTOM
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    due_at: datetime


class MeadTaskComplete(MeadRequestModel):
    completed_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=4000)


class MeadTaskReschedule(MeadRequestModel):
    due_at: datetime


class MeadSuccessResponse(BaseModel):
    success: Literal[True] = True
    data: dict | list

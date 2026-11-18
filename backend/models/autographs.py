"""Legacy and helmet-scoped autograph API contracts."""
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

View = Literal["front", "right", "back", "left"]


class AutographCreate(BaseModel):
    """Installed Android request. Empty image_path means no photograph."""
    driver_name: str = Field(min_length=1, max_length=200)
    image_path: str
    helmet_view: View
    x_percent: float = Field(ge=0, le=1, allow_inf_nan=False)
    y_percent: float = Field(ge=0, le=1, allow_inf_nan=False)
    region: str | None = None
    notes: str | None = None


class AutographEntry(AutographCreate):
    id: int
    created_at: datetime


class HelmetCreate(BaseModel):
    year: int = Field(ge=1911, le=9999)
    display_name: str = Field(min_length=1, max_length=200)
    description: str | None = None

    @field_validator("display_name")
    @classmethod
    def nonblank_name(cls, value: str):
        if not value.strip():
            raise ValueError("display_name cannot be blank")
        return value.strip()


class Helmet(HelmetCreate):
    id: UUID
    uses_legacy_images: bool
    created_at: datetime


class DriverCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)

    @field_validator("display_name")
    @classmethod
    def nonblank_name(cls, value: str):
        if not value.strip():
            raise ValueError("display_name cannot be blank")
        return value


class Driver(DriverCreate):
    id: UUID
    normalized_name: str
    created_at: datetime


class DriverMerge(BaseModel):
    source_driver_id: UUID


class ScopedAutographCreate(BaseModel):
    driver_id: UUID | None = None
    driver_name: str | None = None
    helmet_view: View
    x_percent: float = Field(ge=0, le=1, allow_inf_nan=False)
    y_percent: float = Field(ge=0, le=1, allow_inf_nan=False)
    image_path: str | None = None
    date_signed: date | None = None
    region: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def one_driver(self):
        if (self.driver_id is None) == (self.driver_name is None):
            raise ValueError("Supply exactly one of driver_id or driver_name")
        if self.driver_name is not None and not self.driver_name.strip():
            raise ValueError("driver_name cannot be blank")
        return self


class ScopedAutograph(AutographEntry):
    helmet_id: UUID
    driver_id: UUID
    date_signed: date | None


class HelmetImage(BaseModel):
    helmet_id: UUID
    helmet_view: View
    image_path: str
    image_url: str
    content_type: str
    byte_size: int
    created_at: datetime
    updated_at: datetime


class HelmetStats(BaseModel):
    total_signatures: int
    unique_signers: int


class CollectionStats(HelmetStats):
    total_helmets: int


class DriverHistoryItem(ScopedAutograph):
    helmet_year: int
    helmet_name: str
    image_url: str | None

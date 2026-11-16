"""Wire contract for the private Adventures API. Nullable fields are intentional."""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
import re
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    success: bool = True
    data: T


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class Request(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class Intent(str, Enum):
    CONSIDERING = "Considering"
    PLANNED = "Planned"
    BOOKED = "Booked"
    CANCELLED = "Cancelled"


class State(str, Enum):
    CONSIDERING = "Considering"
    PLANNED = "Planned"
    BOOKED = "Booked"
    IN_PROGRESS = "In Progress"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"


class Electricity(str, Enum):
    AMP_50 = "50A"
    AMP_30 = "30A"
    AMP_20 = "20A"
    NONE = "none"
    UNKNOWN = "unknown"


class ReturnChoice(str, Enum):
    YES = "yes"
    NO = "no"
    UNDECIDED = "undecided"


class CampgroundInput(Request):
    name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(None, max_length=500)
    city: str | None = Field(None, max_length=120)
    state_province: str | None = Field(None, max_length=120)
    postal_code: str | None = Field(None, max_length=40)
    country: str | None = Field(None, max_length=120)
    website: str | None = Field(None, max_length=2048)
    latitude: Decimal | None = Field(None, ge=-90, le=90)
    longitude: Decimal | None = Field(None, ge=-180, le=180)
    notes: str | None = Field(None, max_length=20000)


class CampgroundPatch(CampgroundInput):
    name: str | None = Field(None, min_length=1, max_length=200)


class CampgroundOut(CampgroundInput):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    created_at: datetime
    updated_at: datetime
    name: str


class TripInput(Request):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(None, max_length=20000)
    destination: str | None = Field(None, max_length=500)
    campground_id: UUID | None = None
    arrival_date: date | None = None
    departure_date: date | None = None
    intent: Intent = Intent.CONSIDERING
    notes: str | None = Field(None, max_length=20000)
    campsite_number: str | None = Field(None, max_length=120)
    confirmation_number: str | None = Field(None, max_length=200)
    reservation_status: str | None = Field(None, max_length=120)
    check_in_time: time | None = None
    check_out_time: time | None = None
    electricity: Electricity = Electricity.UNKNOWN
    water_hookup: bool | None = None
    sewer_hookup: bool | None = None
    site_length_feet: Decimal | None = Field(None, gt=0, le=10000)
    access_notes: str | None = Field(None, max_length=4000)
    booking_website: str | None = Field(None, max_length=2048)
    reservation_notes: str | None = Field(None, max_length=10000)

    @field_validator("arrival_date", "departure_date", mode="before")
    @classmethod
    def require_date_only(cls, value):
        if value is None or (isinstance(value, date) and not isinstance(value, datetime)):
            return value
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return value
        raise ValueError("Use an ISO calendar date YYYY-MM-DD, not a timestamp or epoch")

class TripPatch(TripInput):
    title: str | None = Field(None, min_length=1, max_length=200)


class TripOut(TripInput):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    title: str
    state: State
    classification: str
    days_until_arrival: int | None
    nights: int | None
    calendar_days: int | None
    trip_day: int | None
    cover_photo_id: UUID | None
    campground_name: str | None = None
    created_at: datetime
    updated_at: datetime


class NotesPatch(Request):
    notes: str | None = Field(None, max_length=20000)


class CalendarEntry(Request):
    id: UUID
    title: str
    arrival_date: date
    departure_date: date
    intent: Intent
    state: State
    destination: str | None
    campground_name: str | None
    cover_photo_id: UUID | None


class YearSummary(Request):
    year: int
    total_planned: int
    upcoming: int
    active: int
    completed: int
    planned_nights: int
    completed_nights: int
    distinct_visited_campgrounds: int


class Dashboard(Request):
    next_trip: TripOut | None
    active: list[TripOut]
    upcoming: list[TripOut]
    summary: YearSummary


class ActivityInput(Request):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(None, max_length=10000)
    activity_date: date | None = None
    activity_time: time | None = None
    location: str | None = Field(None, max_length=500)
    notes: str | None = Field(None, max_length=10000)
    display_order: int = Field(0, ge=0)
    completed: bool = False

    @field_validator("activity_date", mode="before")
    @classmethod
    def require_date_only(cls, value):
        return TripInput.require_date_only(value)


class ActivityPatch(ActivityInput):
    title: str | None = Field(None, min_length=1, max_length=200)


class ActivityOut(ActivityInput):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    trip_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ChecklistItemInput(Request):
    text: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=100)
    display_order: int = Field(0, ge=0)
    notes: str | None = Field(None, max_length=4000)


class ChecklistItemPatch(ChecklistItemInput):
    text: str | None = Field(None, min_length=1, max_length=500)
    category: str | None = Field(None, min_length=1, max_length=100)


class TripItemInput(ChecklistItemInput):
    completed: bool = False


class TripItemPatch(ChecklistItemPatch):
    completed: bool | None = None


class ChecklistItemOut(TripItemInput):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    created_at: datetime
    updated_at: datetime


class Template(Request):
    categories: list[str]
    items: Page[ChecklistItemOut]


class Checklist(Request):
    instantiated: bool
    items: list[ChecklistItemOut]
    completed_count: int
    total_count: int
    percent: int


class PhotoPatch(Request):
    caption: str | None = Field(None, max_length=4000)
    favorite: bool | None = None
    display_order: int | None = Field(None, ge=0)


class PhotoOut(Request):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    trip_id: UUID
    original_filename: str
    content_type: str
    byte_size: int
    caption: str | None
    favorite: bool
    display_order: int
    uploaded_at: datetime
    updated_at: datetime
    is_cover: bool
    thumbnail_url: str
    original_url: str


class JournalInput(Request):
    overall_summary: str | None = None
    favorite_part: str | None = None
    family_enjoyed: str | None = None
    went_well: str | None = None
    could_improve: str | None = None
    do_differently: str | None = None
    problems: str | None = None
    notes: str | None = None
    would_visit_again: bool | None = None


class JournalOut(JournalInput):
    model_config = ConfigDict(extra="ignore")
    trip_id: UUID
    created_at: datetime
    updated_at: datetime


class MemoryInput(Request):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(None, max_length=4000)
    long_text: str | None = Field(None, max_length=20000)
    memory_date: date | None = None
    photo_ids: list[UUID] = Field(default_factory=list, max_length=50)

    @field_validator("memory_date", mode="before")
    @classmethod
    def require_date_only(cls, value):
        return TripInput.require_date_only(value)


class MemoryPatch(MemoryInput):
    title: str | None = Field(None, min_length=1, max_length=200)
    photo_ids: list[UUID] | None = None


class MemoryOut(MemoryInput):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    trip_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class RatingInput(Request):
    overall: int | None = Field(None, ge=1, le=5)
    campsite_quality: int | None = Field(None, ge=1, le=5)
    facilities: int | None = Field(None, ge=1, le=5)
    activities: int | None = Field(None, ge=1, le=5)
    family_friendliness: int | None = Field(None, ge=1, le=5)
    would_return: ReturnChoice | None = None
    review: str | None = Field(None, max_length=20000)


class RatingOut(RatingInput):
    model_config = ConfigDict(extra="ignore")
    trip_id: UUID
    created_at: datetime
    updated_at: datetime


class Visit(Request):
    trip: TripOut
    rating: RatingOut | None


class CampgroundVisits(Request):
    visits: Page[Visit]
    overall_average: Decimal | None
    overall_recorded_count: int

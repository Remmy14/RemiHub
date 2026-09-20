from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VehicleRequestModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class OdometerSource(str, Enum):
    MANUAL = "MANUAL"
    FUEL = "FUEL"
    MAINTENANCE = "MAINTENANCE"


class DrivingContext(str, Enum):
    NORMAL = "NORMAL"
    TOWING = "TOWING"
    MIXED = "MIXED"


class MaintenanceAction(str, Enum):
    REPLACED = "REPLACED"
    SERVICED = "SERVICED"
    INSPECTED = "INSPECTED"
    ROTATED = "ROTATED"
    REPAIRED = "REPAIRED"
    OTHER = "OTHER"


class VehicleBase(VehicleRequestModel):
    year: int = Field(ge=1886, le=3000)
    make: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=120)
    trim: str | None = Field(default=None, max_length=120)
    engine: str | None = Field(default=None, max_length=120)
    fuel_type: str = Field(min_length=1, max_length=80)
    tank_capacity_gallons: Decimal | None = Field(default=None, gt=0, le=1000)
    vin: str | None = Field(default=None, max_length=32)
    license_plate: str | None = Field(default=None, max_length=32)
    license_state: str | None = Field(default=None, max_length=32)
    purchase_date: date | None = None
    purchase_odometer_miles: Decimal | None = Field(default=None, ge=0, le=10000000)


class VehicleCreate(VehicleBase):
    pass


class VehicleUpdate(VehicleRequestModel):
    year: int | None = Field(default=None, ge=1886, le=3000)
    make: str | None = Field(default=None, min_length=1, max_length=120)
    model: str | None = Field(default=None, min_length=1, max_length=120)
    trim: str | None = Field(default=None, max_length=120)
    engine: str | None = Field(default=None, max_length=120)
    fuel_type: str | None = Field(default=None, min_length=1, max_length=80)
    tank_capacity_gallons: Decimal | None = Field(default=None, gt=0, le=1000)
    vin: str | None = Field(default=None, max_length=32)
    license_plate: str | None = Field(default=None, max_length=32)
    license_state: str | None = Field(default=None, max_length=32)
    purchase_date: date | None = None
    purchase_odometer_miles: Decimal | None = Field(default=None, ge=0, le=10000000)


class ManualOdometerObservationCreate(VehicleRequestModel):
    odometer_miles: Decimal = Field(ge=0, le=10000000)
    observed_at: datetime


class FuelRecordCreate(VehicleRequestModel):
    filled_at: datetime
    odometer_miles: Decimal = Field(ge=0, le=10000000)
    gallons: Decimal | None = Field(default=None, gt=0, le=10000)
    total_cost: Decimal | None = Field(default=None, gt=0, le=1000000)
    price_per_gallon: Decimal | None = Field(default=None, gt=0, le=1000)
    is_full_fill: bool = True
    driving_context: DrivingContext = DrivingContext.NORMAL
    fuel_grade: str | None = Field(default=None, max_length=80)
    station_name: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=4000)


class FuelRecordUpdate(VehicleRequestModel):
    filled_at: datetime | None = None
    odometer_miles: Decimal | None = Field(default=None, ge=0, le=10000000)
    gallons: Decimal | None = Field(default=None, gt=0, le=10000)
    total_cost: Decimal | None = Field(default=None, gt=0, le=1000000)
    price_per_gallon: Decimal | None = Field(default=None, gt=0, le=1000)
    is_full_fill: bool | None = None
    driving_context: DrivingContext | None = None
    fuel_grade: str | None = Field(default=None, max_length=80)
    station_name: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=4000)


class MaintenanceScheduleCreate(VehicleRequestModel):
    name: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    mileage_interval_miles: Decimal | None = Field(default=None, gt=0, le=1000000)
    time_interval_days: int | None = Field(default=None, gt=0, le=50000)
    last_service_odometer_miles: Decimal | None = Field(default=None, ge=0, le=10000000)
    last_service_date: date | None = None
    due_soon_miles: Decimal = Field(default=Decimal("500"), ge=0, le=1000000)
    due_soon_days: int = Field(default=14, ge=0, le=50000)
    overdue_miles: Decimal = Field(default=Decimal("500"), ge=0, le=1000000)
    overdue_days: int = Field(default=14, ge=0, le=50000)

    @model_validator(mode="after")
    def validate_intervals(self):
        if self.mileage_interval_miles is None and self.time_interval_days is None:
            raise ValueError("mileage_interval_miles or time_interval_days is required")
        return self


class MaintenanceScheduleUpdate(VehicleRequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    category: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    mileage_interval_miles: Decimal | None = Field(default=None, gt=0, le=1000000)
    time_interval_days: int | None = Field(default=None, gt=0, le=50000)
    last_service_odometer_miles: Decimal | None = Field(default=None, ge=0, le=10000000)
    last_service_date: date | None = None
    due_soon_miles: Decimal | None = Field(default=None, ge=0, le=1000000)
    due_soon_days: int | None = Field(default=None, ge=0, le=50000)
    overdue_miles: Decimal | None = Field(default=None, ge=0, le=1000000)
    overdue_days: int | None = Field(default=None, ge=0, le=50000)


class MaintenanceEventCreate(VehicleRequestModel):
    maintenance_schedule_id: UUID | None = None
    performed_at: datetime
    odometer_miles: Decimal = Field(ge=0, le=10000000)
    category: str = Field(min_length=1, max_length=120)
    action: MaintenanceAction
    description: str = Field(min_length=1, max_length=4000)
    cost: Decimal | None = Field(default=None, ge=0, le=1000000)
    service_provider: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=4000)


class MaintenanceEventUpdate(VehicleRequestModel):
    maintenance_schedule_id: UUID | None = None
    performed_at: datetime | None = None
    odometer_miles: Decimal | None = Field(default=None, ge=0, le=10000000)
    category: str | None = Field(default=None, min_length=1, max_length=120)
    action: MaintenanceAction | None = None
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    cost: Decimal | None = Field(default=None, ge=0, le=1000000)
    service_provider: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=4000)


class VehicleSuccessResponse(BaseModel):
    success: Literal[True] = True
    data: dict | list

from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictBool, field_validator, model_validator


class FinanceSnapshotItem(BaseModel):
    source: str
    source_account_id: str | None = None
    label: str
    institution_label: str | None = None
    asset_category: str
    value: float
    confidence: str


class FinanceSummaryCategory(BaseModel):
    asset_category: str
    value: float


class FinanceSummary(BaseModel):
    snapshot_id: str
    snapshot_month: str
    status: str
    total_assets: float
    total_liabilities: float
    net_worth: float
    notes: str | None = None
    items: list[FinanceSnapshotItem]
    categories: list[FinanceSummaryCategory]


class FinanceHistoryPoint(BaseModel):
    snapshot_id: str
    snapshot_month: str
    total_assets: float
    total_liabilities: float
    net_worth: float
    status: str


class FinanceAccountUpdate(BaseModel):
    include_in_net_worth: bool | None = None
    asset_category: str | None = None
    display_name: str | None = None
    display_order: int | None = None


MANUAL_CATEGORIES = frozenset({
    "cash", "brokerage", "retirement", "hsa", "education",
    "home", "vehicle", "other", "liability",
})


def _iso_date_input(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("as_of_date must be an ISO date (YYYY-MM-DD)")
    return value


class ManualAccountFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    classification: Literal["asset", "liability"]
    asset_category: str
    current_balance: str
    as_of_date: date
    include_in_net_worth: StrictBool = True
    notes: str | None = None

    @field_validator("as_of_date", mode="before")
    @classmethod
    def validate_iso_date_input(cls, value):
        return _iso_date_input(value)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value or value != value.strip() or len(value) > 200:
            raise ValueError("name must be 1–200 characters without outer whitespace")
        return value

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 2000:
            raise ValueError("notes must be at most 2000 characters")
        return value

    @field_validator("asset_category")
    @classmethod
    def validate_category(cls, value: str) -> str:
        if value not in MANUAL_CATEGORIES:
            raise ValueError("invalid asset_category")
        return value

    @field_validator("current_balance")
    @classmethod
    def validate_balance(cls, value: str) -> str:
        if not value or not value.replace(".", "", 1).isdigit() or value.count(".") > 1:
            raise ValueError("current_balance must be a nonnegative decimal string")
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("invalid current_balance") from exc
        if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
            raise ValueError("current_balance must be finite, nonnegative, with at most two decimal places")
        return value

    @field_validator("as_of_date")
    @classmethod
    def validate_date(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("as_of_date cannot be later than today")
        return value

    @model_validator(mode="after")
    def validate_classification(self):
        if (self.classification == "liability") != (self.asset_category == "liability"):
            raise ValueError("classification and asset_category are contradictory")
        return self


class ManualAccountCreate(ManualAccountFields):
    pass


class ManualAccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    classification: Literal["asset", "liability"] | None = None
    asset_category: str | None = None
    current_balance: str | None = None
    as_of_date: date | None = None
    include_in_net_worth: StrictBool | None = None
    notes: str | None = None
    active: StrictBool | None = None

    @field_validator("as_of_date", mode="before")
    @classmethod
    def validate_iso_date_input(cls, value):
        return _iso_date_input(value)

    @model_validator(mode="after")
    def validate_patch(self):
        supplied = self.model_fields_set
        if not supplied:
            raise ValueError("at least one field is required")
        if any(getattr(self, field) is None for field in supplied - {"notes"}):
            raise ValueError("null is only allowed for notes")
        validators = {
            "name": ManualAccountFields.validate_name,
            "asset_category": ManualAccountFields.validate_category,
            "current_balance": ManualAccountFields.validate_balance,
            "as_of_date": ManualAccountFields.validate_date,
            "notes": ManualAccountFields.validate_notes,
        }
        for field, validator in validators.items():
            if field in supplied:
                validator(getattr(self, field))
        return self

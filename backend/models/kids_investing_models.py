from datetime import date

from pydantic import BaseModel


class KidsInvestChildCreate(BaseModel):
    name: str
    birthday: date | None = None
    display_color: str | None = None
    display_order: int = 0


class KidsInvestChildUpdate(BaseModel):
    name: str | None = None
    birthday: date | None = None
    display_color: str | None = None
    display_order: int | None = None
    active: bool | None = None


class KidsInvestAccountCreate(BaseModel):
    child_id: str
    account_label: str
    account_type: str = "trump_account"
    custodian: str | None = "Fidelity"
    notes: str | None = None


class KidsInvestLotCreate(BaseModel):
    account_id: str
    ticker: str
    purchase_date: date
    total_investment: float
    purchase_price: float
    notes: str | None = None


class KidsInvestLotUpdate(BaseModel):
    ticker: str | None = None
    purchase_date: date | None = None
    total_investment: float | None = None
    purchase_price: float | None = None
    notes: str | None = None


class KidsInvestPriceUpsert(BaseModel):
    ticker: str
    price_date: date
    close_price: float
    source: str = "manual"
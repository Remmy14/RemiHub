from pydantic import BaseModel


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
    
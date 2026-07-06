# Python Imports

# 3rd Party Imports
from fastapi import APIRouter, HTTPException

# Local Imports
from backend.models.finance_models import (
    FinanceAccountUpdate,
    FinanceHistoryPoint,
    FinanceSummary,
)
from backend.services import finance_service


router = APIRouter(prefix="/finance", tags=["Finance"])


@router.get("/summary", response_model=FinanceSummary)
def get_summary():
    summary = finance_service.get_finance_summary()

    if not summary:
        raise HTTPException(status_code=404, detail="No finance snapshots found")

    return summary


@router.get("/history", response_model=list[FinanceHistoryPoint])
def get_history(limit: int = 24):
    return finance_service.get_finance_history(limit=limit)


@router.get("/accounts")
def get_accounts():
    return {
        "success": True,
        "data": finance_service.get_finance_accounts(),
    }


@router.patch("/accounts/{account_id}")
def update_account(account_id: str, req: FinanceAccountUpdate):
    try:
        updated = finance_service.update_finance_account(
            account_id=account_id,
            include_in_net_worth=req.include_in_net_worth,
            asset_category=req.asset_category,
            display_name=req.display_name,
            display_order=req.display_order,
        )

        return {
            "success": True,
            "data": updated,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
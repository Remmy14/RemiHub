# Python Imports

# 3rd Party Imports
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from backend.core.auth import require_admin_principal, require_current_principal

# Local Imports
from backend.models.finance_models import (
    FinanceAccountUpdate,
    FinanceHistoryPoint,
    FinanceSummary,
    ManualAccountCreate,
    ManualAccountPatch,
)
from backend.services import finance_service
from backend.services import manual_finance_service as manual_service


router = APIRouter(prefix="/finance", tags=["Finance"])


def _manual_error(exc: Exception):
    if isinstance(exc, manual_service.AccountNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, manual_service.AccountConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, manual_service.AccountInvalid):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.get("/manual-accounts", dependencies=[Depends(require_admin_principal)])
def list_manual_accounts(include_archived: bool = False):
    return {"success": True, "data": manual_service.list_accounts(include_archived=include_archived)}


@router.post("/manual-accounts", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_admin_principal)])
def create_manual_account(request: ManualAccountCreate):
    try:
        return {"success": True, "data": manual_service.create_account(request)}
    except Exception as exc:
        _manual_error(exc)


@router.get("/manual-accounts/{account_id}", dependencies=[Depends(require_admin_principal)])
def get_manual_account(account_id: UUID):
    try:
        return {"success": True, "data": manual_service.get_account(account_id)}
    except Exception as exc:
        _manual_error(exc)


@router.patch("/manual-accounts/{account_id}", dependencies=[Depends(require_admin_principal)])
def patch_manual_account(account_id: UUID, request: ManualAccountPatch):
    try:
        return {"success": True, "data": manual_service.update_account(account_id, request)}
    except Exception as exc:
        _manual_error(exc)


@router.get("/summary", response_model=FinanceSummary,
            dependencies=[Depends(require_current_principal)])
def get_summary():
    summary = finance_service.get_finance_summary()

    if not summary:
        raise HTTPException(status_code=404, detail="No finance snapshots found")

    return summary


@router.get("/history", response_model=list[FinanceHistoryPoint],
            dependencies=[Depends(require_current_principal)])
def get_history(limit: int = 24):
    return finance_service.get_finance_history(limit=limit)


@router.get("/accounts", dependencies=[Depends(require_current_principal)])
def get_accounts():
    return {
        "success": True,
        "data": finance_service.get_finance_accounts(),
    }


@router.patch("/accounts/{account_id}", dependencies=[Depends(require_current_principal)])
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

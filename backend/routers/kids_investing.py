from fastapi import APIRouter, HTTPException

from backend.models.kids_investing_models import (
    KidsInvestAccountCreate,
    KidsInvestChildCreate,
    KidsInvestChildUpdate,
    KidsInvestLotCreate,
    KidsInvestLotUpdate,
    KidsInvestPriceUpsert,
)
from backend.services import kids_investing_service


router = APIRouter(prefix="/kids-investing", tags=["Kids Investing"])


@router.get("/overview")
def get_overview():
    return {
        "success": True,
        "data": kids_investing_service.get_overview(),
    }


@router.get("/children")
def get_children(active_only: bool = True):
    return {
        "success": True,
        "data": kids_investing_service.get_children(active_only=active_only),
    }


@router.post("/children")
def create_child(req: KidsInvestChildCreate):
    return {
        "success": True,
        "data": kids_investing_service.create_child(
            name=req.name,
            birthday=req.birthday,
            display_color=req.display_color,
            display_order=req.display_order,
        ),
    }


@router.patch("/children/{child_id}")
def update_child(child_id: str, req: KidsInvestChildUpdate):
    try:
        return {
            "success": True,
            "data": kids_investing_service.update_child(
                child_id,
                **req.model_dump(exclude_unset=True),
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/children/{child_id}")
def get_child_detail(child_id: str):
    try:
        return {
            "success": True,
            "data": kids_investing_service.get_child_detail(child_id),
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/accounts")
def get_accounts(child_id: str | None = None):
    return {
        "success": True,
        "data": kids_investing_service.get_accounts(child_id),
    }


@router.post("/accounts")
def create_account(req: KidsInvestAccountCreate):
    return {
        "success": True,
        "data": kids_investing_service.create_account(
            child_id=req.child_id,
            account_label=req.account_label,
            account_type=req.account_type,
            custodian=req.custodian,
            notes=req.notes,
        ),
    }


@router.post("/lots")
def create_lot(req: KidsInvestLotCreate):
    try:
        return {
            "success": True,
            "data": kids_investing_service.create_lot(
                account_id=req.account_id,
                ticker=req.ticker,
                purchase_date=req.purchase_date,
                total_investment=req.total_investment,
                purchase_price=req.purchase_price,
                notes=req.notes,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/lots/{lot_id}")
def update_lot(lot_id: str, req: KidsInvestLotUpdate):
    try:
        return {
            "success": True,
            "data": kids_investing_service.update_lot(
                lot_id,
                **req.model_dump(exclude_unset=True),
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/lots/{lot_id}")
def delete_lot(lot_id: str):
    try:
        return {
            "success": True,
            "data": kids_investing_service.delete_lot(lot_id),
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/prices")
def upsert_price(req: KidsInvestPriceUpsert):
    return {
        "success": True,
        "data": kids_investing_service.upsert_price(
            ticker=req.ticker,
            price_date=req.price_date,
            close_price=req.close_price,
            source=req.source,
        ),
    }


@router.post("/snapshots/daily")
def create_daily_snapshots():
    return {
        "success": True,
        "data": kids_investing_service.create_daily_snapshots(),
    }


@router.get("/history")
def get_history(child_id: str | None = "all", limit: int = 365):
    return {
        "success": True,
        "data": kids_investing_service.get_history(
            child_id=child_id,
            limit=limit,
        ),
    }
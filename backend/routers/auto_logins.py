# backend/routers/auto_logins.py
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from backend.services.auto_login_service import AutoLoginService, AutoLoginResult

router = APIRouter(prefix='/autologin', tags=['Autologin'])

class AutoLoginRequest(BaseModel):
    network: str = Field(..., description='e.g. "fanduel"')
    code: str = Field(..., description='Activation code shown on TV')

class AutoLoginResponse(BaseModel):
    success: bool
    message: str
    logs: list[str]
    elapsed_sec: float

@router.post('', response_model=AutoLoginResponse)
def auto_login(req: AutoLoginRequest):
    # Parse the login request
    auto_login_service = AutoLoginService()
    res: AutoLoginResult = auto_login_service.authenticate(req.network, req.code)
    if not res.success:
        # 400 for unsupported/missing; 500 for runtime failures—keep it simple:
        if "Unsupported network" in res.message or "Missing" in res.message:
            raise HTTPException(status_code=400, detail=res.message)
        raise HTTPException(status_code=500, detail=res.message)
    return AutoLoginResponse(
        success=True,
        message=res.message,
        logs=res.logs,
        elapsed_sec=res.elapsed_sec,
    )

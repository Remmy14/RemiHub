from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class AutographCreate(BaseModel):
    driver_name: str
    image_path: str
    helmet_view: str
    x_percent: float = Field(ge=0.0, le=1.0)
    y_percent: float = Field(ge=0.0, le=1.0)
    region: Optional[str] = None
    notes: Optional[str] = None


class AutographEntry(AutographCreate):
    id: int
    created_at: datetime
    
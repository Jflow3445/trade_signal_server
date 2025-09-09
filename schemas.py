# schemas.py
from typing import Optional, List, Any
from pydantic import BaseModel, Field
from datetime import datetime


# ---------- Validate ----------
class ValidateRequest(BaseModel):
    email: str
    api_key: str


class ValidateResponse(BaseModel):
    ok: bool
    is_active: bool = True
    plan: str = "free"
    daily_quota: Optional[int] = 1  # None/0 = unlimited
    expires_at: Optional[datetime] = None


# ---------- Signal (POST from sender) ----------
class SignalIn(BaseModel):
    symbol: str
    action: str
    sl_pips: Optional[int] = 0
    tp_pips: Optional[int] = 0
    lot_size: Optional[float] = 0.0
    details: Optional[Any] = None


class SignalOut(BaseModel):
    id: int
    symbol: str
    action: str
    sl_pips: Optional[int] = 0
    tp_pips: Optional[int] = 0
    lot_size: Optional[float] = 0.0
    details: Optional[Any] = None
    created_at: datetime


class SignalsResponse(BaseModel):
    signals: List[SignalOut] = Field(default_factory=list)

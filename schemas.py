from typing import Any, Optional, List, Dict
from pydantic import BaseModel, Field, EmailStr


# ---------- /validate ----------
class ValidateRequest(BaseModel):
    email: EmailStr
    api_key: str


class ValidateResponse(BaseModel):
    ok: bool
    is_active: bool
    plan: Optional[str] = None
    daily_quota: Optional[int] = None
    expires_at: Optional[str] = None  # keep for compatibility


# ---------- /signals ----------
class SignalCreate(BaseModel):
    symbol: str
    action: str
    sl_pips: Optional[int] = Field(default=None, ge=0)
    tp_pips: Optional[int] = Field(default=None, ge=0)
    lot_size: Optional[float] = None
    details: Optional[Dict[str, Any]] = None


class SignalOut(BaseModel):
    id: int
    symbol: str
    action: str
    sl_pips: Optional[int] = None
    tp_pips: Optional[int] = None
    lot_size: Optional[float] = None
    details: Optional[Dict[str, Any]] = None
    created_at: str

from pydantic import BaseModel, EmailStr
from typing import Optional, Dict, Any, Union
from datetime import datetime

# ===== Signals =====
class TradeSignalBase(BaseModel):
    symbol: str
    action: str
    sl_pips: int
    tp_pips: int
    lot_size: float
    details: Optional[Dict[str, Any]] = None

class TradeSignalCreate(TradeSignalBase):
    user_id: Optional[int] = None

class TradeSignalOut(TradeSignalBase):
    id: int
    created_at: datetime
    user_id: Optional[int]
    class Config:
        orm_mode = True

class LatestSignalOut(TradeSignalBase):
    id: int
    updated_at: datetime
    user_id: Optional[int]
    class Config:
        orm_mode = True

# ===== Trades =====
class TradeRecordBase(BaseModel):
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    volume: float
    pnl: float
    duration: Optional[Union[str, float, int]] = None
    open_time: Optional[Union[datetime, float, int]] = None
    close_time: Optional[Union[datetime, float, int]] = None
    details: Optional[Dict[str, Any]] = None

class TradeRecordCreate(TradeRecordBase):
    user_id: Optional[int] = None

class TradeRecordOut(TradeRecordBase):
    id: int
    user_id: Optional[int]
    class Config:
        orm_mode = True

# ===== Admin token issue/renew =====
class AdminIssueTokenRequest(BaseModel):
    email: EmailStr
    username: Optional[str] = None
    plan: str                 # "free" | "silver" | "gold"
    months: int = 1           # validity window for non-free
    daily_quota: Optional[int] = None

class AdminIssueTokenResponse(BaseModel):
    email: EmailStr
    username: str
    plan: str
    token: str                # = api_key (alias for clarity to WP/EA)
    api_key: str              # kept for backward-compat
    daily_quota: Optional[int]
    expires_at: Optional[datetime]
    is_active: bool

# ===== EA validate =====
class ValidateRequest(BaseModel):
    email: EmailStr
    api_key: str
    consume: Optional[bool] = False  # if true, decrement the daily quota

class ValidateResponse(BaseModel):
    ok: bool
    plan: Optional[str] = None
    daily_quota: Optional[int] = None
    remaining_today: Optional[int] = None
    expires_at: Optional[datetime] = None
    is_active: Optional[bool] = None
    reason: Optional[str] = None

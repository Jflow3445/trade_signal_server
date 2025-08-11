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
    username: str
    plan: str  # free|silver|gold
    # optional overrides
    daily_quota: Optional[int] = None
    months_valid: Optional[int] = 1  # free can be None => no expiry

class AdminIssueTokenResponse(BaseModel):
    email: EmailStr
    username: str
    plan: str
    api_key: str
    daily_quota: Optional[int]
    expires_at: Optional[datetime]

# ===== EA validate =====
class ValidateRequest(BaseModel):
    email: EmailStr
    api_key: str

class ValidateResponse(BaseModel):
    ok: bool
    plan: str
    remaining_today: Optional[int]  # None => unlimited
    expires_at: Optional[datetime]

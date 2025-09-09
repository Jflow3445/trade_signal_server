# schemas.py
from __future__ import annotations
from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

# ---- Signals ----
class TradeSignalBase(BaseModel):
    symbol: str
    action: str
    sl_pips: int = 1
    tp_pips: int = 1
    lot_size: float = 0.01
    details: Optional[Dict[str, Any]] = None

class TradeSignalCreate(TradeSignalBase):
    pass

class TradeSignalOut(TradeSignalBase):
    id: int
    user_id: int
    created_at: datetime
    class Config:
        orm_mode = True

class LatestSignalOut(TradeSignalBase):
    id: int
    user_id: int
    updated_at: datetime
    class Config:
        orm_mode = True

# ---- Trades ----
class TradeRecordCreate(BaseModel):
    symbol: str
    side: str
    entry_price: float
    exit_price: Optional[float] = None
    volume: float
    pnl: Optional[float] = None
    duration: Optional[str] = None
    open_time: Optional[datetime] = None
    close_time: Optional[datetime] = None
    details: Optional[Dict[str, Any]] = None

class TradeRecordOut(TradeRecordCreate):
    id: int
    user_id: int
    created_at: datetime
    class Config:
        orm_mode = True

# ---- Admin ----
class AdminIssueTokenRequest(BaseModel):
    email: Optional[str] = None
    username: str
    plan: str
    daily_quota: Optional[int] = None     # None = plan default
    months_valid: Optional[int] = None

class AdminIssueTokenResponse(BaseModel):
    email: Optional[str] = None
    username: str
    plan: str
    token: str
    api_key: str
    daily_quota: Optional[int] = None
    expires_at: Optional[datetime] = None
    is_active: bool

# ---- Validate (EA) ----
class ValidateRequest(BaseModel):
    email: str
    api_key: str

class ValidateResponse(BaseModel):
    ok: bool
    plan: Optional[str] = None
    daily_quota: Optional[int] = None  # None => unlimited
    expires_at: Optional[datetime] = None
    is_active: Optional[bool] = None

# ---- EA sync (optional) ----
class EAOpenPosition(BaseModel):
    ticket: int
    symbol: str
    side: str
    volume: float
    entry_price: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    open_time: Optional[datetime] = None
    magic: Optional[int] = None
    comment: Optional[str] = None

class EASyncRequest(BaseModel):
    account_id: str
    broker_server: str
    hwid: Optional[str] = None
    positions: Optional[List[EAOpenPosition]] = Field(default_factory=list)

class EASyncResponse(BaseModel):
    updated: int

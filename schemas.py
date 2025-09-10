from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from datetime import datetime

# -------- Trade Signal --------
class TradeSignalCreate(BaseModel):
    symbol: str
    action: str
    sl_pips: Optional[int] = None
    tp_pips: Optional[int] = None
    lot_size: Optional[float] = None
    details: Optional[Dict[str, Any]] = None

class TradeSignalOut(BaseModel):
    id: int
    symbol: str
    action: str
    sl_pips: Optional[int] = None
    tp_pips: Optional[int] = None
    lot_size: Optional[float] = None
    details: Optional[Dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True

class LatestSignalOut(BaseModel):
    signals: List[TradeSignalOut]

# -------- Trade Record --------
class TradeRecordCreate(BaseModel):
    action: str
    symbol: str
    details: Optional[Dict[str, Any]] = None

class TradeRecordOut(BaseModel):
    id: int
    action: str
    symbol: str
    details: Optional[Dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True

# -------- Admin --------
class AdminIssueTokenRequest(BaseModel):
    username_or_email: str
    token: str
    plan: str

class AdminIssueTokenResponse(BaseModel):
    username: str
    email: Optional[str]
    token: str
    plan: str
    is_active: bool

# -------- Validate --------
class ValidateRequest(BaseModel):
    email: str
    api_key: str

class ValidateResponse(BaseModel):
    ok: bool
    is_active: Optional[bool] = None
    plan: Optional[str] = None
    daily_quota: Optional[int] = None
    expires_at: Optional[datetime] = None

# -------- EA sync --------
class EASyncRequest(BaseModel):
    email: str
    api_key: str

class EASyncResponse(BaseModel):
    ok: bool
    plan: Optional[str] = None
    daily_quota: Optional[int] = None
    unlimited: Optional[bool] = None

# -------- Activations --------
class ActivationItem(BaseModel):
    username: str
    email: Optional[str] = None
    plan: Optional[str] = None

class ActivationsList(BaseModel):
    items: List[ActivationItem] = Field(default_factory=list)

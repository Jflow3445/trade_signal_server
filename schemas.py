from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from datetime import datetime

# ---- Signals ----
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
    lot_size: Optional[str] = None
    # Be tolerant to legacy rows (list/str/None) to avoid 500s
    details: Optional[Any] = None
    created_at: datetime
    class Config:
        from_attributes = True

class LatestSignalOut(BaseModel):
    items: List[TradeSignalOut] = Field(default_factory=list)

# ---- Trade records ----
class TradeRecordCreate(BaseModel):
    symbol: str
    action: str
    details: Optional[Dict[str, Any]] = None

class TradeRecordOut(BaseModel):
    id: int
    symbol: str
    action: str
    details: Optional[Dict[str, Any]] = None
    created_at: datetime
    class Config:
        from_attributes = True

# ---- Admin/user ----
class UserOut(BaseModel):
    id: int
    username: str
    email: Optional[str] = None
    plan: str
    api_key: Optional[str] = None
    is_active: bool
    class Config:
        from_attributes = True

class PlanChangeIn(BaseModel):
    user_id: Optional[int] = None
    username: Optional[str] = None
    email: Optional[str] = None
    plan: str
    rotate: Optional[bool] = False

class PlanChangeOut(BaseModel):
    ok: bool
    user: UserOut
    plan: str
    rotated: bool
    api_key: str
    daily_quota: Optional[int] = None
    unlimited: Optional[bool] = None

class VerifyOut(BaseModel):
    ok: bool
    plan: Optional[str] = None
    daily_quota: Optional[int] = None
    unlimited: Optional[bool] = None

class ActivationsList(BaseModel):
    items: List[UserOut] = Field(default_factory=list)

# ---- Admin issue token (kept from existing server behaviour) ----
class AdminIssueTokenRequest(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None
    user_id: Optional[int] = None
    plan: str = "free"
    rotate: bool = True

class AdminIssueTokenResponse(BaseModel):
    username: str
    email: Optional[str] = None
    plan: str
    api_key: str
    rotated: bool

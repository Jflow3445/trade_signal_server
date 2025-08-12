from __future__ import annotations
from typing import Optional, Dict, Any, List, Annotated
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, ConfigDict
from pydantic import StringConstraints  # pydantic v2

# ---------- Type aliases (Pylance-friendly) ----------
SymbolStr = Annotated[str, StringConstraints(
    strip_whitespace=True,
    to_upper=True,
    pattern=r'^[A-Z.\-]{3,20}$'
)]
ActionStr = Annotated[str, StringConstraints(
    strip_whitespace=True,
    to_lower=True,
    pattern=r'^[a-z_]{2,20}$'
)]

Pips     = Annotated[int,   Field(ge=1, le=5000)]
LotSize  = Annotated[float, Field(gt=0, lt=1000)]
PosFlt   = Annotated[float, Field(gt=0)]
NonNegF  = Annotated[float, Field(ge=0)]

# ===== Signals =====
class TradeSignalBase(BaseModel):
    symbol: SymbolStr
    action: ActionStr
    sl_pips: Pips
    tp_pips: Pips
    lot_size: LotSize
    details: Optional[Dict[str, Any]] = None

class TradeSignalCreate(TradeSignalBase):
    pass

class TradeSignalOut(TradeSignalBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    created_at: datetime

class LatestSignalOut(TradeSignalBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    updated_at: datetime

# ===== Trades =====
class TradeRecordBase(BaseModel):
    symbol: SymbolStr
    side: Annotated[str, StringConstraints(to_lower=True, pattern=r'^(buy|sell)$')]
    entry_price: PosFlt
    exit_price: Optional[NonNegF] = None
    volume: LotSize  # same bounds are fine for volume
    pnl: Optional[float] = None
    duration: Optional[str] = None
    open_time: Optional[datetime] = None
    close_time: Optional[datetime] = None
    details: Optional[Dict[str, Any]] = None

class TradeRecordCreate(TradeRecordBase):
    pass

class TradeRecordOut(TradeRecordBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    created_at: datetime

# ===== Admin token issuance =====
class AdminIssueTokenRequest(BaseModel):
    email: Optional[EmailStr] = None
    username: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=3, max_length=64)]
    plan: Annotated[str, StringConstraints(to_lower=True, pattern=r'^(free|silver|gold)$')] = "free"
    daily_quota: Optional[int] = Field(default=None, description="None means unlimited")
    months_valid: Optional[int] = Field(default=None, ge=1, le=36)

class AdminIssueTokenResponse(BaseModel):
    email: Optional[EmailStr]
    username: str
    plan: str
    token: str
    api_key: str
    daily_quota: Optional[int] = None
    expires_at: Optional[datetime] = None
    is_active: bool

# ===== Validate =====
class ValidateRequest(BaseModel):
    email: EmailStr
    api_key: str

class ValidateResponse(BaseModel):
    ok: bool
    plan: Optional[str] = None
    daily_quota: Optional[int] = None
    expires_at: Optional[datetime] = None
    is_active: Optional[bool] = None

# ===== EA Open positions sync =====
class EAOpenPosition(BaseModel):
    ticket: int
    symbol: SymbolStr
    side: Annotated[str, StringConstraints(to_lower=True, pattern=r'^(buy|sell)$')]
    volume: LotSize
    entry_price: PosFlt
    sl: Optional[NonNegF] = None
    tp: Optional[NonNegF] = None
    open_time: Optional[datetime] = None
    magic: Optional[int] = None
    comment: Optional[str] = Field(default=None, max_length=255)

class EASyncRequest(BaseModel):
    account_id: str
    broker_server: str
    positions: List[EAOpenPosition]

class EASyncResponse(BaseModel):
    ok: bool
    upserted: int

# ===== Activations (optional admin view) =====
class ActivationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: str
    broker_server: str
    hwid: Optional[str] = None
    created_at: datetime
    last_seen_at: datetime

class ActivationsList(BaseModel):
    email: EmailStr
    plan: str
    used: int
    limit: Optional[int]
    items: List[ActivationOut]

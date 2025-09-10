from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, EmailStr
from datetime import datetime

# -----------------------------
# USERS
# -----------------------------
class UserBase(BaseModel):
    username: str
    email: Optional[EmailStr] = None
    is_active: bool = True

class UserCreate(UserBase):
    api_key: str
    plan: Optional[str] = "free"
    daily_quota: Optional[int] = None
    expires_at: Optional[datetime] = None

class UserOut(UserBase):
    id: int
    api_key: str
    plan: Optional[str]
    daily_quota: Optional[int]
    expires_at: Optional[datetime]

    class Config:
        from_attributes = True

class UserPlanUpdate(BaseModel):
    plan: Optional[str] = None
    daily_quota: Optional[int] = None
    expires_at: Optional[datetime] = None

# -----------------------------
# SIGNALS
# -----------------------------
class SignalBase(BaseModel):
    symbol: str = Field(..., max_length=16)
    action: str = Field(..., description="buy|sell|adjust_sl|tp|close")
    sl_pips: Optional[int] = None
    tp_pips: Optional[int] = None
    lot_size: Optional[float] = None
    details: Optional[Dict[str, Any]] = None

class SignalCreate(SignalBase):
    pass

class SignalOut(SignalBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True

# -----------------------------
# SUBSCRIPTIONS
# -----------------------------
class SubscriptionCreate(BaseModel):
    receiver_id: int
    sender_id: int

class SubscriptionOut(BaseModel):
    id: int
    receiver_id: int
    sender_id: int

    class Config:
        from_attributes = True

# -----------------------------
# VALIDATION
# -----------------------------
class ValidateRequest(BaseModel):
    email: Optional[EmailStr] = None
    api_key: Optional[str] = None

class ValidateResponse(BaseModel):
    ok: bool
    is_active: bool
    plan: str
    daily_quota: Optional[int]
    expires_at: Optional[datetime] = None

from typing import Optional, Any
import datetime
from pydantic import BaseModel


# ----------------------------
# User schemas
# ----------------------------

class UserBase(BaseModel):
    username: str
    email: Optional[str] = None

class UserCreate(UserBase):
    api_key: str
    plan: str = "free"
    daily_quota: Optional[int] = None
    is_active: bool = True
    expires_at: Optional[datetime.datetime] = None

class User(UserBase):
    id: int
    api_key: str
    plan: str
    daily_quota: Optional[int]
    is_active: bool
    expires_at: Optional[datetime.datetime]

    class Config:
        orm_mode = True


# ----------------------------
# Signal schemas
# ----------------------------

class SignalBase(BaseModel):
    symbol: str
    action: str
    sl_pips: Optional[int] = None
    tp_pips: Optional[int] = None
    lot_size: Optional[float] = None
    details: Optional[Any] = None

class SignalCreate(SignalBase):
    pass

class Signal(SignalBase):
    id: int
    created_at: datetime.datetime

    class Config:
        orm_mode = True


# ----------------------------
# Subscription schemas
# ----------------------------

class Subscription(BaseModel):
    id: int
    receiver_id: int
    sender_id: int

    class Config:
        orm_mode = True

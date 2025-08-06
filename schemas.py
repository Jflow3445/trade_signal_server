from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime

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

# --- For upserting latest signal ---
class LatestSignalOut(TradeSignalBase):
    id: int
    updated_at: datetime
    user_id: Optional[int]
    class Config:
        orm_mode = True

# --- For trade record ---
class TradeRecordBase(BaseModel):
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    volume: float
    pnl: float
    duration: str
    open_time: datetime
    close_time: datetime
    details: Optional[Dict[str, Any]] = None

class TradeRecordCreate(TradeRecordBase):
    user_id: Optional[int] = None

class TradeRecordOut(TradeRecordBase):
    id: int
    user_id: Optional[int]
    class Config:
        orm_mode = True

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

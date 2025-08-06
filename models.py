from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    api_key = Column(String, unique=True, index=True)
    tier = Column(String, default="free")
    quota = Column(Integer, default=1)

class TradeSignal(Base):
    __tablename__ = "trade_signals"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    action = Column(String)
    sl_pips = Column(Integer)
    tp_pips = Column(Integer)
    lot_size = Column(Float)
    details = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

# --- NEW: latest signal per symbol ---
class LatestSignal(Base):
    __tablename__ = "latest_signals"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True, unique=True)
    action = Column(String)
    sl_pips = Column(Integer)
    tp_pips = Column(Integer)
    lot_size = Column(Float)
    details = Column(JSON)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

# --- NEW: record of closed trades ---
class TradeRecord(Base):
    __tablename__ = "trade_records"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    side = Column(String)        # buy/sell
    entry_price = Column(Float)
    exit_price = Column(Float)
    volume = Column(Float)
    pnl = Column(Float)
    duration = Column(String)
    open_time = Column(DateTime(timezone=True))
    close_time = Column(DateTime(timezone=True))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    details = Column(JSON, nullable=True)

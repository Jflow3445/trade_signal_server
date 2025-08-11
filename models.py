from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, ForeignKey, UniqueConstraint, Boolean
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)

    # existing
    username = Column(String, unique=True, index=True)
    api_key  = Column(String, unique=True, index=True)
    tier     = Column(String, default="free")
    quota    = Column(Integer, default=1)

    # new fields you added in DB
    email           = Column(String, unique=True, nullable=True)
    plan            = Column(String, default="free")            # free|silver|gold
    daily_quota     = Column(Integer, default=1, nullable=True) # None => unlimited
    used_today      = Column(Integer, default=0)
    usage_reset_at  = Column(DateTime(timezone=True), nullable=True)
    expires_at      = Column(DateTime(timezone=True), nullable=True)
    is_active       = Column(Boolean, default=True)

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

class TradeRecord(Base):
    __tablename__ = "trade_records"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    side = Column(String)
    entry_price = Column(Float)
    exit_price = Column(Float)
    volume = Column(Float)
    pnl = Column(Float)
    duration = Column(String, nullable=True)
    open_time = Column(DateTime(timezone=True), nullable=True)
    close_time = Column(DateTime(timezone=True), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    details = Column(JSON, nullable=True)

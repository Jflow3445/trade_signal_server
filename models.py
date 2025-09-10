from datetime import datetime
from typing import Optional, Dict, Any
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, JSON, Text
)
from sqlalchemy.orm import relationship
from database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)

    username = Column(String(64), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=True)

    # Legacy per-user token
    api_key = Column(String(128), unique=True, index=True, nullable=True)

    is_active = Column(Boolean, default=True)

    # Basic plan flags (legacy default path)
    plan = Column(String(16), nullable=True)          # free / silver / gold
    daily_quota = Column(Integer, nullable=True)      # None = unlimited (gold)
    plan_upgraded_at = Column(DateTime, nullable=True)

    # Relations
    tokens = relationship("APIToken", back_populates="user")

class APIToken(Base):
    __tablename__ = "api_tokens"
    id = Column(Integer, primary_key=True)
    token = Column(String(128), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_active = Column(Boolean, default=True)
    plan = Column(String(16), nullable=True)  # per-token plan override

    user = relationship("User", back_populates="tokens")

class TradeSignal(Base):
    __tablename__ = "trade_signals"
    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)  # sender
    symbol = Column(String(20), nullable=False)
    action = Column(String(20), nullable=False)  # buy / sell / adjust_sl / close / ...
    sl_pips = Column(Integer, nullable=True)
    tp_pips = Column(Integer, nullable=True)
    lot_size = Column(String(32), nullable=True)
    details = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    receiver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)

class SignalRead(Base):
    __tablename__ = "signal_reads"
    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("trade_signals.id"), nullable=False)
    receiver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    read_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Per-token dedupe & per-token usage accounting
    token_hash = Column(String(128), nullable=True)

class TradeRecord(Base):
    __tablename__ = "trade_records"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)  # receiver who placed trade
    action = Column(String(32), nullable=False)
    symbol = Column(String(20), nullable=False)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

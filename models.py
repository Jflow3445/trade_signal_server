from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, JSON, UniqueConstraint, Index
)
from sqlalchemy.orm import relationship
from database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)

    username = Column(String(64), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=True)

    # Legacy single API key (for older clients)
    api_key = Column(String(128), unique=True, nullable=True)

    # Account status
    is_active = Column(Boolean, default=True, index=True)

    # Server-side plan truth
    plan = Column(String(16), default="free", index=True)  # free/silver/gold
    daily_quota = Column(Integer, nullable=True)          # None => unlimited (gold)
    plan_upgraded_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relations
    tokens = relationship("APIToken", back_populates="user", cascade="all, delete-orphan")

class APIToken(Base):
    __tablename__ = "api_tokens"
    id = Column(Integer, primary_key=True)
    token = Column(String(128), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan = Column(String(16), default="free", index=True)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="tokens")

    __table_args__ = (
        Index("ix_api_tokens_user_active", "user_id", "is_active"),
    )

class TradeSignal(Base):
    __tablename__ = "trade_signals"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)  # sender id
    symbol = Column(String(20), nullable=False)
    action = Column(String(20), nullable=False)  # buy/sell/adjust_sl/adjust_tp/close/hold
    sl_pips = Column(Integer, nullable=True)
    tp_pips = Column(Integer, nullable=True)
    lot_size = Column(String(32), nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    receiver_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)  # EA user
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)    # Signal source
    __table_args__ = (UniqueConstraint("receiver_id", "sender_id", name="uq_subscription_pair"),)

class SignalRead(Base):
    __tablename__ = "signal_reads"
    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("trade_signals.id"), nullable=False, index=True)
    receiver_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(128), nullable=True, index=True)
    read_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("signal_id", "receiver_id", "token_hash", name="uq_signal_read_dedupe"),)

class TradeRecord(Base):
    __tablename__ = "trade_records"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)  # receiver
    action = Column(String(32), nullable=False)
    symbol = Column(String(20), nullable=False)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

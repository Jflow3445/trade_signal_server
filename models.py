from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, UniqueConstraint, Index
)
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from database import Base


class User(Base):
    __tablename__ = "users"
    id          = Column(Integer, primary_key=True)
    username    = Column(String(64), nullable=False, unique=True)
    email       = Column(String(255), nullable=True, unique=False)
    api_key     = Column(String(128), nullable=False, unique=True, index=True)
    is_active   = Column(Boolean, nullable=False, default=True)

    # Plan / quota (NULL daily_quota => unlimited)
    plan                = Column(String(16), nullable=True)       # 'free' | 'silver' | 'gold'
    daily_quota         = Column(Integer, nullable=True)          # 1 | 3 | NULL (unlimited)
    plan_upgraded_at    = Column(DateTime(timezone=True), nullable=True)

    created_at  = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    sent_signals = relationship("TradeSignal", back_populates="sender", lazy="selectin")


class Subscription(Base):
    __tablename__ = "subscriptions"
    id           = Column(Integer, primary_key=True)
    receiver_id  = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    sender_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    __table_args__ = (
        UniqueConstraint("receiver_id", "sender_id", name="uniq_subscription"),
    )


class TradeSignal(Base):
    __tablename__ = "trade_signals"
    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)  # sender
    symbol     = Column(String(32), nullable=False)
    action     = Column(String(16), nullable=False)  # buy|sell|adjust_sl|adjust_tp|close|hold
    sl_pips    = Column(Integer, nullable=True)
    tp_pips    = Column(Integer, nullable=True)
    lot_size   = Column(String(32), nullable=True)   # keep as text or numeric; many brokers use decimals
    details    = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    sender = relationship("User", back_populates="sent_signals")


class SignalRead(Base):
    __tablename__ = "signal_reads"
    id          = Column(Integer, primary_key=True)
    signal_id   = Column(Integer, ForeignKey("trade_signals.id", ondelete="CASCADE"), nullable=False)
    receiver_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)   # optional
    token_hash  = Column(String(128), nullable=True)  # sha256 of Bearer token (per-token accounting)
    read_at     = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # IMPORTANT: Avoid double-counting the same signal for the same token
    __table_args__ = (
        Index("uniq_signal_reads_token_signal", "token_hash", "signal_id", unique=True, postgresql_where=(token_hash != None)),
    )

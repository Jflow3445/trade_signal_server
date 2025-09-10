from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from .database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)

    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=True)

    is_active = Column(Boolean, default=True)
    api_key = Column(String(255), unique=True, index=True, nullable=False)

    plan = Column(String(16), nullable=True)        # free|silver|gold
    daily_quota = Column(Integer, nullable=True)    # null => unlimited (gold)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    # relationships
    signals = relationship("TradeSignal", back_populates="user", lazy="selectin")
    subscriptions = relationship("Subscription", back_populates="receiver", foreign_keys="Subscription.receiver_id", lazy="selectin")

class TradeSignal(Base):
    __tablename__ = "trade_signals"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    symbol = Column(String(16), nullable=False)
    action = Column(String(16), nullable=False)  # buy|sell|adjust_sl|tp|close
    sl_pips = Column(Integer, nullable=True)
    tp_pips = Column(Integer, nullable=True)
    lot_size = Column(String(32), nullable=True)  # keep as string to avoid float rounding issues
    details = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("User", back_populates="signals")

class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    receiver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    receiver = relationship("User", back_populates="subscriptions", foreign_keys=[receiver_id])
    sender = relationship("User", foreign_keys=[sender_id])

class SignalRead(Base):
    __tablename__ = "signal_reads"

    id = Column(Integer, primary_key=True, index=True)
    receiver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    signal_id = Column(Integer, ForeignKey("trade_signals.id"), nullable=False)

    # optional: per-token hash to ensure idempotence per API token
    token_hash = Column(String(128), nullable=True)

    read_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

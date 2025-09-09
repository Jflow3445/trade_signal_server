# models.py
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date, Float, JSON,
    UniqueConstraint, Index, Text
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()

UTC = timezone.utc
def utcnow() -> datetime:
    return datetime.now(tz=UTC)

# ---------------- Users ----------------
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=False, nullable=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)

    # IMPORTANT: quotas are accounted per api_key (token), not id/email
    api_key = Column(String(128), unique=True, nullable=False, index=True)

    plan = Column(String(32), nullable=False, default="free")  # free/silver/gold/...
    # Optional daily quota override; None = use plan default; also None can mean unlimited
    daily_quota = Column(Integer, nullable=True)

    is_active = Column(Boolean, default=True, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)

    expires_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

# --------------- Signals (history) ---------------
class TradeSignal(Base):
    __tablename__ = "trade_signals"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)  # sender id
    symbol = Column(String(32), nullable=False, index=True)
    action = Column(String(16), nullable=False)            # buy/sell/adjust_sl/adjust_tp/close/hold
    sl_pips = Column(Integer, nullable=False, default=1)
    tp_pips = Column(Integer, nullable=False, default=1)
    lot_size = Column(Float, nullable=False, default=0.01)
    details = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_trade_signals_user_created", "user_id", "created_at"),
    )

# --------------- Latest per (user, symbol) ---------------
class LatestSignal(Base):
    __tablename__ = "latest_signals"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)  # sender id
    symbol = Column(String(32), nullable=False, index=True)
    action = Column(String(16), nullable=False)
    sl_pips = Column(Integer, nullable=False, default=1)
    tp_pips = Column(Integer, nullable=False, default=1)
    lot_size = Column(Float, nullable=False, default=0.01)
    details = Column(JSON, nullable=True)

    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_latest_user_symbol"),
        Index("ix_latest_user_updated", "user_id", "updated_at"),
    )

# --------------- Trades log (optional) ---------------
class TradeRecord(Base):
    __tablename__ = "trade_records"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    symbol = Column(String(32), nullable=False)
    side = Column(String(16), nullable=False)  # buy/sell
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    volume = Column(Float, nullable=False)
    pnl = Column(Float, nullable=True)
    duration = Column(String(32), nullable=True)
    open_time = Column(DateTime(timezone=True), nullable=True)
    close_time = Column(DateTime(timezone=True), nullable=True)
    details = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

# --------------- Referral boost (optional) ---------------
class ReferralBoost(Base):
    __tablename__ = "referral_boosts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)

    # "silver" or "gold"
    boost_to = Column(String(16), nullable=False)

    start_at = Column(DateTime(timezone=True), nullable=False)
    end_at = Column(DateTime(timezone=True), nullable=False)

    is_revoked = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

# --------------- Daily consumption (token-bound) ---------------
class DailyConsumption(Base):
    """
    Quota accounting is per API token (api_key) and per day.
    We deliberately avoid a foreign key to users.api_key, because tokens rotate;
    we want historical rows to remain valid while a new token starts a fresh day counter.
    """
    __tablename__ = "daily_consumption"

    id = Column(Integer, primary_key=True)
    api_key = Column(String(128), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    signals_consumed = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("api_key", "date", name="uq_consumption_api_key_date"),
    )

# --------------- EA bookkeeping (optional) ---------------
class Activation(Base):
    __tablename__ = "activations"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    account_id = Column(String(64), nullable=False, index=True)
    broker_server = Column(String(128), nullable=False, index=True)
    hwid = Column(Text, nullable=True)

    last_seen_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_activation_user_account_server", "user_id", "account_id", "broker_server"),
    )

class OpenPosition(Base):
    __tablename__ = "open_positions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    account_id = Column(String(64), nullable=False, index=True)
    broker_server = Column(String(128), nullable=False, index=True)
    ticket = Column(Integer, nullable=False, index=True)
    symbol = Column(String(32), nullable=False)
    side = Column(String(8), nullable=False)  # buy/sell
    volume = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    sl = Column(Float, nullable=True)
    tp = Column(Float, nullable=True)
    open_time = Column(DateTime(timezone=True), nullable=True)
    magic = Column(Integer, nullable=True)
    comment = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

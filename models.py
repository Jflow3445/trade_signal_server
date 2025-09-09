from __future__ import annotations
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, JSON, ForeignKey, Boolean,
    UniqueConstraint, Index, text, Date
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base

# All timestamps are timezone-aware (UTC) at the DB level via timezone=True.

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, index=True, nullable=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    # API token (opaque, Bearer). Store plaintext initially; migrate to hashed later.
    api_key = Column(String(128), unique=True, index=True, nullable=False)

    # Licensing
    plan = Column(String(16), nullable=False, server_default=text("'free'"))
    daily_quota = Column(Integer, nullable=True)  # None means unlimited
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, server_default=text("true"))

    # Meta
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    signals = relationship("TradeSignal", back_populates="user", cascade="all,delete-orphan")
    trades = relationship("TradeRecord", back_populates="user", cascade="all,delete-orphan")
    latest_signals = relationship("LatestSignal", back_populates="user", cascade="all,delete-orphan")
    activations = relationship("Activation", back_populates="user", cascade="all,delete-orphan")
    referral_boosts = relationship("ReferralBoost", back_populates="user", cascade="all,delete-orphan")


class TradeSignal(Base):
    __tablename__ = "trade_signals"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    symbol = Column(String(32), index=True, nullable=False)
    action = Column(String(32), nullable=False)  # buy/sell/adjust_sl/adjust_tp/close/close_all/hold/do_nothing
    sl_pips = Column(Integer, nullable=False)
    tp_pips = Column(Integer, nullable=False)
    lot_size = Column(Float, nullable=False)
    details = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="signals")


class LatestSignal(Base):
    __tablename__ = "latest_signals"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    symbol = Column(String(32), index=True, nullable=False)
    action = Column(String(32), nullable=False)
    sl_pips = Column(Integer, nullable=False)
    tp_pips = Column(Integer, nullable=False)
    lot_size = Column(Float, nullable=False)
    details = Column(JSON, nullable=True)

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="latest_signals")

    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_latest_by_user_symbol"),
        Index("ix_latest_user_updated", "user_id", "updated_at"),
    )


class TradeRecord(Base):
    __tablename__ = "trade_records"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    symbol = Column(String(32), nullable=False)
    side = Column(String(8), nullable=False)  # buy/sell
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    volume = Column(Float, nullable=False)
    pnl = Column(Float, nullable=True)
    duration = Column(String(32), nullable=True)
    open_time = Column(DateTime(timezone=True), nullable=True)
    close_time = Column(DateTime(timezone=True), nullable=True)
    details = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="trades")


class Activation(Base):
    """
    Records EA activations (accounts/devices) for a user, with limits.
    """
    __tablename__ = "activations"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    account_id = Column(String(64), nullable=False)     # MT5 account number string
    broker_server = Column(String(128), nullable=False) # Broker server name
    hwid = Column(String(128), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="activations")

    __table_args__ = (
        UniqueConstraint("user_id", "account_id", "broker_server", name="uq_activation_user_acct_server"),
    )


class OpenPosition(Base):
    """
    Optional: EA open positions sync storage.
    """
    __tablename__ = "open_positions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    account_id = Column(String(64), nullable=False)
    broker_server = Column(String(128), nullable=False)
    ticket = Column(Integer, nullable=False)

    symbol = Column(String(32), nullable=False)
    side = Column(String(8), nullable=False)  # buy/sell
    volume = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    sl = Column(Float, nullable=True)
    tp = Column(Float, nullable=True)
    open_time = Column(DateTime(timezone=True), nullable=True)
    magic = Column(Integer, nullable=True)
    comment = Column(String(255), nullable=True)

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("user_id","account_id","broker_server","ticket", name="uq_openpos_user_acct_server_ticket"),
        Index("ix_openpos_user_updated", "user_id", "updated_at"),
    )


class ReferralBoost(Base):
    """
    Temporary plan boost awarded by referrals.
    boost_to: one of 'silver' or 'gold' (one-tier upgrade target).
    """
    __tablename__ = "referral_boosts"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    boost_to = Column(String(16), nullable=False)  # "silver" or "gold"
    start_at = Column(DateTime(timezone=True), nullable=False)
    end_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    is_revoked = Column(Boolean, nullable=False, server_default=text("false"))

    user = relationship("User", back_populates="referral_boosts")

    __table_args__ = (
        Index("ix_refboost_user_window", "user_id", "start_at", "end_at"),
    )


class DailyConsumption(Base):
    """
    Tracks daily signal consumption per API key for quota enforcement.
    This allows immediate quota changes when users upgrade/downgrade plans.
    """
    __tablename__ = "daily_consumption"
    id = Column(Integer, primary_key=True)
    api_key = Column(String(128), nullable=False, index=True)
    date = Column(Date, nullable=False)
    signals_consumed = Column(Integer, nullable=False, server_default=text("0"))
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("api_key", "date", name="uq_consumption_api_date"),
        Index("ix_consumption_api_date", "api_key", "date"),
    )

from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, ForeignKey, Boolean, UniqueConstraint
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    api_key  = Column(String, unique=True, index=True)
    tier     = Column(String, default="free")
    quota    = Column(Integer, default=1)

    # licensing / auth
    email           = Column(String, unique=True, index=True, nullable=True)
    plan            = Column(String, default="free")            # free|silver|gold
    daily_quota     = Column(Integer, default=1, nullable=True) # None => unlimited
    used_today      = Column(Integer, default=0)
    usage_reset_at  = Column(DateTime(timezone=True), nullable=True)
    expires_at      = Column(DateTime(timezone=True), nullable=True)
    is_active       = Column(Boolean, default=True)

class Activation(Base):
    __tablename__ = "activations"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    account_id = Column(String, nullable=False)        # MT4/MT5 account number as string
    broker_server = Column(String, nullable=False)     # e.g. ICMarketsSC-Demo
    hwid = Column(String, nullable=True)               # optional device fingerprint
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint('user_id', 'account_id', 'broker_server', name='u_user_account_broker'),
    )

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

class OpenPosition(Base):
    __tablename__ = "open_positions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)

    account_id = Column(String(64), index=True, nullable=False)
    broker_server = Column(String(128), index=True, nullable=False)
    hwid = Column(String(128), nullable=True)

    ticket = Column(String(64), nullable=False)             # MT5 ticket as string
    symbol = Column(String(32), nullable=False)
    side = Column(String(8), nullable=False)                # 'buy'/'sell'
    volume = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    sl = Column(Float, nullable=True)
    tp = Column(Float, nullable=True)
    open_time = Column(DateTime, nullable=True)
    magic = Column(Integer, nullable=True)
    comment = Column(String(255), nullable=True)

    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id","account_id","broker_server","ticket", name="uq_openpos_user_acct_server_ticket"),
    )
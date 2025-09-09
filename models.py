import datetime

from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, JSON
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id          = Column(Integer, primary_key=True, index=True)
    username    = Column(String, unique=True, index=True, nullable=False)
    email       = Column(String, unique=True, index=True, nullable=True)
    api_key     = Column(String, unique=True, index=True, nullable=False)

    # subscription / status
    plan        = Column(String, default="free")  # free/silver/gold...
    daily_quota = Column(Integer, nullable=True)  # null = unlimited
    is_active   = Column(Boolean, default=True)
    expires_at  = Column(DateTime, nullable=True)

    # relationships
    sent_signals = relationship("TradeSignal", back_populates="sender", cascade="all,delete-orphan")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id          = Column(Integer, primary_key=True, index=True)
    receiver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    sender_id   = Column(Integer, ForeignKey("users.id"), nullable=False)

    receiver = relationship("User", foreign_keys=[receiver_id])
    sender   = relationship("User", foreign_keys=[sender_id])


class TradeSignal(Base):
    __tablename__ = "trade_signals"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)  # sender
    symbol     = Column(String, index=True, nullable=False)
    action     = Column(String, index=True, nullable=False)  # buy|sell|close|adjust_sl|...
    sl_pips    = Column(Integer, nullable=True)
    tp_pips    = Column(Integer, nullable=True)
    lot_size   = Column(String, nullable=True)
    details    = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)

    sender     = relationship("User", back_populates="sent_signals")


class SignalRead(Base):
    __tablename__ = "signal_reads"

    id          = Column(Integer, primary_key=True, index=True)
    receiver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    signal_id   = Column(Integer, ForeignKey("trade_signals.id"), nullable=False)
    read_at     = Column(DateTime, default=datetime.datetime.utcnow)

    receiver = relationship("User")
    signal   = relationship("TradeSignal")

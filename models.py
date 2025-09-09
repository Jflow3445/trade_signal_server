# models.py
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Text, ForeignKey,
    JSON, UniqueConstraint, Index
)
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"

    id          = Column(Integer, primary_key=True, index=True)
    username    = Column(String, nullable=False, unique=True)
    email       = Column(String, nullable=True, unique=False, index=True)
    api_key     = Column(String, nullable=False, unique=True, index=True)
    is_active   = Column(Boolean, nullable=False, default=True)
    expires_at  = Column(DateTime(timezone=True), nullable=True)

    # relationships (not strictly required for queries)
    signals     = relationship("TradeSignal", back_populates="user", lazy="selectin")


class TradeSignal(Base):
    __tablename__ = "trade_signals"

    id         = Column(Integer, primary_key=True, index=True)
    symbol     = Column(String, nullable=False)
    action     = Column(String, nullable=False)  # 'buy','sell','adjust_sl','adjust_tp','close','hold'
    sl_pips    = Column(Integer, nullable=True)
    tp_pips    = Column(Integer, nullable=True)
    lot_size   = Column(Integer, nullable=True)  # often double precision in PG; int okay if you send scaled
    details    = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    user       = relationship("User", back_populates="signals", lazy="joined")


Index("idx_trade_signals_user_created", TradeSignal.user_id, TradeSignal.created_at.desc())


class Subscription(Base):
    """
    Who may receive whose signals.
    (receiver_id, sender_id) is the natural PK; both reference users.id
    """
    __tablename__ = "subscriptions"

    receiver_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    sender_id   = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    created_at  = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    # optional relationships:
    # receiver = relationship("User", foreign_keys=[receiver_id])
    # sender   = relationship("User", foreign_keys=[sender_id])


Index("idx_subscriptions_receiver", Subscription.receiver_id)
Index("idx_subscriptions_sender",   Subscription.sender_id)


class SignalRead(Base):
    """
    Ledger of which receiver has been delivered which signal (consumes quota for buy/sell).
    """
    __tablename__ = "signal_reads"

    id        = Column(Integer, primary_key=True)
    user_id   = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    signal_id = Column(Integer, ForeignKey("trade_signals.id", ondelete="CASCADE"), nullable=False, index=True)
    read_at   = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "signal_id", name="uq_signal_reads_user_signal"),
    )

import datetime
from typing import List, Optional, Tuple, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from . import models, schemas


# ----------------------------
# Users
# ----------------------------

def get_user_by_api_key(db: Session, api_key: str) -> Optional[models.User]:
    return db.query(models.User).filter(models.User.api_key == api_key).first()

def get_user_by_email(db: Session, email: str) -> Optional[models.User]:
    return db.query(models.User).filter(models.User.email == email).first()

def create_user(db: Session, user: schemas.UserCreate) -> models.User:
    db_user = models.User(**user.dict())
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

def update_user_plan(
    db: Session,
    user: models.User,
    plan: str,
    daily_quota: Optional[int] = None,
    expires_at: Optional[datetime.datetime] = None,
    is_active: Optional[bool] = None,
) -> models.User:
    user.plan = plan
    if daily_quota is not None:
        user.daily_quota = daily_quota
    if expires_at is not None:
        user.expires_at = expires_at
    if is_active is not None:
        user.is_active = is_active
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ----------------------------
# Subscriptions
# ----------------------------

def create_subscription(db: Session, receiver_id: int, sender_id: int) -> models.Subscription:
    sub = models.Subscription(receiver_id=receiver_id, sender_id=sender_id)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub

def delete_subscription(db: Session, receiver_id: int, sender_id: int) -> bool:
    sub = db.query(models.Subscription).filter(
        models.Subscription.receiver_id == receiver_id,
        models.Subscription.sender_id == sender_id,
    ).first()
    if not sub:
        return False
    db.delete(sub)
    db.commit()
    return True

def list_receiver_senders(db: Session, receiver_id: int) -> List[models.User]:
    # All senders that receiver follows
    q = (
        db.query(models.User)
        .join(models.Subscription, models.Subscription.sender_id == models.User.id)
        .filter(models.Subscription.receiver_id == receiver_id)
    )
    return q.all()

def list_sender_receivers(db: Session, sender_id: int) -> List[models.User]:
    # All receivers that follow the sender
    q = (
        db.query(models.User)
        .join(models.Subscription, models.Subscription.receiver_id == models.User.id)
        .filter(models.Subscription.sender_id == sender_id)
    )
    return q.all()


# ----------------------------
# Signals
# ----------------------------

def create_signal(
    db: Session,
    sender_id: int,
    signal: schemas.SignalCreate,
) -> models.TradeSignal:
    data = signal.dict()
    db_signal = models.TradeSignal(user_id=sender_id, **data)
    db.add(db_signal)
    db.commit()
    db.refresh(db_signal)
    return db_signal


def list_signals_for_receiver(
    db: Session,
    receiver_id: int,
    limit: int = 50,
    offset: int = 0,
    since_id: Optional[int] = None,
    symbols: Optional[List[str]] = None,
    actions: Optional[List[str]] = None,
) -> List[models.TradeSignal]:
    """
    Fetch signals from all senders that this receiver is subscribed to.
    Newest first by default.
    """
    # join subscriptions -> trade_signals where trade_signals.user_id = sender_id
    q = (
        db.query(models.TradeSignal)
        .join(models.Subscription, models.Subscription.sender_id == models.TradeSignal.user_id)
        .filter(models.Subscription.receiver_id == receiver_id)
    )

    if since_id is not None:
        q = q.filter(models.TradeSignal.id > since_id)

    if symbols:
        q = q.filter(models.TradeSignal.symbol.in_(symbols))

    if actions:
        q = q.filter(models.TradeSignal.action.in_(actions))

    q = q.order_by(models.TradeSignal.id.desc())

    if offset:
        q = q.offset(offset)
    if limit:
        q = q.limit(limit)

    return q.all()


def list_signals_for_sender(
    db: Session,
    sender_id: int,
    limit: int = 50,
    offset: int = 0,
    since_id: Optional[int] = None,
    symbols: Optional[List[str]] = None,
    actions: Optional[List[str]] = None,
) -> List[models.TradeSignal]:
    """
    Fetch signals authored by a given sender (useful for debugging).
    """
    q = db.query(models.TradeSignal).filter(models.TradeSignal.user_id == sender_id)

    if since_id is not None:
        q = q.filter(models.TradeSignal.id > since_id)

    if symbols:
        q = q.filter(models.TradeSignal.symbol.in_(symbols))

    if actions:
        q = q.filter(models.TradeSignal.action.in_(actions))

    q = q.order_by(models.TradeSignal.id.desc())

    if offset:
        q = q.offset(offset)
    if limit:
        q = q.limit(limit)

    return q.all()


# ----------------------------
# Reads / Acknowledgements
# ----------------------------

def mark_signal_read(
    db: Session,
    receiver_id: int,
    signal_id: int,
) -> models.SignalRead:
    sr = models.SignalRead(receiver_id=receiver_id, signal_id=signal_id)
    db.add(sr)
    db.commit()
    db.refresh(sr)
    return sr

def list_reads_for_receiver(
    db: Session,
    receiver_id: int,
    limit: int = 100,
    offset: int = 0,
) -> List[models.SignalRead]:
    q = db.query(models.SignalRead).filter(models.SignalRead.receiver_id == receiver_id)
    q = q.order_by(models.SignalRead.read_at.desc())
    if offset:
        q = q.offset(offset)
    if limit:
        q = q.limit(limit)
    return q.all()


# ----------------------------
# Quota helpers
# ----------------------------

def count_actionable_signals_today(
    db: Session,
    receiver_id: int,
    senders: Optional[List[int]] = None,
) -> int:
    """
    How many actionable signals (buy/sell) has the receiver pulled today across all senders?
    Uses UTC date window.
    """
    today = datetime.datetime.utcnow().date()
    start = datetime.datetime.combine(today, datetime.time.min)
    end = datetime.datetime.combine(today, datetime.time.max)

    q = (
        db.query(func.count(models.TradeSignal.id))
        .join(models.Subscription, models.Subscription.sender_id == models.TradeSignal.user_id)
        .filter(models.Subscription.receiver_id == receiver_id)
        .filter(
            models.TradeSignal.action.in_(["buy", "sell"]),
            models.TradeSignal.created_at >= start,
            models.TradeSignal.created_at <= end,
        )
    )
    if senders:
        q = q.filter(models.TradeSignal.user_id.in_(senders))

    return q.scalar() or 0


def count_actionable_signals_today_per_sender(
    db: Session,
    receiver_id: int,
) -> Dict[int, int]:
    """
    Return a dict of sender_id -> count of actionable signals today visible to receiver.
    """
    today = datetime.datetime.utcnow().date()
    start = datetime.datetime.combine(today, datetime.time.min)
    end = datetime.datetime.combine(today, datetime.time.max)

    rows = (
        db.query(models.TradeSignal.user_id, func.count(models.TradeSignal.id))
        .join(models.Subscription, models.Subscription.sender_id == models.TradeSignal.user_id)
        .filter(
            models.Subscription.receiver_id == receiver_id,
            models.TradeSignal.action.in_(["buy", "sell"]),
            models.TradeSignal.created_at >= start,
            models.TradeSignal.created_at <= end,
        )
        .group_by(models.TradeSignal.user_id)
        .all()
    )
    return {user_id: cnt for (user_id, cnt) in rows}


# ----------------------------
# Admin / Maintenance
# ----------------------------

def purge_old_signals(db: Session, days: int = 90) -> int:
    """
    Delete signals older than `days`. Returns number deleted.
    """
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    q = db.query(models.TradeSignal).filter(models.TradeSignal.created_at < cutoff)
    n = q.count()
    q.delete(synchronize_session=False)
    db.commit()
    return n

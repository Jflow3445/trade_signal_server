from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple, Dict

from . import models, schemas

# ------------------------------
# USERS
# ------------------------------
def get_user_by_email_or_api(db: Session, email: Optional[str] = None, api_key: Optional[str] = None) -> Optional[models.User]:
    """
    Retrieve a user by email OR api_key. Either parameter may be provided.
    If both are provided, email is used first then api_key fallback.
    """
    query = db.query(models.User)
    if email:
        user = query.filter(func.lower(models.User.email) == func.lower(email)).first()
        if user:
            return user
    if api_key:
        return query.filter(models.User.api_key == api_key).first()
    return None


def get_user_by_api_key(db: Session, api_key: str) -> Optional[models.User]:
    return db.query(models.User).filter(models.User.api_key == api_key).first()


def get_user_by_email(db: Session, email: str) -> Optional[models.User]:
    return db.query(models.User).filter(func.lower(models.User.email) == func.lower(email)).first()


def list_users(db: Session, skip: int = 0, limit: int = 100) -> List[models.User]:
    return db.query(models.User).offset(skip).limit(limit).all()


def create_user(db: Session, user_in: schemas.UserCreate) -> models.User:
    user = models.User(
        username=user_in.username,
        email=user_in.email,
        is_active=user_in.is_active,
        api_key=user_in.api_key,
        plan=user_in.plan,
        daily_quota=user_in.daily_quota,
        expires_at=user_in.expires_at,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def update_user_plan_and_quota(
    db: Session,
    user: models.User,
    plan: Optional[str] = None,
    daily_quota: Optional[int] = None,
    expires_at: Optional[datetime] = None,
) -> models.User:
    """
    Update plan/daily_quota/expires_at for a user. Any None param is ignored.
    """
    if plan is not None:
        user.plan = plan
    if daily_quota is not None:
        user.daily_quota = daily_quota
    if expires_at is not None:
        user.expires_at = expires_at
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ------------------------------
# SIGNALS
# ------------------------------
def create_signal(db: Session, signal_in: schemas.SignalCreate, user_id: int) -> models.TradeSignal:
    signal = models.TradeSignal(
        user_id=user_id,
        symbol=signal_in.symbol,
        action=signal_in.action,
        sl_pips=signal_in.sl_pips,
        tp_pips=signal_in.tp_pips,
        lot_size=signal_in.lot_size,
        details=signal_in.details,
    )
    db.add(signal)
    db.commit()
    db.refresh(signal)
    return signal


def list_signals_for_sender(db: Session, sender_id: int, skip: int = 0, limit: int = 100) -> List[models.TradeSignal]:
    return (
        db.query(models.TradeSignal)
        .filter(models.TradeSignal.user_id == sender_id)
        .order_by(models.TradeSignal.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def list_signals_for_receiver(
    db: Session,
    receiver: models.User,
    since_id: Optional[int] = None,
    symbol: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 200,
) -> List[Dict]:
    """
    Fetch signals from the user's subscriptions (senders they follow).
    Apply optional filters (since_id, symbol, action).
    Return plain dicts (id, symbol, action, sl_pips, tp_pips, lot_size, details, created_at)
    """
    # Find sender_ids user is subscribed to
    subs = db.query(models.Subscription).filter(models.Subscription.receiver_id == receiver.id).all()
    sender_ids = [s.sender_id for s in subs]
    if not sender_ids:
        return []

    q = (
        db.query(models.TradeSignal)
        .filter(models.TradeSignal.user_id.in_(sender_ids))
    )

    if since_id:
        q = q.filter(models.TradeSignal.id > since_id)
    if symbol:
        q = q.filter(func.lower(models.TradeSignal.symbol) == func.lower(symbol))
    if action:
        q = q.filter(func.lower(models.TradeSignal.action) == func.lower(action))

    q = q.order_by(models.TradeSignal.id.desc()).limit(limit)
    rows = q.all()

    result = []
    for s in rows:
        result.append({
            "id": s.id,
            "symbol": s.symbol,
            "action": s.action,
            "sl_pips": s.sl_pips,
            "tp_pips": s.tp_pips,
            "lot_size": s.lot_size,
            "details": s.details,
            "created_at": s.created_at.replace(tzinfo=timezone.utc),
        })
    return list(reversed(result))  # ascending by id to be friendly to consumers


# ------------------------------
# SUBSCRIPTIONS
# ------------------------------
def add_subscription(db: Session, receiver_id: int, sender_id: int) -> models.Subscription:
    sub = models.Subscription(receiver_id=receiver_id, sender_id=sender_id)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def remove_subscription(db: Session, receiver_id: int, sender_id: int) -> bool:
    sub = (
        db.query(models.Subscription)
        .filter(
            models.Subscription.receiver_id == receiver_id,
            models.Subscription.sender_id == sender_id,
        )
        .first()
    )
    if not sub:
        return False
    db.delete(sub)
    db.commit()
    return True


def list_subscriptions(db: Session, receiver_id: int) -> List[models.Subscription]:
    return (
        db.query(models.Subscription)
        .filter(models.Subscription.receiver_id == receiver_id)
        .all()
    )


# ------------------------------
# QUOTA / USAGE
# ------------------------------
def count_open_actions_today_for_user(db: Session, user_id: int) -> int:
    """
    Count 'buy' and 'sell' signals created by this user today (UTC).
    """
    today_utc = datetime.now(timezone.utc).date()
    tomorrow_utc = today_utc + timedelta(days=1)
    return (
        db.query(models.TradeSignal)
        .filter(
            models.TradeSignal.user_id == user_id,
            models.TradeSignal.action.in_(["buy", "sell"]),
            models.TradeSignal.created_at >= datetime.combine(today_utc, datetime.min.time(), tzinfo=timezone.utc),
            models.TradeSignal.created_at < datetime.combine(tomorrow_utc, datetime.min.time(), tzinfo=timezone.utc),
        )
        .count()
    )


def plan_to_quota(plan: Optional[str]) -> Optional[int]:
    """
    Map plan -> daily quota for 'buy'/'sell' opens.
    None means unlimited.
    """
    if not plan:
        return 1
    p = plan.lower()
    if p == "free":
        return 1
    if p == "silver":
        return 3
    if p == "gold":
        return None
    # default
    return 1


def within_quota(db: Session, user: models.User) -> bool:
    """
    Check if user is within open-action quota for today.
    """
    if not user.is_active:
        return False
    if user.expires_at and user.expires_at < datetime.now(timezone.utc):
        return False

    # Determine effective daily quota
    eff_quota = user.daily_quota if user.daily_quota is not None else plan_to_quota(user.plan)
    if eff_quota is None:
        return True  # unlimited

    used = count_open_actions_today_for_user(db, user.id)
    return used < eff_quota


# ------------------------------
# SIGNAL READS (DELIVERY)
# ------------------------------
def record_signal_read(
    db: Session,
    receiver_id: int,
    signal_id: int,
    token_hash: Optional[str] = None,
) -> models.SignalRead:
    sr = models.SignalRead(
        receiver_id=receiver_id,
        signal_id=signal_id,
        token_hash=token_hash,
    )
    db.add(sr)
    db.commit()
    db.refresh(sr)
    return sr


def already_read_this_signal(
    db: Session,
    receiver_id: int,
    signal_id: int,
    token_hash: Optional[str] = None,
) -> bool:
    q = db.query(models.SignalRead).filter(
        models.SignalRead.receiver_id == receiver_id,
        models.SignalRead.signal_id == signal_id,
    )
    if token_hash:
        q = q.filter(models.SignalRead.token_hash == token_hash)
    return db.query(q.exists()).scalar()


# ------------------------------
# ADMIN / UTIL
# ------------------------------
def list_all_signals(db: Session, limit: int = 500) -> List[models.TradeSignal]:
    return (
        db.query(models.TradeSignal)
        .order_by(models.TradeSignal.id.desc())
        .limit(limit)
        .all()
    )

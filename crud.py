# crud.py
from __future__ import annotations
from typing import Optional, List, Tuple, Iterable
from datetime import datetime, timezone, date
from sqlalchemy.orm import Session
from sqlalchemy import select, func, and_, or_, literal, text
from models import User, TradeSignal, Subscription, SignalRead


# ---------- helpers ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_utc() -> date:
    return now_utc().date()


# ---------- users ----------
def get_user_by_api_key(db: Session, api_key: str) -> Optional[User]:
    return db.execute(
        select(User).where(User.api_key == api_key)
    ).scalar_one_or_none()


def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.execute(
        select(User).where(func.lower(User.email) == func.lower(literal(email)))
    ).scalar_one_or_none()


def validate_user(db: Session, email: str, api_key: str) -> Tuple[bool, Optional[User]]:
    """
    Validate by email + api_key, but **return ok=False instead of raising**.
    EA expects 200 with "ok":true/false.
    """
    user = get_user_by_api_key(db, api_key)
    if not user:
        return False, None
    if email and user.email and email.lower() != user.email.lower():
        # api_key belongs to a different email
        return False, None
    if not user.is_active:
        return False, user
    if user.expires_at and user.expires_at <= now_utc():
        # expired
        return False, user
    return True, user


# ---------- plan & quota (per API KEY) ----------
def plan_for_user(user: User) -> Tuple[str, Optional[int]]:
    """
    Map user to (plan_name, daily_quota).
    If you already store plan elsewhere, adjust this mapping.
    """
    # Heuristic: derive from user's recent /validate examples
    # If you store plan in another table, look it up here.
    name = "free"
    daily = 1

    # Examples seen: free=1, silver=3, gold=unlimited
    # We can infer by looking at expires_at presence or username/email patterns,
    # but it's better to store plan in your DB. Here we keep a simple override:
    # (Adjust this logic if you already persist a "plan")
    uname = (user.username or "").lower()
    mail = (user.email or "").lower()

    if uname in {"jflow3445", "adzikakafui123", "felix"} or "admin@" in mail:
        name, daily = "gold", None
    elif uname in {"alice", "tarkan"}:
        name, daily = "silver", 3
    else:
        name, daily = "free", 1

    return name, daily


def remaining_quota_for_api_key(db: Session, api_key: str) -> Tuple[Optional[int], str]:
    """
    Return (remaining, plan_name) for today UTC.
    None remaining means unlimited.
    """
    user = get_user_by_api_key(db, api_key)
    if not user:
        return 0, "unknown"

    plan_name, daily_quota = plan_for_user(user)

    if not daily_quota:  # None or 0 -> unlimited
        return None, plan_name

    # count buy/sell reads today
    start = datetime.combine(today_utc(), datetime.min.time(), tzinfo=timezone.utc)
    end   = datetime.combine(today_utc(), datetime.max.time(), tzinfo=timezone.utc)

    q = (
        select(func.count(SignalRead.id))
        .join(TradeSignal, TradeSignal.id == SignalRead.signal_id)
        .where(
            SignalRead.user_id == user.id,
            SignalRead.read_at >= start,
            SignalRead.read_at <= end,
            TradeSignal.action.in_(("buy", "sell")),
        )
    )
    used = db.execute(q).scalar_one()
    remaining = max(0, daily_quota - int(used or 0))
    return remaining, plan_name


# ---------- subscriptions ----------
def list_subscriptions(db: Session, receiver_id: int) -> List[int]:
    """
    Return list of sender user_ids whom 'receiver_id' is allowed to receive.
    """
    rows = db.execute(
        select(Subscription.sender_id).where(Subscription.receiver_id == receiver_id)
    ).all()
    return [sid for (sid,) in rows]


# ---------- signals (create & list) ----------
def create_trade_signal(
    db: Session,
    user_id: int,
    symbol: str,
    action: str,
    sl_pips: Optional[int],
    tp_pips: Optional[int],
    lot_size: Optional[float],
    details,
) -> TradeSignal:
    sig = TradeSignal(
        user_id=user_id,
        symbol=symbol,
        action=action,
        sl_pips=sl_pips or 0,
        tp_pips=tp_pips or 0,
        lot_size=lot_size or 0.0,
        details=details,
        created_at=now_utc(),
    )
    db.add(sig)
    db.commit()
    db.refresh(sig)
    return sig


def _unread_signals_for_receiver(db: Session, receiver: User, sender_ids: List[int], limit: int = 200) -> List[TradeSignal]:
    """
    Fetch latest signals from allowed senders that this receiver hasn't been delivered yet.
    We return a bit more than we might deliver (we'll slice by quota later).
    """
    if not sender_ids:
        return []

    q = (
        select(TradeSignal)
        .where(TradeSignal.user_id.in_(sender_ids))
        .where(~TradeSignal.id.in_(
            select(SignalRead.signal_id).where(SignalRead.user_id == receiver.id)
        ))
        .order_by(TradeSignal.id.asc())
        .limit(limit)
    )
    return [row[0] for row in db.execute(q).all()]


def record_signal_reads(db: Session, user_id: int, signal_ids: Iterable[int]) -> None:
    """Insert rows into signal_reads with ON CONFLICT DO NOTHING semantics."""
    if not signal_ids:
        return
    values = [{"user_id": user_id, "signal_id": sid, "read_at": now_utc()} for sid in signal_ids]
    # Bulk insert with conflict ignore (SQLAlchemy Core upsert)
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = pg_insert(SignalRead).values(values)
    stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "signal_id"])
    db.execute(stmt)
    db.commit()


def list_signals_for_receiver(
    db: Session,
    receiver: User,
    api_key: str,
    max_fetch: int = 200
) -> List[TradeSignal]:
    """
    Return signals the receiver is allowed to see, respecting:
      - subscriptions (who they follow)
      - daily quota for **buy/sell** based on API key
      - deliver once (avoid duplicates) via signal_reads
    """
    sender_ids = list_subscriptions(db, receiver.id)
    if not sender_ids:
        return []

    # Pull a buffer (unread)
    unread = _unread_signals_for_receiver(db, receiver, sender_ids, limit=max_fetch)

    # Quota remaining (None => unlimited)
    remaining, _plan = remaining_quota_for_api_key(db, api_key)

    deliver: List[TradeSignal] = []
    buysells_taken = 0

    for sig in unread:
        if sig.action in ("buy", "sell"):
            if remaining is None:
                deliver.append(sig)
            else:
                if buysells_taken < remaining:
                    deliver.append(sig)
                    buysells_taken += 1
                else:
                    # skip buy/sell when quota exhausted
                    continue
        else:
            # non-actionables always pass through
            deliver.append(sig)

    # Mark delivered so we won't send again next poll
    record_signal_reads(db, receiver.id, (s.id for s in deliver))
    return deliver


# ---------- diagnostics (optional) ----------
def count_opens_today(db: Session, user_id: int) -> int:
    start = datetime.combine(today_utc(), datetime.min.time(), tzinfo=timezone.utc)
    end   = datetime.combine(today_utc(), datetime.max.time(), tzinfo=timezone.utc)
    q = (
        select(func.count(SignalRead.id))
        .join(TradeSignal, TradeSignal.id == SignalRead.signal_id)
        .where(
            SignalRead.user_id == user_id,
            SignalRead.read_at >= start,
            SignalRead.read_at <= end,
            TradeSignal.action.in_(("buy", "sell")),
        )
    )
    return int(db.execute(q).scalar_one() or 0)

from __future__ import annotations
import hashlib
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

from sqlalchemy import select, func, and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import User, Subscription, TradeSignal, SignalRead


# ------------------------ helpers ------------------------

ACTIONABLE = {"buy", "sell"}
NON_ACTIONABLE = {"adjust_sl", "adjust_tp", "close", "hold"}

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def utc_day_bounds(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """Return (start_of_day_utc, next_day_utc) for filtering daily quota."""
    now = now or datetime.now(timezone.utc)
    start = datetime(year=now.year, month=now.month, day=now.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


# ------------------------ auth / users ------------------------

def get_user_by_token(db: Session, token: str) -> Optional[User]:
    return db.execute(select(User).where(User.api_key == token, User.is_active == True)).scalar_one_or_none()

def get_user_by_email_and_token(db: Session, email: str, token: str) -> Optional[User]:
    return db.execute(
        select(User).where(User.email == email, User.api_key == token, User.is_active == True)
    ).scalar_one_or_none()


def get_sender_ids_for_receiver(db: Session, receiver_id: int) -> List[int]:
    rows = db.execute(select(Subscription.sender_id).where(Subscription.receiver_id == receiver_id)).all()
    return [r[0] for r in rows]


# ------------------------ signals: write ------------------------

def create_signal(db: Session, sender: User, payload: dict) -> TradeSignal:
    ts = TradeSignal(
        user_id=sender.id,
        symbol=payload["symbol"],
        action=payload["action"].lower(),
        sl_pips=payload.get("sl_pips"),
        tp_pips=payload.get("tp_pips"),
        lot_size=payload.get("lot_size"),
        details=payload.get("details"),
    )
    db.add(ts)
    db.flush()  # so ts.id is available
    return ts


# ------------------------ quota accounting ------------------------

def count_actionable_used_today(db: Session, token_hash: str) -> int:
    """
    Count how many actionable (BUY/SELL) signals this token has already consumed today.
    Uses token_hash + join to TradeSignal to filter by action and date.
    """
    start, end = utc_day_bounds()
    q = (
        select(func.count())
        .select_from(SignalRead)
        .join(TradeSignal, TradeSignal.id == SignalRead.signal_id)
        .where(
            SignalRead.token_hash == token_hash,
            SignalRead.read_at >= start,
            SignalRead.read_at < end,
            TradeSignal.action.in_(ACTIONABLE),
        )
    )
    return int(db.execute(q).scalar() or 0)


def mark_read_if_new(db: Session, signal_id: int, receiver_id: Optional[int], token_hash: str) -> bool:
    """
    Record a read for (token_hash, signal_id) if it doesn't exist.
    Returns True if a new row was inserted (i.e., first time this token sees the signal).
    """
    sr = SignalRead(signal_id=signal_id, receiver_id=receiver_id, token_hash=token_hash)
    db.add(sr)
    try:
        db.flush()
        return True
    except IntegrityError:
        db.rollback()
        # already recorded for this token+signal
        return False


def effective_quota(user: User) -> Optional[int]:
    """
    Return an integer daily quota or None for unlimited.
    (We trust user.daily_quota at request time so upgrades/downgrades take effect immediately.)
    """
    return user.daily_quota  # None => unlimited


def fetch_signals_for_receiver_with_quota(
    db: Session,
    receiver: User,
    bearer_token: str,
    limit: int = 200,
) -> Tuple[List[TradeSignal], int, Optional[int]]:
    """
    Returns (signals_list, used_today, remaining) where remaining can be None for unlimited.
    Enforcement:
      - NON_ACTIONABLE signals are always included (not counted).
      - ACTIONABLE signals are included only if (a) they've already been seen by this token (idempotent),
        or (b) quota remaining > 0 (then we 'consume' one and include it).
      - This function never leaks *new* actionable signals after quota is exhausted.
    """
    token_hash = sha256_hex(bearer_token)
    q = effective_quota(receiver)  # int or None
    used = count_actionable_used_today(db, token_hash)
    remaining = None if q is None else max(0, q - used)

    # All senders this receiver subscribes to
    sender_ids = get_sender_ids_for_receiver(db, receiver.id)
    if not sender_ids:
        return [], used, remaining

    # Pull recent signals from subscribed senders
    rows = db.execute(
        select(TradeSignal)
        .where(TradeSignal.user_id.in_(sender_ids))
        .order_by(TradeSignal.id.asc())
        .limit(max(1000, limit))   # fetch a generous window; we'll filter in python
    ).scalars().all()

    out: List[TradeSignal] = []
    # We include signals (ascending id). For actionable:
    #   - if already seen by this token -> include (no extra cost)
    #   - else if remaining > 0 -> mark_read & include; remaining -= 1
    #   - else -> skip
    for sig in rows:
        act = (sig.action or "").lower()
        if act in NON_ACTIONABLE:
            out.append(sig)
            # we don't have to mark reads for non-actionable; omit to keep the read table lean
            continue

        if act in ACTIONABLE:
            # check if already seen for this token
            inserted = mark_read_if_new(db, signal_id=sig.id, receiver_id=receiver.id, token_hash=token_hash)
            if not inserted:
                # already known to this token => include without consuming new quota
                out.append(sig)
            else:
                if remaining is None:
                    # unlimited
                    out.append(sig)
                elif remaining > 0:
                    out.append(sig)
                    remaining -= 1
                else:
                    # quota exhausted; undo the inserted read to keep counts exact
                    # (edge case: two parallel requests). A simpler approach is to keep it,
                    # but we'll delete it to avoid incrementing used.
                    db.query(SignalRead).filter(
                        SignalRead.token_hash == token_hash,
                        SignalRead.signal_id == sig.id
                    ).delete(synchronize_session=False)
                    db.flush()
                    # skip the signal
                    continue
        else:
            # Unknown action: be strict and skip
            continue

    return out[-limit:], used, remaining

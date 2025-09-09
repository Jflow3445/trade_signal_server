# crud.py
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert

from models import (
    User, TradeSignal, LatestSignal, TradeRecord, Activation,
    OpenPosition, ReferralBoost, DailyConsumption
)
import models
from schemas import TradeSignalCreate, TradeRecordCreate, EAOpenPosition

UTC = timezone.utc
def now() -> datetime:
    return datetime.now(tz=UTC)

# -------- Plans & defaults --------
PLAN_DEFAULTS = {
    "free":   {"daily_quota": 1},
    "silver": {"daily_quota": 3},
    "gold":   {"daily_quota": None},   # unlimited
}
def plan_defaults(plan: str) -> dict:
    return PLAN_DEFAULTS.get((plan or "free").lower(), PLAN_DEFAULTS["free"]).copy()

# -------- Users / tokens --------
def issue_or_update_user(
    db: Session,
    *,
    email: Optional[str],
    username: str,
    plan: str,
    daily_quota_override: Optional[int] = None,
    months_valid: Optional[int] = None,
    rotate_token: bool = True,  # rotate on upgrade/downgrade so new quota applies immediately
) -> User:
    username = (username or "").strip().lower()
    if username in {"farm_robot"}:
        raise ValueError("reserved_username")

    user = db.query(User).filter(User.username == username).one_or_none()

    # decide quota to store on the user row
    quota = daily_quota_override if daily_quota_override is not None else plan_defaults(plan)["daily_quota"]
    token = secrets.token_hex(32) if (rotate_token or not user) else (user.api_key if user else secrets.token_hex(32))

    if user:
        user.email = email or user.email
        user.plan = plan
        user.daily_quota = quota
        if rotate_token:
            user.api_key = token
        if months_valid:
            user.expires_at = now() + timedelta(days=30 * months_valid)
        user.is_active = True
    else:
        user = User(
            email=email,
            username=username,
            api_key=token,
            plan=plan,
            daily_quota=quota,
            expires_at=(now() + timedelta(days=30 * months_valid)) if months_valid else None,
            is_active=True,
        )
        db.add(user)

    db.commit()
    db.refresh(user)
    return user

def user_by_token(db: Session, token: str) -> Optional[User]:
    return db.query(User).filter(User.api_key == token, User.is_active == True).one_or_none()

def user_by_email(db: Session, email: str) -> Optional[User]:
    return db.query(User).filter(User.email == email).one_or_none()

# -------- Referral boost (optional) --------
def get_active_referral_boost(db: Session, user_id: int) -> Optional[ReferralBoost]:
    now_ts = now()
    boosts = (
        db.query(ReferralBoost)
        .filter(
            ReferralBoost.user_id == user_id,
            ReferralBoost.is_revoked == False,
            ReferralBoost.start_at <= now_ts,
            ReferralBoost.end_at > now_ts,
        )
        .all()
    )
    if not boosts:
        return None
    def score(b: ReferralBoost):
        lvl = 2 if b.boost_to == "gold" else 1
        return (lvl, b.end_at)
    boosts.sort(key=score, reverse=True)
    return boosts[0]

# -------- Signals --------
def create_signal(db: Session, user_id: int, s: TradeSignalCreate) -> TradeSignal:
    # 1) append to history
    sig = models.TradeSignal(
        user_id=user_id,
        symbol=s.symbol,
        action=s.action,
        sl_pips=s.sl_pips,
        tp_pips=s.tp_pips,
        lot_size=s.lot_size,
        details=s.details,
    )
    db.add(sig)
    db.flush()

    # 2) upsert latest by (user_id, symbol)
    stmt = insert(models.LatestSignal).values(
        user_id=user_id,
        symbol=s.symbol,
        action=s.action,
        sl_pips=s.sl_pips,
        tp_pips=s.tp_pips,
        lot_size=s.lot_size,
        details=s.details,
    ).on_conflict_do_update(
        index_elements=["user_id", "symbol"],
        set_={
            "action": s.action,
            "sl_pips": s.sl_pips,
            "tp_pips": s.tp_pips,
            "lot_size": s.lot_size,
            "details": s.details,
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)
    db.commit()
    db.refresh(sig)
    return sig

def list_signals(db: Session, user_id: int, limit: int = 100, max_age_minutes: int = 1):
    cutoff = now() - timedelta(minutes=max_age_minutes)
    return (
        db.query(TradeSignal)
        .filter(TradeSignal.user_id == user_id, TradeSignal.created_at >= cutoff)
        .order_by(TradeSignal.created_at.desc())
        .limit(limit)
        .all()
    )

def list_latest_signals(db: Session, user_id: int, limit: int = 50, max_age_minutes: int = 1):
    cutoff = now() - timedelta(minutes=max_age_minutes)
    return (
        db.query(LatestSignal)
        .filter(LatestSignal.user_id == user_id, LatestSignal.updated_at >= cutoff)
        .order_by(LatestSignal.updated_at.desc())
        .limit(limit)
        .all()
    )

# -------- Trades --------
def create_trade_record(db: Session, trade: TradeRecordCreate, user_id: int) -> TradeRecord:
    tr = TradeRecord(
        user_id=user_id,
        symbol=trade.symbol,
        side=trade.side,
        entry_price=trade.entry_price,
        exit_price=trade.exit_price,
        volume=trade.volume,
        pnl=trade.pnl,
        duration=trade.duration,
        open_time=trade.open_time,
        close_time=trade.close_time,
        details=trade.details,
    )
    db.add(tr)
    db.commit()
    db.refresh(tr)
    return tr

# -------- Quota (token-bound) --------
def get_daily_consumption(db: Session, api_key: str) -> int:
    today = now().date()
    row = (
        db.query(DailyConsumption)
        .filter(DailyConsumption.api_key == api_key, DailyConsumption.date == today)
        .one_or_none()
    )
    return row.signals_consumed if row else 0

def record_signal_consumption(db: Session, api_key: str, count: int) -> None:
    if count <= 0:
        return
    today = now().date()
    stmt = insert(DailyConsumption).values(
        api_key=api_key, date=today, signals_consumed=count
    ).on_conflict_do_update(
        index_elements=["api_key", "date"],
        set_={
            "signals_consumed": DailyConsumption.signals_consumed + count,
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)
    db.commit()

def check_and_consume_quota(db: Session, api_key: str, requested: int, daily_quota: int) -> int:
    """
    Returns how many signals may be served right now without exceeding the token’s daily_quota.
    No DB write here; we write only for what we actually return.
    """
    consumed = get_daily_consumption(db, api_key)
    remaining = max(daily_quota - consumed, 0)
    if remaining <= 0:
        return 0
    return min(requested, remaining)

def consume_quota_for_signals(db: Session, api_key: str, actually_returned: int) -> None:
    record_signal_consumption(db, api_key, actually_returned)

# -------- EA state (optional) --------
def touch_activation(db: Session, user_id: int, account_id: str, broker_server: str, hwid: Optional[str]) -> Activation:
    act = (
        db.query(Activation)
        .filter(
            Activation.user_id == user_id,
            Activation.account_id == account_id,
            Activation.broker_server == broker_server,
        )
        .one_or_none()
    )
    if act:
        act.last_seen_at = now()
        if hwid:
            act.hwid = hwid
    else:
        act = Activation(user_id=user_id, account_id=account_id, broker_server=broker_server, hwid=hwid)
        db.add(act)
    db.commit()
    db.refresh(act)
    return act

def upsert_open_positions(db: Session, user_id: int, account_id: str, broker_server: str, positions: Iterable[EAOpenPosition]) -> int:
    db.query(OpenPosition).filter(
        OpenPosition.user_id == user_id,
        OpenPosition.account_id == account_id,
        OpenPosition.broker_server == broker_server,
    ).delete(synchronize_session=False)

    count = 0
    for p in positions:
        row = OpenPosition(
            user_id=user_id, account_id=account_id, broker_server=broker_server,
            ticket=p.ticket, symbol=p.symbol, side=p.side, volume=p.volume,
            entry_price=p.entry_price, sl=p.sl, tp=p.tp, open_time=p.open_time,
            magic=p.magic, comment=p.comment,
        )
        db.add(row)
        count += 1
    db.commit()
    return count

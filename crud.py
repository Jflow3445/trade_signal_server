from __future__ import annotations
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Iterable, List, Dict, Any
from sqlalchemy import func
from sqlalchemy.orm import Session
from models import (
    User, TradeSignal, LatestSignal, TradeRecord, Activation,
    OpenPosition, ReferralBoost
)
import models
from schemas import TradeSignalCreate, TradeRecordCreate, EAOpenPosition

UTC = timezone.utc

# ---------- Plans & quotas ----------
PLAN_DEFAULTS = {
    "free":   {"daily_quota": 1},
    "silver": {"daily_quota": 3},
    "gold":   {"daily_quota": None},  # unlimited
}

def plan_defaults(plan: str) -> dict:
    return PLAN_DEFAULTS.get((plan or "free").lower(), PLAN_DEFAULTS["free"]).copy()

def now() -> datetime:
    return datetime.now(tz=UTC)

def to_datetime(dt) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    raise TypeError("Expected datetime or None")

# ---------- Users / tokens ----------
def issue_or_update_user(
    db: Session,
    *, email: Optional[str], username: str, plan: str,
    daily_quota_override: Optional[int] = None,
    months_valid: Optional[int] = None,
) -> User:
    username = username.strip().lower()
    if username in {"farm_robot"}:
        # Disallow reserved names
        raise ValueError("reserved_username")

    token = secrets.token_hex(32)
    user = db.query(User).filter(User.username == username).one_or_none()
    if user:
        user.email = email or user.email
        user.plan = plan
        user.api_key = token
        user.daily_quota = daily_quota_override
        if months_valid:
            user.expires_at = now() + timedelta(days=30*months_valid)
        user.is_active = True
    else:
        user = User(
            email=email,
            username=username,
            api_key=token,
            plan=plan,
            daily_quota=daily_quota_override,
            expires_at=(now() + timedelta(days=30*months_valid)) if months_valid else None,
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

# ---------- Referral boost ----------
def get_active_referral_boost(db: Session, user_id: int) -> Optional[ReferralBoost]:
    """
    Returns an active referral boost record if current time is within [start_at, end_at)
    and not revoked. If multiple overlaps exist, return the one with the highest boost (gold > silver),
    breaking ties by latest end_at.
    """
    now_ts = now()
    boosts = (db.query(ReferralBoost)
                .filter(
                    ReferralBoost.user_id == user_id,
                    ReferralBoost.is_revoked == False,
                    ReferralBoost.start_at <= now_ts,
                    ReferralBoost.end_at > now_ts,
                )
                .all())
    if not boosts:
        return None
    def score(b: ReferralBoost):
        level = 2 if b.boost_to == "gold" else 1
        return (level, b.end_at)
    boosts.sort(key=score, reverse=True)
    return boosts[0]

# ---------- Signals ----------
def create_signal(db: Session, user_id: int, s: TradeSignalCreate) -> TradeSignal:
    sig = TradeSignal(
        user_id=user_id,
        symbol=s.symbol,
        action=s.action,
        sl_pips=s.sl_pips,
        tp_pips=s.tp_pips,
        lot_size=s.lot_size,
        details=s.details,
    )
    db.add(sig)
    # upsert latest
    latest = (db.query(LatestSignal)
                .filter(LatestSignal.user_id == user_id, LatestSignal.symbol == s.symbol)
                .one_or_none())
    if latest:
        latest.action = s.action
        latest.sl_pips = s.sl_pips
        latest.tp_pips = s.tp_pips
        latest.lot_size = s.lot_size
        latest.details = s.details
    else:
        latest = LatestSignal(
            user_id=user_id, symbol=s.symbol, action=s.action,
            sl_pips=s.sl_pips, tp_pips=s.tp_pips, lot_size=s.lot_size, details=s.details
        )
        db.add(latest)
    db.commit()
    db.refresh(sig)
    return sig

def list_signals(db: Session, user_id: int, limit: int = 100):
    return (db.query(TradeSignal)
              .filter(TradeSignal.user_id == user_id)
              .order_by(TradeSignal.created_at.desc())
              .limit(limit).all())

def list_latest_signals(db: Session, user_id: int, limit: int = 50):
    return (db.query(LatestSignal)
              .filter(LatestSignal.user_id == user_id)
              .order_by(LatestSignal.updated_at.desc())
              .limit(limit).all())

# ---------- Trades ----------
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
        open_time=to_datetime(trade.open_time),
        close_time=to_datetime(trade.close_time),
        details=trade.details,
    )
    db.add(tr)
    db.commit()
    db.refresh(tr)
    return tr

def count_signals_created_today(db: Session, user_id: int) -> int:
    start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(func.count(models.TradeSignal.id))
          .filter(models.TradeSignal.user_id == user_id,
                  models.TradeSignal.created_at >= start)
          .scalar()
          or 0
    )

# ---------- EA activations / positions ----------
def touch_activation(db: Session, user_id: int, account_id: str, broker_server: str, hwid: Optional[str]) -> Activation:
    act = (db.query(Activation)
             .filter(Activation.user_id == user_id,
                     Activation.account_id == account_id,
                     Activation.broker_server == broker_server)
             .one_or_none())
    if act:
        act.last_seen_at = now()
        if hwid:
            act.hwid = hwid
    else:
        act = Activation(
            user_id=user_id, account_id=account_id, broker_server=broker_server, hwid=hwid
        )
        db.add(act)
    db.commit()
    db.refresh(act)
    return act

def upsert_open_positions(
    db: Session, user_id: int, account_id: str, broker_server: str, positions: Iterable[EAOpenPosition]
) -> int:
    # naive upsert: delete old for user/account/server then insert provided set
    db.query(OpenPosition).filter(
        OpenPosition.user_id == user_id,
        OpenPosition.account_id == account_id,
        OpenPosition.broker_server == broker_server,
    ).delete(synchronize_session=False)

    count = 0
    for p in positions:
        row = OpenPosition(
            user_id=user_id, account_id=account_id, broker_server=broker_server, ticket=p.ticket,
            symbol=p.symbol, side=p.side, volume=p.volume, entry_price=p.entry_price, sl=p.sl, tp=p.tp,
            open_time=to_datetime(p.open_time), magic=p.magic, comment=p.comment
        )
        db.add(row)
        count += 1
    db.commit()
    return count

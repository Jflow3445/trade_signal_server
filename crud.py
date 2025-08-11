from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from models import TradeSignal, User, LatestSignal, TradeRecord
from schemas import TradeSignalCreate, TradeRecordCreate
import secrets
from typing import Optional, Tuple

UTC = timezone.utc

# ---------- Users ----------

def get_user_by_api_key(db: Session, api_key: str) -> Optional[User]:
    return db.query(User).filter(User.api_key == api_key).first()

def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.query(User).filter(User.email == email).first()

def ensure_daily_reset(user: User) -> None:
    now = datetime.now(UTC)
    # daily reset keyed by date (UTC)
    if not user.usage_reset_at or user.usage_reset_at.date() != now.date():
        user.used_today = 0
        user.usage_reset_at = now

def plan_defaults(plan: str) -> dict:
    """
    Return default limits for a plan.
    - free: 1/day, no expiry
    - silver: 3/day, 1 month expiry
    - gold: unlimited/day, 1 month expiry
    """
    p = (plan or "free").lower()
    if p == "gold":
        return {"daily_quota": None, "months_valid": 1}
    if p == "silver":
        return {"daily_quota": 3, "months_valid": 1}
    # free
    return {"daily_quota": 1, "months_valid": None}

def ensure_user(
    db: Session,
    email: str,
    username: Optional[str],
    plan: str,
    daily_quota_override: Optional[int]
) -> User:
    u = get_user_by_email(db, email)
    defaults = plan_defaults(plan)
    dq = daily_quota_override if daily_quota_override is not None else defaults["daily_quota"]

    if not u:
        u = User(
            email=email,
            username=username or email.split("@")[0],
            plan=plan,
            tier=plan,
            daily_quota=dq,
            quota=dq if dq is not None else None,
            is_active=True,
            used_today=0,
            usage_reset_at=datetime.now(UTC),
            expires_at=None if defaults["months_valid"] is None
                        else (datetime.now(UTC) + timedelta(days=30 * defaults["months_valid"]))
        )
        # also create an api_key so /me works if needed
        u.api_key = secrets.token_hex(16)
        db.add(u)
        db.commit()
        db.refresh(u)
    else:
        u.plan = plan
        u.tier = plan
        u.daily_quota = dq
        u.quota = dq if dq is not None else None
        db.commit()
    return u

def issue_or_rotate_token(
    db: Session,
    *,
    email: str,
    username: str,
    plan: str,
    daily_quota_override: Optional[int] = None,
    months_valid: Optional[int] = 1
) -> User:
    user = get_user_by_email(db, email)
    defaults = plan_defaults(plan)
    dq = daily_quota_override if daily_quota_override is not None else defaults["daily_quota"]
    mv = defaults["months_valid"] if months_valid is None else months_valid

    expires = None if (plan == "free" or mv is None) else datetime.now(UTC) + timedelta(days=30 * mv)

    if not user:
        # create new
        user = User(
            email=email,
            username=username,
            api_key=secrets.token_hex(16),
            plan=plan,
            daily_quota=dq,
            used_today=0,
            usage_reset_at=datetime.now(UTC),
            expires_at=expires,
            is_active=True,
            tier=plan,
            quota=dq if dq is not None else None
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    # rotate token & update limits/expiry
    user.api_key = secrets.token_hex(16)
    user.plan = plan
    user.tier = plan
    user.daily_quota = dq
    user.quota = dq if dq is not None else None
    user.expires_at = expires
    user.is_active = True
    user.used_today = 0
    user.usage_reset_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    return user

def remaining_today(user: User) -> Optional[int]:
    """Return remaining actionable count today. None => unlimited."""
    ensure_daily_reset(user)
    if user.daily_quota is None:
        return None
    return max(0, int(user.daily_quota) - int(user.used_today or 0))

def consume(db: Session, user: User, count: int) -> None:
    """Consume count actionable uses (no-op if unlimited)."""
    ensure_daily_reset(user)
    if user.daily_quota is None:
        return
    user.used_today = int(user.used_today or 0) + int(count)
    db.commit()
    db.refresh(user)

def validate_and_consume(
    db: Session,
    *,
    email: str,
    api_key: str,
    count: int = 1
) -> Tuple[bool, Optional[User], Optional[str]]:
    user = get_user_by_email(db, email)
    if not user or user.api_key != api_key:
        return False, None, "invalid_credentials"

    # farm_robot never limited
    if user.username == "farm_robot":
        return True, user, None

    if not user.is_active:
        return False, None, "inactive"

    now = datetime.now(UTC)
    if user.expires_at and user.expires_at <= now:
        return False, None, "expired"

    ensure_daily_reset(user)

    # unlimited
    if user.daily_quota is None:
        return True, user, None

    used = int(user.used_today or 0)
    quota = int(user.daily_quota)
    if used + count > quota:
        return False, user, "quota_exhausted"

    user.used_today = used + count
    db.commit()
    db.refresh(user)
    return True, user, None

# ---------- Signals ----------

def create_signal(db: Session, signal: TradeSignalCreate):
    db_signal = TradeSignal(**signal.dict())
    db.add(db_signal)
    db.commit()
    db.refresh(db_signal)
    return db_signal

def upsert_latest_signal(db: Session, signal: TradeSignalCreate):
    from datetime import datetime as dt
    db_signal = db.query(LatestSignal).filter(LatestSignal.symbol == signal.symbol).first()
    if db_signal:
        for field, value in signal.dict().items():
            setattr(db_signal, field, value)
        db_signal.updated_at = dt.utcnow()
    else:
        db_signal = LatestSignal(**signal.dict())
        db.add(db_signal)
    db.commit()
    db.refresh(db_signal)
    return db_signal

def get_latest_signal(db: Session, symbol: str):
    return db.query(LatestSignal).filter(LatestSignal.symbol == symbol).first()

# ---------- Trades ----------

def create_trade_record(db: Session, trade: TradeRecordCreate) -> TradeRecord:
    from datetime import datetime as dt
    def to_datetime(val):
        if isinstance(val, (float, int)):
            return dt.utcfromtimestamp(val)
        if isinstance(val, str):
            try:
                return dt.fromisoformat(val)
            except Exception:
                return None
        return val

    db_trade = TradeRecord(
        symbol=trade.symbol,
        side=trade.side,
        entry_price=trade.entry_price,
        exit_price=trade.exit_price,
        volume=trade.volume,
        pnl=trade.pnl,
        duration=str(trade.duration) if trade.duration is not None else None,
        open_time=to_datetime(trade.open_time) if trade.open_time else None,
        close_time=to_datetime(trade.close_time) if trade.close_time else None,
        details=trade.details,
        user_id=trade.user_id
    )
    db.add(db_trade)
    db.commit()
    db.refresh(db_trade)
    return db_trade

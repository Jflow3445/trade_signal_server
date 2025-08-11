from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from sqlalchemy.orm import Session
from models import TradeSignal, User, LatestSignal, TradeRecord
from schemas import TradeSignalCreate, TradeRecordCreate
import secrets

UTC = timezone.utc

# ---------- Users ----------
def get_user_by_api_key(db: Session, api_key: str) -> Optional[User]:
    return db.query(User).filter(User.api_key == api_key).first()

def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.query(User).filter(User.email == email).first()

def ensure_daily_reset(user: User) -> None:
    """
    Reset counters when the day changes (UTC).
    """
    now = datetime.now(UTC)
    if not user.usage_reset_at or user.usage_reset_at.date() != now.date():
        user.used_today = 0
        user.usage_reset_at = now

def plan_defaults(plan: str) -> dict:
    """
    Returns defaults for the plan:
      - daily_quota: None means unlimited
      - months_valid: None means no expiry
    """
    p = (plan or "free").lower()
    if p == "gold":
        return {"daily_quota": None, "months_valid": 1}
    if p == "silver":
        return {"daily_quota": 3, "months_valid": 1}
    # free
    return {"daily_quota": 1, "months_valid": None}

def ensure_user(db: Session, email: str, username: Optional[str], plan: str, daily_quota_override: Optional[int]) -> User:
    u = get_user_by_email(db, email)
    if not u:
        defs = plan_defaults(plan)
        dq = daily_quota_override if daily_quota_override is not None else defs["daily_quota"]
        u = User(
            email=email,
            username=username or email.split("@")[0],
            plan=plan,
            tier=plan,
            daily_quota=dq,
            quota=dq if dq is not None else 0,  # legacy field not used for unlimited
            is_active=True,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
    else:
        defs = plan_defaults(plan)
        u.plan = plan
        u.tier = plan
        u.daily_quota = daily_quota_override if daily_quota_override is not None else defs["daily_quota"]
        u.quota = u.daily_quota if u.daily_quota is not None else 0
        db.commit()
    return u

def issue_or_rotate_token(
    db: Session,
    *,
    email: str,
    username: Optional[str],
    plan: str,
    daily_quota_override: Optional[int] = None,
    months_valid: Optional[int] = 1
) -> User:
    user = get_user_by_email(db, email)
    defs = plan_defaults(plan)
    dq = daily_quota_override if daily_quota_override is not None else defs["daily_quota"]
    mv = defs["months_valid"] if months_valid is None else months_valid
    expires = None if (plan == "free" or mv is None) else datetime.now(UTC) + timedelta(days=30 * max(1, mv))

    if not user:
        api_key = secrets.token_hex(16)
        user = User(
            email=email,
            username=(username or email.split("@")[0]),
            api_key=api_key,
            plan=plan,
            tier=plan,
            daily_quota=dq,
            used_today=0,
            usage_reset_at=datetime.now(UTC),
            expires_at=expires,
            is_active=True
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    # rotate and update plan/limits
    user.api_key = secrets.token_hex(16)
    user.plan = plan
    user.tier = plan
    user.daily_quota = dq
    user.used_today = 0
    user.usage_reset_at = datetime.now(UTC)
    user.expires_at = expires
    user.is_active = True
    db.commit()
    db.refresh(user)
    return user

def check_credentials(db: Session, *, email: str, api_key: str) -> Tuple[bool, Optional[User], Optional[str]]:
    user = get_user_by_email(db, email)
    if not user or user.api_key != api_key:
        return False, None, "Invalid credentials"

    if user.username == "farm_robot":
        return True, user, None

    if not user.is_active:
        return False, None, "Account inactive"

    now = datetime.now(UTC)
    if user.expires_at and user.expires_at <= now:
        return False, None, "Token expired"

    # do not consume here
    ensure_daily_reset(user)
    remaining = None
    if user.daily_quota is not None:
        remaining = max(0, (user.daily_quota or 0) - (user.used_today or 0))
    return True, user, None

def consume_for_user(db: Session, user: User) -> Tuple[bool, Optional[str], Optional[int]]:
    """
    Consume one unit of quota for a user (non-publisher). Returns (ok, reason, remaining).
    """
    if user.username == "farm_robot":
        return True, None, None

    if not user.is_active:
        return False, "inactive", None

    now = datetime.now(UTC)
    if user.expires_at and user.expires_at <= now:
        return False, "expired", None

    ensure_daily_reset(user)

    if user.daily_quota is not None:
        if (user.used_today or 0) >= (user.daily_quota or 0):
            remaining = 0
            db.commit()
            return False, "quota_exhausted", remaining
        user.used_today = (user.used_today or 0) + 1
        remaining = max(0, (user.daily_quota or 0) - (user.used_today or 0))
        db.commit()
        db.refresh(user)
        return True, None, remaining

    # unlimited
    db.commit()
    return True, None, None

def validate_and_consume(db: Session, *, email: str, api_key: str):
    """
    Backward-compatible path: validates by email + api_key and consumes one unit.
    Returns (ok, user, reason).
    """
    ok, user, reason = check_credentials(db, email=email, api_key=api_key)
    if not ok:
        return False, None, reason
    ok2, reason2, _rem = consume_for_user(db, user)
    if not ok2:
        return False, user, reason2
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

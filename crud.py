from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from models import TradeSignal, User, LatestSignal, TradeRecord
from schemas import TradeSignalCreate, TradeRecordCreate
import secrets

UTC = timezone.utc

# ---------- Users ----------
def get_user_by_api_key(db: Session, api_key: str):
    return db.query(User).filter(User.api_key == api_key).first()

def get_user_by_email(db: Session, email: str):
    return db.query(User).filter(User.email == email).first()

def ensure_daily_reset(user: User) -> None:
    now = datetime.now(UTC)
    # reset at UTC midnight (or if usage_reset_at is missing/past day)
    if not user.usage_reset_at or user.usage_reset_at.date() != now.date():
        user.used_today = 0
        user.usage_reset_at = now

def plan_defaults(plan: str):
    plan = (plan or "free").lower()
    if plan == "free":
        return dict(daily_quota=1, months_valid=None)     # no expiry for free
    if plan == "silver":
        return dict(daily_quota=3, months_valid=1)
    if plan == "gold":
        return dict(daily_quota=None, months_valid=1)     # unlimited per day
    return dict(daily_quota=1, months_valid=None)

def issue_or_rotate_token(
    db: Session,
    *,
    email: str,
    username: str,
    plan: str,
    daily_quota_override: int | None = None,
    months_valid: int | None = 1
) -> User:
    user = get_user_by_email(db, email)
    if not user:
        # create new server user record for EA auth
        api_key = secrets.token_hex(16)
        defaults = plan_defaults(plan)
        dq = daily_quota_override if daily_quota_override is not None else defaults["daily_quota"]
        months_valid = defaults["months_valid"] if months_valid is None else months_valid
        expires = None if (plan == "free" or months_valid is None) else datetime.now(UTC) + timedelta(days=30 * months_valid)
        user = User(
            email=email,
            username=username,
            api_key=api_key,
            plan=plan,
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

    # rotate token & update plan/quotas
    user.api_key = secrets.token_hex(16)
    user.plan = plan
    defaults = plan_defaults(plan)
    dq = daily_quota_override if daily_quota_override is not None else defaults["daily_quota"]
    user.daily_quota = dq
    months_valid = defaults["months_valid"] if months_valid is None else months_valid
    user.expires_at = None if (plan == "free" or months_valid is None) else datetime.now(UTC) + timedelta(days=30 * months_valid)
    user.is_active = True
    user.used_today = 0
    user.usage_reset_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    return user

def validate_and_consume(db: Session, *, email: str, api_key: str) -> tuple[bool, User, str | None]:
    user = get_user_by_email(db, email)
    if not user or user.api_key != api_key:
        return False, None, "Invalid credentials"

    # farm_robot never limited
    if user.username == "farm_robot":
        return True, user, None

    if not user.is_active:
        return False, None, "Account inactive"

    now = datetime.now(UTC)
    if user.expires_at and user.expires_at <= now:
        return False, None, "Token expired"

    ensure_daily_reset(user)

    # quota None => unlimited
    if user.daily_quota is not None:
        if user.used_today >= user.daily_quota:
            return False, user, "Daily quota exceeded"
        user.used_today += 1
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
    from datetime import datetime
    db_signal = db.query(LatestSignal).filter(LatestSignal.symbol == signal.symbol).first()
    if db_signal:
        for field, value in signal.dict().items():
            setattr(db_signal, field, value)
        db_signal.updated_at = datetime.utcnow()
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
    from datetime import datetime
    def to_datetime(val):
        if isinstance(val, (float, int)):
            return datetime.utcfromtimestamp(val)
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val)
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

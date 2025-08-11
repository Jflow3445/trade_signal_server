from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import func
from models import TradeSignal, User, LatestSignal, TradeRecord, Activation
from schemas import TradeSignalCreate, TradeRecordCreate
import secrets

UTC = timezone.utc

# ---------- Plan rules ----------
def plan_defaults(plan: str) -> dict:
    p = (plan or "free").lower()
    if p == "gold":   return {"daily_quota": None, "months_valid": 1}  # unlimited per day, monthly rotation
    if p == "silver": return {"daily_quota": 3,    "months_valid": 1}
    return {"daily_quota": 1, "months_valid": None}  # free: no expiry by month

def plan_activation_limit(plan: str):
    p = (plan or "free").lower()
    if p == "gold":   return 3
    if p == "silver": return 1
    return 1  # free

# ---------- Users ----------
def get_user_by_api_key(db: Session, api_key: str):
    return db.query(User).filter(User.api_key == api_key).first()

def get_user_by_email(db: Session, email: str):
    return db.query(User).filter(User.email == email).first()

def ensure_daily_reset(user: User) -> None:
    now = datetime.now(UTC)
    if not user.usage_reset_at or user.usage_reset_at.date() != now.date():
        user.used_today = 0
        user.usage_reset_at = now

def get_remaining_today(user: User):
    if user.username == "farm_robot":
        return None
    ensure_daily_reset(user)
    if user.daily_quota is None:
        return None
    return max(0, int(user.daily_quota) - int(user.used_today))

def consume_n(db: Session, user: User, n: int) -> bool:
    if n <= 0:
        return True
    if user.username == "farm_robot":
        return True
    ensure_daily_reset(user)
    if user.daily_quota is None:
        return True
    if user.used_today + n > user.daily_quota:
        return False
    user.used_today += n
    db.commit()
    db.refresh(user)
    return True

def clear_activations_for_user(db: Session, user_id: int):
    db.query(Activation).filter(Activation.user_id == user_id).delete(synchronize_session=False)
    db.commit()

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
    defaults = plan_defaults(plan)
    dq = daily_quota_override if daily_quota_override is not None else defaults["daily_quota"]
    months_valid = defaults["months_valid"] if months_valid is None else months_valid
    expires = None if (plan == "free" or months_valid is None) else datetime.now(UTC) + timedelta(days=30 * months_valid)

    if not user:
        api_key = secrets.token_hex(16)
        user = User(
            email=email,
            username=username or email.split("@")[0],
            api_key=api_key,
            plan=plan,
            tier=plan,
            daily_quota=dq,
            quota=dq if dq is not None else 0,
            used_today=0,
            usage_reset_at=datetime.now(UTC),
            expires_at=expires,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    # rotate
    user.api_key = secrets.token_hex(16)
    user.plan = plan
    user.tier = plan
    user.daily_quota = dq
    user.quota = dq if dq is not None else 0
    user.used_today = 0
    user.usage_reset_at = datetime.now(UTC)
    user.expires_at = expires
    user.is_active = True
    db.commit()
    db.refresh(user)

    # reset activations each rotation
    clear_activations_for_user(db, user.id)
    return user

# ---------- Activations ----------
def count_activations(db: Session, user_id: int) -> int:
    return db.query(func.count(Activation.id)).filter(Activation.user_id == user_id).scalar() or 0

def find_activation(db: Session, user_id: int, account_id: str, broker_server: str):
    return db.query(Activation).filter(
        Activation.user_id == user_id,
        Activation.account_id == account_id,
        Activation.broker_server == broker_server
    ).first()

def ensure_activation(db: Session, user: User, account_id: str, broker_server: str, hwid: str | None):
    if user.username == "farm_robot":
        return True, 0, None  # no limits for farm poster

    limit = plan_activation_limit(user.plan)
    used = count_activations(db, user.id)

    existing = find_activation(db, user.id, account_id, broker_server)
    if existing:
        existing.last_seen_at = datetime.now(UTC)
        if hwid and not existing.hwid:
            existing.hwid = hwid
        db.commit()
        return True, used, limit

    if used >= (limit or 0):
        return False, used, limit

    a = Activation(
        user_id=user.id,
        account_id=account_id,
        broker_server=broker_server,
        hwid=hwid,
        created_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    db.add(a)
    db.commit()
    return True, used + 1, limit

def list_activations(db: Session, user_id: int):
    return db.query(Activation).filter(Activation.user_id == user_id).order_by(Activation.created_at.asc()).all()

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

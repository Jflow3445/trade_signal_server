from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import func
from models import TradeSignal, User, LatestSignal, TradeRecord, Activation
from schemas import TradeSignalCreate, TradeRecordCreate
import secrets
from schemas import EAOpenPosition
import json
import models
from sqlalchemy import and_
from sqlalchemy.dialects.postgresql import insert as pg_insert
UTC = timezone.utc

# ---------- Plan rules ----------
def plan_defaults(plan: str) -> dict:
    p = (plan or "free").lower()
    if p == "gold":
        return {"daily_quota": None, "months_valid": 1}
    if p == "silver":
        return {"daily_quota": 3, "months_valid": 1}
    # free
    return {"daily_quota": 1, "months_valid": None}

def upsert_open_position(db: Session, user_id: int, account_id: str, broker_server: str, hwid: str | None, p: EAOpenPosition) -> bool:
    """Insert or update a single open position. Returns True if inserted/updated."""
    row = db.query(models.OpenPosition).filter(
        and_(
            models.OpenPosition.user_id == user_id,
            models.OpenPosition.account_id == account_id,
            models.OpenPosition.broker_server == broker_server,
            models.OpenPosition.ticket == p.ticket,
        )
    ).one_or_none()

    changed = False
    if row is None:
        row = models.OpenPosition(
            user_id=user_id,
            account_id=account_id,
            broker_server=broker_server,
            hwid=hwid,
            ticket=p.ticket,
            symbol=p.symbol.upper(),
            side=p.side.lower(),
            volume=p.volume,
            entry_price=p.entry_price,
            sl=p.sl,
            tp=p.tp,
            open_time=p.open_time,
            magic=p.magic,
            comment=p.comment,
            updated_at=datetime.utcnow(),
        )
        db.add(row)
        changed = True
    else:
        # update fields if changed
        def upd(attr, val):
            nonlocal changed
            if getattr(row, attr) != val:
                setattr(row, attr, val)
                changed = True

        upd("hwid", hwid)
        upd("symbol", p.symbol.upper())
        upd("side", p.side.lower())
        upd("volume", p.volume)
        upd("entry_price", p.entry_price)
        upd("sl", p.sl)
        upd("tp", p.tp)
        upd("open_time", p.open_time)
        upd("magic", p.magic)
        upd("comment", p.comment)
        row.updated_at = datetime.utcnow()

    if changed:
        db.commit()
    return changed

def prune_open_positions(db: Session, user_id: int, account_id: str, broker_server: str, keep_tickets: set[str]) -> int:
    """Delete any open positions not in keep_tickets for this user/account/server."""
    q = db.query(models.OpenPosition).filter(
        and_(
            models.OpenPosition.user_id == user_id,
            models.OpenPosition.account_id == account_id,
            models.OpenPosition.broker_server == broker_server
        )
    )
    removed = 0
    for row in q.all():
        if row.ticket not in keep_tickets:
            db.delete(row)
            removed += 1
    if removed:
        db.commit()
    return removed

def get_open_symbols(db: Session, user_id: int, account_id: str, broker_server: str) -> set[str]:
    rows = db.query(models.OpenPosition.symbol).filter(
        and_(
            models.OpenPosition.user_id == user_id,
            models.OpenPosition.account_id == account_id,
            models.OpenPosition.broker_server == broker_server
        )
    ).all()
    return { (r[0] or "").upper() for r in rows }

def ensure_user(db: Session, email: str, username: str | None, plan: str, daily_quota_override: int | None) -> User:
    u = get_user_by_email(db, email)
    defaults = plan_defaults(plan)
    dq = daily_quota_override if daily_quota_override is not None else defaults["daily_quota"]
    if not u:
        u = User(
            email=email,
            username=(username or email.split("@")[0]),
            plan=plan,
            tier=plan,
            daily_quota=dq,
            quota=dq,  # keep legacy column in sync
            is_active=True,
            used_today=0,
            usage_reset_at=datetime.now(UTC),
            expires_at=(None if defaults["months_valid"] is None else datetime.now(UTC) + timedelta(days=30 * defaults["months_valid"]))
        )
        db.add(u)
        db.commit()
        db.refresh(u)
    else:
        u.plan = plan
        u.tier = plan
        u.daily_quota = dq
        u.quota = dq
        # don't change expires_at here; rotation path below sets it explicitly
        db.commit()
        db.refresh(u)
    return u
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
    # Decide final months_valid: if caller passes None, use default
    mv = defaults["months_valid"] if months_valid is None else months_valid
    expires = None if (plan.lower() == "free" or mv is None) else datetime.now(UTC) + timedelta(days=30 * mv)

    if not user:
        user = User(
            email=email,
            username=username or email.split("@")[0],
            api_key=secrets.token_hex(16),
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

    # rotate token and update
    user.api_key = secrets.token_hex(16)
    user.plan = plan
    user.daily_quota = dq
    user.used_today = 0
    user.usage_reset_at = datetime.now(UTC)
    user.expires_at = expires
    user.is_active = True
    db.commit()
    db.refresh(user)
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

def ensure_activation(db, user, account_id: str, broker_server: str, hwid: str | None):
    if user.username == "farm_robot":
        return True, 0, None

    limit = plan_activation_limit(user.plan)
    used  = count_activations(db, user.id)

    # Already at limit? bail early unless it already exists
    existing = find_activation(db, user.id, account_id, broker_server)
    if not existing and used >= (limit or 0):
        return False, used, limit

    stmt = (
        pg_insert(Activation)
        .values(
            user_id=user.id,
            account_id=account_id,
            broker_server=broker_server,
            hwid=hwid,
            created_at=func.now(),
            last_seen_at=func.now(),
        )
        .on_conflict_do_update(
            index_elements=[Activation.user_id, Activation.account_id, Activation.broker_server],
            set_={
                "last_seen_at": func.now(),
                "hwid": func.coalesce(Activation.hwid, hwid),
            },
        )
    )
    db.execute(stmt)
    db.commit()
    # If it already existed, 'used' didn’t increase
    return True, (used if existing else used + 1), limit

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
    from datetime import datetime as dt, timezone
    db_signal = db.query(LatestSignal).filter(LatestSignal.symbol == signal.symbol).first()
    if db_signal:
        for field, value in signal.dict().items():
            setattr(db_signal, field, value)
        db_signal.updated_at = dt.now(timezone.utc)
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

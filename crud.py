import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import func
from models import User, APIToken, TradeSignal, Subscription, SignalRead, TradeRecord

# ---------- Plans & quotas ----------
PLAN_DEFAULTS = {
    "free":   {"daily_quota": 1, "unlimited": False},
    "silver": {"daily_quota": 3, "unlimited": False},
    "gold":   {"daily_quota": None, "unlimited": True},
}

# ---------- Helpers ----------
def utc_now() -> datetime:
    # store naive UTC to match default datetime.utcnow columns
    return datetime.now(timezone.utc).replace(tzinfo=None)

MONTH = timedelta(days=30)

def start_of_utc_day(ts: Optional[datetime] = None) -> datetime:
    ts = ts or utc_now()
    return datetime(ts.year, ts.month, ts.day)

def normalize_plan(plan: Optional[str]) -> str:
    s = (plan or "").strip().lower()
    return s if s in ("silver", "gold") else "free"

def plan_limits(plan: str) -> Dict[str, Any]:
    info = PLAN_DEFAULTS.get(normalize_plan(plan), PLAN_DEFAULTS["free"])
    return {"daily_quota": info["daily_quota"], "unlimited": info["unlimited"]}

def hash_token_for_read(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

# ---------- Users & tokens ----------
def get_user_by_identity(db: Session, user_id: Optional[int], username: Optional[str], email: Optional[str]) -> Optional[User]:
    q = db.query(User)
    if user_id:
        return q.filter(User.id == user_id).first()
    if username:
        return q.filter(func.lower(User.username) == func.lower(username)).first()
    if email:
        return q.filter(func.lower(User.email) == func.lower(email)).first()
    return None

def ensure_user(db: Session, user_id: Optional[int], username: Optional[str], email: Optional[str]) -> User:
    u = get_user_by_identity(db, user_id, username, email)
    if u:
        return u
    # Require at least one identity
    name = username or (email.split("@")[0] if email else None)
    if not name:
        raise ValueError("username or email required")
    u = User(
        username=name,
        email=email,
        plan="free",
        is_active=True,
        created_at=utc_now(),
        updated_at=utc_now()
    )
    db.add(u)
    db.flush()
    return u

def generate_token() -> str:
    return secrets.token_urlsafe(32)

def upsert_active_token(db: Session, user: User, plan: Optional[str] = None, rotate: bool = False) -> Tuple[APIToken, bool]:
    """
    Strict model:
      * Exactly one active token per user.
      * Rotate (delete all existing tokens) if plan changes or rotate=True.
      * New token gets 30-day hard expiry.
      * Mirror user.api_key for legacy display only (not used for auth).
    """
    plan_norm = normalize_plan(plan or user.plan)
    active = db.query(APIToken).filter(
        APIToken.user_id == user.id,
        APIToken.is_active == True
    ).first()
    rotated = False

    if active:
        plan_changed = normalize_plan(active.plan) != plan_norm
        if rotate or plan_changed:
            # HARD ROTATE: delete all prior tokens (strict)
            db.query(APIToken).filter(APIToken.user_id == user.id).delete(synchronize_session=False)
            rotated = True
            active = None
        else:
            # keep existing token, just sync plan if drifted
            if active.plan != plan_norm:
                active.plan = plan_norm
            # Ensure farm_robot never expires (even if an older row had an expiry)
            if (user.username or "").strip().lower() == "farm_robot" and active.expires_at is not None:
                active.expires_at = None
            user.api_key = active.token
            user.plan = plan_norm
            user.updated_at = utc_now()
            db.flush()
            return active, False
    if not active:
        now = utc_now()
        nonexpiring = (user.username or "").strip().lower() == "farm_robot"
        new_tok = APIToken(
            user_id=user.id,
            token=generate_token(),
            plan=plan_norm,
            is_active=True,
            created_at=now,
            expires_at=(None if nonexpiring else now + MONTH),
        )
        db.add(new_tok)
        user.api_key = new_tok.token
        user.plan = plan_norm
        user.updated_at = now
        db.flush()
        return new_tok, rotated or True

def verify_token(db: Session, api_key: str) -> Tuple[bool, Optional[User], Dict[str, Any]]:
    now = utc_now()
    tok = db.query(APIToken).filter(
        APIToken.token == api_key,
        APIToken.is_active == True,
        (APIToken.expires_at == None) | (APIToken.expires_at > now)
    ).first()
    if tok and tok.user and tok.user.is_active:
        limits = plan_limits(tok.plan)
        expires_iso = (
            tok.expires_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
            if tok.expires_at else None
        )
        return True, tok.user, {"plan": tok.plan, **limits, "expires_at": expires_iso}

    return False, None, {"reason": "invalid_or_expired_token"}


def purge_expired_tokens(db: Session) -> int:
    """
    Delete all tokens whose expires_at <= now.
    If a user ends up with no active token, downgrade to free and clear legacy api_key.
    Returns the number of tokens deleted.
    """
    now = utc_now()
    expired = db.query(APIToken).filter(APIToken.expires_at <= now).all()
    if not expired:
        return 0
    affected_user_ids = {t.user_id for t in expired}
    for t in expired:
        db.delete(t)
    db.flush()

    # For any affected user without an active (non-expired) token, reset plan/api_key
    for uid in affected_user_ids:
        has_active = db.query(APIToken).filter(
            APIToken.user_id == uid,
            APIToken.is_active == True,
            APIToken.expires_at > now
        ).first()
        if not has_active:
            u = db.query(User).filter(User.id == uid).first()
            if u:
                u.plan = "free"
                u.api_key = None
                u.updated_at = now
    return len(expired)

def ensure_subscription_to_sender(db: Session, receiver: User, sender_username: str = None) -> None:
    """
    Make sure `receiver` is subscribed to `sender_username` (default from env or 'farm_robot').
    No-op if sender missing or already subscribed or same user.
    """
    sender_username = sender_username or os.getenv("DEFAULT_SIGNAL_SENDER", "farm_robot")
    sender = db.query(User).filter(func.lower(User.username) == func.lower(sender_username)).first()
    if not sender or sender.id == receiver.id:
        return
    exists = db.query(Subscription).filter(
        Subscription.receiver_id == receiver.id,
        Subscription.sender_id == sender.id
    ).first()
    if not exists:
        db.add(Subscription(receiver_id=receiver.id, sender_id=sender.id))
        db.flush()

# ---------- Signals ----------
def create_signal(
    db: Session, sender: User, symbol: str, action: str,
    sl_pips=None, tp_pips=None, lot_size=None, details=None
) -> TradeSignal:
    sig = TradeSignal(
        user_id=sender.id, symbol=symbol, action=action,
        sl_pips=sl_pips, tp_pips=tp_pips,
        lot_size=str(lot_size) if lot_size is not None else None,
        details=details or {}, created_at=utc_now()
    )
    db.add(sig)
    db.flush()
    return sig

def get_latest_signals_for_receiver(db: Session, receiver: User, limit: int = 20) -> List[TradeSignal]:
    # If subscriptions exist, only from those senders; else return empty
    subs = db.query(Subscription).filter(Subscription.receiver_id == receiver.id).all()
    if not subs:
        default_sender_name = os.getenv("DEFAULT_SIGNAL_SENDER", "farm_robot")
        default_sender = db.query(User).filter(func.lower(User.username) == func.lower(default_sender_name)).first()
        if not default_sender:
            return []
        sender_ids = [default_sender.id]
    else:
        sender_ids = [s.sender_id for s in subs]
    q = db.query(TradeSignal).filter(
        TradeSignal.user_id.in_(sender_ids)
    ).order_by(TradeSignal.id.desc()).limit(limit)
    return list(reversed(q.all()))  # ascending delivery

def get_signals_for_receiver_since(
    db: Session,
    receiver: User,
    limit: int = 20,
    since_id: Optional[int] = None,
    min_created_at: Optional[datetime] = None,
) -> List[TradeSignal]:
    subs = db.query(Subscription).filter(Subscription.receiver_id == receiver.id).all()
    if not subs:
        return []
    sender_ids = [s.sender_id for s in subs]

    q = db.query(TradeSignal).filter(TradeSignal.user_id.in_(sender_ids))
    if since_id is not None and since_id > 0:
        q = q.filter(TradeSignal.id > since_id)
    if min_created_at is not None:
        q = q.filter(TradeSignal.created_at >= min_created_at)

    # Ascending so clients can process in order
    q = q.order_by(TradeSignal.id.asc()).limit(limit)
    return q.all()

def count_reads_today(db: Session, receiver: User, token_hash: Optional[str] = None) -> int:
    sod = start_of_utc_day()
    q = db.query(SignalRead).filter(
        SignalRead.receiver_id == receiver.id,
        SignalRead.read_at >= sod
    )
    if token_hash:
        q = q.filter(SignalRead.token_hash == token_hash)
    return q.count()

def record_signal_read(db: Session, signal_id: int, receiver: User, token_hash: str):
    # Insert best-effort; unique constraint prevents double-count inflations
    sr = SignalRead(signal_id=signal_id, receiver_id=receiver.id, token_hash=token_hash, read_at=utc_now())
    db.add(sr)
    try:
        db.flush()
    except Exception:
        db.rollback()

# ---------- Trades ----------
def record_trade(db: Session, receiver: User, symbol: str, action: str, details=None) -> TradeRecord:
    tr = TradeRecord(user_id=receiver.id, action=action, symbol=symbol, details=details or {}, created_at=utc_now())
    db.add(tr)
    db.flush()
    return tr

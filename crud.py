import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from models import User, APIToken, TradeSignal, Subscription, SignalRead, TradeRecord

# ---------- Plans & quotas ----------
PLAN_DEFAULTS = {
    "free":   {"daily_quota": 1, "unlimited": False},
    "silver": {"daily_quota": 3, "unlimited": False},
    "gold":   {"daily_quota": None, "unlimited": True},
}

# ---------- Helpers ----------
def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

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
    u = User(username=name, email=email, plan="free", is_active=True, created_at=utc_now(), updated_at=utc_now())
    db.add(u); db.flush()
    return u

def generate_token() -> str:
    return secrets.token_urlsafe(32)

def upsert_active_token(db: Session, user: User, plan: Optional[str] = None, rotate: bool = False) -> Tuple[APIToken, bool]:
    """
    Keep exactly one active token per user.
    - Rotate if explicit OR if plan actually changes.
    - Deactivate all prior tokens when rotating.
    - Mirror user.api_key to the active token for legacy clients.
    """
    plan_norm = normalize_plan(plan or user.plan)
    active = db.query(APIToken).filter(APIToken.user_id == user.id, APIToken.is_active == True).first()
    rotated = False

    if active:
        plan_changed = normalize_plan(active.plan) != plan_norm
        if rotate or plan_changed:
            # hard rotate: invalidate all active tokens, issue new one with new plan
            db.query(APIToken).filter(APIToken.user_id == user.id, APIToken.is_active == True)\
                              .update({APIToken.is_active: False})
            new_tok = APIToken(user_id=user.id, token=generate_token(), plan=plan_norm, is_active=True, created_at=utc_now())
            db.add(new_tok)
            user.api_key = new_tok.token
            user.plan = plan_norm
            user.updated_at = utc_now()
            db.flush()
            return new_tok, True
        else:
            # no plan change: keep token, just ensure stored plan is consistent
            if active.plan != plan_norm:
                active.plan = plan_norm
            user.api_key = active.token
            user.plan = plan_norm
            user.updated_at = utc_now()
            db.flush()
            return active, False

    # no active token exists -> create a fresh one (counts as rotation)
    new_tok = APIToken(user_id=user.id, token=generate_token(), plan=plan_norm, is_active=True, created_at=utc_now())
    db.add(new_tok)
    user.api_key = new_tok.token
    user.plan = plan_norm
    user.updated_at = utc_now()
    db.flush()
    return new_tok, True


def verify_token(db: Session, api_key: str) -> Tuple[bool, Optional[User], Dict[str, Any]]:
    # Prefer token row
    tok = db.query(APIToken).filter(APIToken.token == api_key, APIToken.is_active == True).first()
    if tok and tok.user and tok.user.is_active:
        limits = plan_limits(tok.plan)
        return True, tok.user, {"plan": tok.plan, **limits}
    # Fallback legacy
    u = db.query(User).filter(User.api_key == api_key, User.is_active == True).first()
    if u:
        limits = plan_limits(u.plan)
        return True, u, {"plan": u.plan, **limits}
    return False, None, {"reason": "invalid_token"}

# ---------- Signals ----------
def create_signal(db: Session, sender: User, symbol: str, action: str, sl_pips=None, tp_pips=None, lot_size=None, details=None) -> TradeSignal:
    sig = TradeSignal(
        user_id=sender.id, symbol=symbol, action=action,
        sl_pips=sl_pips, tp_pips=tp_pips, lot_size=str(lot_size) if lot_size is not None else None,
        details=details or {}, created_at=utc_now()
    )
    db.add(sig); db.flush()
    return sig

def get_latest_signals_for_receiver(db: Session, receiver: User, limit: int = 20) -> List[TradeSignal]:
    # If subscriptions exist, only from those senders; else return empty
    subs = db.query(Subscription).filter(Subscription.receiver_id == receiver.id).all()
    if not subs:
        return []
    sender_ids = [s.sender_id for s in subs]
    q = db.query(TradeSignal).filter(TradeSignal.user_id.in_(sender_ids)).order_by(TradeSignal.id.desc()).limit(limit)
    return list(reversed(q.all()))  # ascending delivery

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
    db.add(tr); db.flush()
    return tr

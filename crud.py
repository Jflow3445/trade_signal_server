import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone, date
from typing import List, Optional, Tuple, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_, select, text
from models import User, APIToken, TradeSignal, Subscription, SignalRead, TradeRecord

# ---------- Plans & quotas ----------
PLAN_DEFAULTS = {
    "free":   {"daily_quota": 1, "unlimited": False},
    "silver": {"daily_quota": 3, "unlimited": False},
    "gold":   {"daily_quota": None, "unlimited": True},  # unlimited
}

def normalize_plan(plan: Optional[str]) -> str:
    p = (plan or "").strip().lower()
    return p if p in PLAN_DEFAULTS else "free"

def plan_limits(plan: Optional[str]) -> Dict[str, Any]:
    p = normalize_plan(plan)
    return PLAN_DEFAULTS[p]

# ---------- Helpers ----------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def hash_token_for_read(token: str) -> str:
    # store a one-way hash to deduplicate reads by token without keeping the token itself
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def start_of_utc_day(ts: Optional[datetime] = None) -> datetime:
    ts = ts or utc_now()
    return datetime(ts.year, ts.month, ts.day, tzinfo=timezone.utc)

# ---------- Users & tokens ----------
def user_by_email(db: Session, email: str) -> Optional[User]:
    return db.query(User).filter(func.lower(User.email) == func.lower(email)).first()

def user_by_api_token(db: Session, token: str) -> Optional[User]:
    """
    Resolve an API token to the owning user. Supports both:
      - legacy per-user api_key column
      - new APIToken table (current approach)
    """
    # First try the APIToken table (recommended)
    tok = db.query(APIToken).filter(
        APIToken.token == token,
        APIToken.is_active == True
    ).first()
    if tok and tok.user and tok.user.is_active:
        return tok.user

    # Fallback: legacy api_key on users
    user = db.query(User).filter(
        User.api_key == token,
        User.is_active == True
    ).first()
    return user

def token_record(db: Session, token: str) -> Optional[APIToken]:
    return db.query(APIToken).filter(APIToken.token == token).first()

def upsert_token_for_user(db: Session, user: User, token: str, plan: Optional[str] = None, is_active: Optional[bool] = None) -> APIToken:
    tok = db.query(APIToken).filter(APIToken.token == token).first()
    if tok is None:
        tok = APIToken(token=token, user_id=user.id)
        db.add(tok)
    if plan is not None:
        tok.plan = normalize_plan(plan)
    if is_active is not None:
        tok.is_active = is_active
    db.flush()
    return tok

# ---------- Subscriptions ----------
def subscriptions_for_receiver(db: Session, receiver: User) -> List[Subscription]:
    return db.query(Subscription).filter(Subscription.receiver_id == receiver.id).all()

# ---------- Signal posting (sender) ----------
def create_signal(
    db: Session,
    sender: User,
    symbol: str,
    action: str,
    sl_pips: Optional[int],
    tp_pips: Optional[int],
    lot_size: Optional[float],
    details: Optional[Dict[str, Any]] = None
) -> TradeSignal:
    sig = TradeSignal(
        user_id=sender.id,
        symbol=symbol,
        action=action,
        sl_pips=sl_pips,
        tp_pips=tp_pips,
        lot_size=lot_size,
        details=details or {},
        created_at=utc_now()
    )
    db.add(sig)
    db.flush()
    return sig

# ---------- Quota checking ----------
def count_actionable_opens_today(db: Session, receiver: User) -> int:
    """
    Count number of actionable BUY/SELL signals the receiver has opened today (UTC).
    Uses SignalRead records as the source of truth for 'delivered/consumed' quota.
    """
    today_utc = start_of_utc_day()
    # Count distinct actionable signals that have a read for this receiver for today.
    q = (
        db.query(func.count(func.distinct(SignalRead.signal_id)))
        .join(TradeSignal, TradeSignal.id == SignalRead.signal_id)
        .filter(
            SignalRead.receiver_id == receiver.id,
            SignalRead.read_at >= today_utc,
            TradeSignal.action.in_(["buy", "sell"])
        )
    )
    return int(q.scalar() or 0)

def token_daily_usage_today(db: Session, receiver: User, token_hash: str) -> int:
    """
    Count distinct actionable signals consumed today by this specific token (token_hash),
    for this receiver. This helps prevent a single user from using multiple tokens to
    bypass quota (each token accrues usage), while also letting new tokens enforce quota
    immediately even if the user upgraded/downgraded moments ago.
    """
    today_utc = start_of_utc_day()
    q = (
        db.query(func.count(func.distinct(SignalRead.signal_id)))
        .join(TradeSignal, TradeSignal.id == SignalRead.signal_id)
        .filter(
            SignalRead.receiver_id == receiver.id,
            SignalRead.token_hash == token_hash,
            SignalRead.read_at >= today_utc,
            TradeSignal.action.in_(["buy", "sell"])
        )
    )
    return int(q.scalar() or 0)

def effective_quota_for_token(db: Session, receiver: User, bearer_token: Optional[str]) -> Tuple[bool, Optional[int]]:
    """
    Determine if the token is unlimited (gold) or a finite quota.
    Rules:
      - If token exists in APIToken table, use its plan (most up-to-date per-token plan).
      - Else fallback to receiver.plan (legacy).
    Returns (is_unlimited, daily_quota_int_or_None)
    """
    if bearer_token:
        tok = token_record(db, bearer_token)
        if tok:
            limits = plan_limits(tok.plan)
            return (limits["unlimited"], limits["daily_quota"])

    # fallback to legacy user plan
    limits = plan_limits(receiver.plan or "free")
    return (limits["unlimited"], limits["daily_quota"])

# ---------- Reading signals (receiver) ----------
def latest_signals_for_token(
    db: Session,
    receiver_token: str,
    limit: int = 50
) -> Tuple[User, List[TradeSignal], Dict[str, Any]]:
    """
    Fetch signals for the user that owns receiver_token, respecting per-token quota:
      * Tokens on 'gold' are unlimited.
      * Finite plans ('free','silver', etc.) are enforced per UTC day.
      * We never decrement quota for non-actionable actions (e.g. adjust_sl/close).
    We also stamp a SignalRead per (receiver, signal, token_hash) once we return it.
    """
    receiver = user_by_api_token(db, receiver_token)
    if not receiver or not receiver.is_active:
        raise ValueError("Invalid or inactive API token")

    # Who are you subscribed to?
    subs = subscriptions_for_receiver(db, receiver)
    sender_ids = [s.sender_id for s in subs]
    if not sender_ids:
        return receiver, [], {"plan": receiver.plan, "unlimited": False, "daily_quota": 0, "remaining": 0}

    # Determine quota from the token's own plan if present
    unlimited, daily_quota = effective_quota_for_token(db, receiver, receiver_token)
    token_hash = hash_token_for_read(receiver_token)

    # Pull latest signals from all subscribed senders
    signals = (
        db.query(TradeSignal)
        .filter(TradeSignal.user_id.in_(sender_ids))
        .order_by(TradeSignal.id.desc())
        .limit(max(200, limit))  # get a healthy window; we'll trim after quota filter
        .all()
    )

    delivered: List[TradeSignal] = []
    actionable_seen = 0

    # Preload today's usage counts
    total_used_today = count_actionable_opens_today(db, receiver)
    token_used_today = token_daily_usage_today(db, receiver, token_hash)

    # If unlimited plan, we just deliver everything and record reads
    if unlimited:
        for s in signals[:limit]:
            _record_read_once(db, receiver, s, token_hash)
            delivered.append(s)
        return receiver, delivered, {
            "plan": "gold",
            "unlimited": True,
            "daily_quota": None,
            "remaining": None,
            "used_today": total_used_today,
            "token_used_today": token_used_today
        }

    # Finite plan
    quota = int(daily_quota or 0)
    remaining_total = max(quota - total_used_today, 0)
    remaining_for_token = max(quota - token_used_today, 0)

    # We enforce *both*: you can't exceed total per-user/day nor per-token/day
    remaining = min(remaining_total, remaining_for_token)

    for s in signals:
        is_actionable = s.action in ("buy", "sell")
        if is_actionable:
            if remaining <= 0:
                # quota exhausted; skip actionable
                continue
            if _was_read_by_token(db, receiver, s, token_hash):
                # already counted for this token; do not double-count, but we can still return it
                delivered.append(s)
                continue
            # consume one quota unit
            _record_read_once(db, receiver, s, token_hash)
            actionable_seen += 1
            remaining -= 1
            delivered.append(s)
        else:
            # non-actionable (e.g. adjust_sl, close) - return but record read for dedupe
            _record_read_once(db, receiver, s, token_hash)
            delivered.append(s)

        if len(delivered) >= limit:
            break

    meta = {
        "plan": receiver.plan,
        "unlimited": False,
        "daily_quota": quota,
        "remaining": remaining,
        "used_today": total_used_today + actionable_seen,
        "token_used_today": token_used_today + actionable_seen,
    }
    return receiver, delivered, meta

def _was_read_by_token(db: Session, receiver: User, signal: TradeSignal, token_hash: str) -> bool:
    exists = (
        db.query(SignalRead)
        .filter(
            SignalRead.signal_id == signal.id,
            SignalRead.receiver_id == receiver.id,
            SignalRead.token_hash == token_hash
        )
        .first()
    )
    return exists is not None

def _record_read_once(db: Session, receiver: User, signal: TradeSignal, token_hash: str) -> None:
    """
    Insert SignalRead if it doesn't exist yet for (signal, receiver, token_hash).
    This relies on a DB unique index:
      CREATE UNIQUE INDEX uniq_signal_reads_token_signal
        ON signal_reads (token_hash, signal_id)
        WHERE token_hash IS NOT NULL;
    """
    if _was_read_by_token(db, receiver, signal, token_hash):
        return
    sr = SignalRead(signal_id=signal.id, receiver_id=receiver.id, token_hash=token_hash, read_at=utc_now())
    db.add(sr)
    db.flush()

# ---------- Records ----------
def create_trade_record(
    db: Session,
    receiver: User,
    action: str,
    symbol: str,
    details: Optional[Dict[str, Any]] = None
) -> TradeRecord:
    rec = TradeRecord(
        user_id=receiver.id,
        action=action,
        symbol=symbol,
        details=details or {},
        created_at=utc_now()
    )
    db.add(rec)
    db.flush()
    return rec

# ---------- Admin ops ----------
def admin_issue_token(db: Session, username_or_email: str, token: str, plan: str) -> Tuple[User, APIToken]:
    # Find by username or email
    u = db.query(User).filter(
        or_(
            func.lower(User.username) == func.lower(username_or_email),
            func.lower(User.email) == func.lower(username_or_email),
        )
    ).first()
    if not u:
        raise ValueError("User not found")

    tok = upsert_token_for_user(db, u, token=token, plan=plan, is_active=True)
    # Also reflect on user's plan for legacy paths if you want:
    u.plan = normalize_plan(plan)
    db.flush()
    return u, tok

def validate_email_token(db: Session, email: str, api_key: str) -> Tuple[bool, Optional[User], Dict[str, Any]]:
    """
    Validate a pair (email, api_key). Works for either APIToken or legacy User.api_key
    Returns (ok, user?, info)
    """
    u = user_by_email(db, email)
    if not u or not u.is_active:
        return False, None, {"reason": "user_not_found_or_inactive"}

    # Does api_key match one of their active tokens?
    tok = db.query(APIToken).filter(
        APIToken.user_id == u.id,
        APIToken.token == api_key,
        APIToken.is_active == True
    ).first()
    if tok:
        limits = plan_limits(tok.plan)
        return True, u, {"plan": tok.plan, "daily_quota": limits["daily_quota"], "unlimited": limits["unlimited"], "expires_at": None}

    # Fallback: legacy user.api_key
    if u.api_key == api_key:
        limits = plan_limits(u.plan)
        return True, u, {"plan": u.plan, "daily_quota": limits["daily_quota"], "unlimited": limits["unlimited"], "expires_at": None}

    return False, None, {"reason": "bad_credentials"}

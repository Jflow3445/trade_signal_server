from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from models import User, TradeSignal


# ------------------------------------------------------------
# Users
# ------------------------------------------------------------
def get_user_by_email_and_token(db: Session, email: str, api_key: str) -> Optional[User]:
    return (
        db.query(User)
        .filter(User.email == email, User.api_key == api_key)
        .first()
    )


def get_user_by_token(db: Session, api_key: str) -> Optional[User]:
    return (
        db.query(User)
        .filter(User.api_key == api_key)
        .first()
    )


# ------------------------------------------------------------
# Plan / quota (effective)
#   - If users.daily_quota is set, it overrides plan default
#   - Defaults: free=1, silver=3, gold=None (unlimited)
# ------------------------------------------------------------
def plan_default_quota(plan: Optional[str]) -> Optional[int]:
    plan = (plan or "free").lower()
    if plan == "free":
        return 1
    if plan == "silver":
        return 3
    if plan == "gold":
        return None
    # Unknown plans fall back to free
    return 1


def effective_plan_for_user(user: User) -> Dict[str, Optional[int]]:
    plan = (user.plan or "free").lower()
    # explicit per-user daily_quota overrides the plan default (including None for unlimited)
    if user.daily_quota is None:
        quota = plan_default_quota(plan)
    else:
        quota = user.daily_quota
    return {"plan": plan, "daily_quota": quota}


# ------------------------------------------------------------
# Signals (sender creates)
# ------------------------------------------------------------
def create_signal(
    db: Session,
    user_id: int,
    symbol: str,
    action: str,
    sl_pips: int,
    tp_pips: int,
    lot_size: float,
    details: dict,
) -> TradeSignal:
    s = TradeSignal(
        user_id=user_id,
        symbol=symbol,
        action=action,
        sl_pips=sl_pips,
        tp_pips=tp_pips,
        lot_size=lot_size,
        details=details or {},
        created_at=datetime.now(timezone.utc),
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


# ------------------------------------------------------------
# Signals feed for a receiver (join subscriptions)
# Returns list of dicts for easy pydantic casting in main
# ------------------------------------------------------------
def list_latest_signals_for_receiver(db: Session, receiver_id: int) -> List[dict]:
    # We return the entire stream (client filters by last-id).
    # Adjust ORDER BY as needed; here ascending by id to keep original execution order.
    sql = text(
        """
        SELECT ts.id,
               ts.symbol,
               ts.action,
               ts.sl_pips,
               ts.tp_pips,
               ts.lot_size,
               ts.details,
               ts.created_at
        FROM trade_signals ts
        JOIN subscriptions sub ON sub.sender_id = ts.user_id
        WHERE sub.receiver_id = :rid
        ORDER BY ts.id ASC
        """
    )
    rows = db.execute(sql, {"rid": receiver_id}).mappings().all()
    # Force JSON-able Python types
    return [
        {
            "id": int(r["id"]),
            "symbol": str(r["symbol"]),
            "action": str(r["action"]),
            "sl_pips": int(r["sl_pips"]),
            "tp_pips": int(r["tp_pips"]),
            "lot_size": float(r["lot_size"]),
            "details": r["details"] if isinstance(r["details"], dict) else None,
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# ------------------------------------------------------------
# Delivery accounting: per-token, per-day, actionable-only
#   - signal_reads(token_hash, signal_id, created_at)
#   - Unique index on (token_hash, signal_id) avoids double-count
# ------------------------------------------------------------
def track_signal_read(db: Session, token_hash: str, signal_id: int) -> bool:
    """
    Insert a delivery record if not exists.
    Returns True if a new row was inserted, False if it already existed.
    """
    sql = text(
        """
        INSERT INTO signal_reads (token_hash, signal_id, created_at)
        VALUES (:th, :sid, NOW() AT TIME ZONE 'UTC')
        ON CONFLICT (token_hash, signal_id) DO NOTHING
        """
    )
    res = db.execute(sql, {"th": token_hash, "sid": signal_id})
    db.commit()
    # SQLAlchemy's rowcount is unreliable on PostgreSQL for ON CONFLICT DO NOTHING,
    # but it is 1 when inserted and 0 when skipped in recent versions.
    return bool(getattr(res, "rowcount", 0))


def count_actionables_today_for_token(db: Session, token_hash: str) -> int:
    """
    Count how many actionable (buy/sell) deliveries this token has for the current UTC day.
    Uses sr.created_at if present; otherwise falls back to ts.created_at.
    """
    sql = text(
        """
        SELECT COUNT(*) AS c
        FROM signal_reads sr
        JOIN trade_signals ts ON ts.id = sr.signal_id
        WHERE sr.token_hash = :th
          AND ts.action IN ('buy','sell')
          AND (COALESCE(sr.created_at, ts.created_at) AT TIME ZONE 'UTC')::date =
              (NOW() AT TIME ZONE 'UTC')::date
        """
    )
    c = db.execute(sql, {"th": token_hash}).scalar_one()
    return int(c)

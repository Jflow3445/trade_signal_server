from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from typing import Optional, List, Dict
from datetime import datetime, timezone
import hashlib
import logging

from .database import get_db, Base, engine
from . import models, schemas, crud
from sqlalchemy.orm import Session

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nister")

app = FastAPI(title="Trade Signal Server")

# Create tables on startup if not exist
Base.metadata.create_all(bind=engine)

@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

# --------------------------
# AUTH HELPERS
# --------------------------
def get_bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

# --------------------------
# VALIDATE
# --------------------------
@app.post("/validate", response_model=schemas.ValidateResponse)
def validate(payload: schemas.ValidateRequest, db: Session = Depends(get_db)):
    """
    Validate a user by email + api_key.
    """
    user = crud.get_user_by_email_or_api(db, email=payload.email, api_key=payload.api_key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # active and not expired
    is_active = bool(user.is_active) and (not user.expires_at or user.expires_at >= datetime.now(timezone.utc))
    daily_quota = user.daily_quota if user.daily_quota is not None else crud.plan_to_quota(user.plan)

    return schemas.ValidateResponse(
        ok=True,
        is_active=is_active,
        plan=user.plan or "free",
        daily_quota=daily_quota,
        expires_at=user.expires_at.replace(tzinfo=timezone.utc) if user.expires_at else None,
    )

# --------------------------
# CREATE SIGNAL (Sender)
# --------------------------
@app.post("/signals", response_model=schemas.SignalOut)
def post_signal(
    payload: schemas.SignalCreate,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
):
    token = get_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    sender = crud.get_user_by_api_key(db, token)
    if not sender:
        raise HTTPException(status_code=401, detail="Invalid API token")

    # Only allow known actions
    if payload.action not in {"buy", "sell", "adjust_sl", "tp", "close"}:
        raise HTTPException(status_code=400, detail="Invalid action")

    # If it's an opening action, enforce sender's quota
    if payload.action in {"buy", "sell"}:
        if not crud.within_quota(db, sender):
            raise HTTPException(status_code=403, detail="Quota exhausted for today")

    sig = crud.create_signal(db, payload, user_id=sender.id)
    return schemas.SignalOut.from_orm(sig)

# --------------------------
# LIST SIGNALS (Receiver)
# --------------------------
@app.get("/signals", response_model=List[schemas.SignalOut])
def get_signals(
    request: Request,
    authorization: Optional[str] = Header(None),
    since_id: Optional[int] = None,
    symbol: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 200,
    db: Session = Depends(get_db)
):
    token = get_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    receiver = crud.get_user_by_api_key(db, token)
    if not receiver:
        raise HTTPException(status_code=401, detail="Invalid API token")

    # Enforce receiver must be active / not expired
    if not receiver.is_active or (receiver.expires_at and receiver.expires_at < datetime.now(timezone.utc)):
        raise HTTPException(status_code=403, detail="Inactive or expired account")

    items = crud.list_signals_for_receiver(
        db=db,
        receiver=receiver,
        since_id=since_id,
        symbol=symbol,
        action=action,
        limit=min(limit, 500),
    )

    # Record a read marker for each returned signal (per-token hash to avoid duplicates)
    token_h = hash_token(token)
    for it in items:
        try:
            if not crud.already_read_this_signal(db, receiver_id=receiver.id, signal_id=it["id"], token_hash=token_h):
                crud.record_signal_read(db, receiver_id=receiver.id, signal_id=it["id"], token_hash=token_h)
        except Exception as e:
            logger.warning(f"record_signal_read failed for receiver={receiver.id} signal={it['id']}: {e}")

    # Set quota headers for visibility
    eff_quota = receiver.daily_quota if receiver.daily_quota is not None else crud.plan_to_quota(receiver.plan)
    used_opens = crud.count_open_actions_today_for_user(db, receiver.id)
    remaining = None if eff_quota is None else max(eff_quota - used_opens, 0)

    headers = {}
    if eff_quota is None:
        headers["X-Plan"] = receiver.plan or "gold"
        headers["X-Quota-Daily"] = "unlimited"
        headers["X-Quota-Used-Opens-Today"] = str(used_opens)
    else:
        headers["X-Plan"] = receiver.plan or "free"
        headers["X-Quota-Daily"] = str(eff_quota)
        headers["X-Quota-Used-Opens-Today"] = str(used_opens)
        headers["X-Quota-Remaining"] = str(remaining)

    return JSONResponse(content=items, headers=headers)

# --------------------------
# ADMIN: Users, Subs, Signals
# --------------------------
@app.get("/admin/users", response_model=List[schemas.UserOut])
def admin_list_users(db: Session = Depends(get_db)):
    return db.query(models.User).order_by(models.User.id.asc()).all()

@app.post("/admin/users", response_model=schemas.UserOut)
def admin_create_user(user_in: schemas.UserCreate, db: Session = Depends(get_db)):
    u = crud.create_user(db, user_in)
    return u

@app.post("/admin/users/{user_id}/plan", response_model=schemas.UserOut)
def admin_update_plan(
    user_id: int,
    update: schemas.UserPlanUpdate,
    db: Session = Depends(get_db)
):
    u = db.query(models.User).get(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    u = crud.update_user_plan_and_quota(
        db, u,
        plan=update.plan,
        daily_quota=update.daily_quota,
        expires_at=update.expires_at,
    )
    return u

@app.post("/admin/subscriptions", response_model=schemas.SubscriptionOut)
def admin_add_sub(sub: schemas.SubscriptionCreate, db: Session = Depends(get_db)):
    s = crud.add_subscription(db, sub.receiver_id, sub.sender_id)
    return s

@app.delete("/admin/subscriptions", response_model=schemas.SubscriptionOut)
def admin_del_sub(sub: schemas.SubscriptionCreate, db: Session = Depends(get_db)):
    ok = crud.remove_subscription(db, sub.receiver_id, sub.sender_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return JSONResponse({"ok": True})

@app.get("/admin/signals", response_model=List[schemas.SignalOut])
def admin_list_all(db: Session = Depends(get_db)):
    rows = crud.list_all_signals(db, limit=500)
    return [schemas.SignalOut.from_orm(r) for r in rows]

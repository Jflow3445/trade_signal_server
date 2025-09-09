# main.py
from __future__ import annotations

import hmac
import os
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Query, Header, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

import models
from database import SessionLocal, engine
import crud
from schemas import (
    TradeSignalCreate, TradeSignalOut, LatestSignalOut,
    TradeRecordCreate, TradeRecordOut,
    AdminIssueTokenRequest, AdminIssueTokenResponse,
    ValidateRequest, ValidateResponse,
    EAOpenPosition, EASyncRequest, EASyncResponse
)

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Trade Signal Server", version="1.0")

# CORS (adjust as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# Config
SENDER_USERNAME = os.getenv("SENDER_USERNAME", "farm_robot")  # the ONLY user allowed to POST /signals
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")                    # bearer token for /admin endpoints

# ----- DB dependency -----
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ----- auth helpers -----
def get_api_token(authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)) -> str:
    """
    Accepts Authorization: Bearer <token> or X-API-Key header.
    """
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    if x_api_key:
        return x_api_key.strip()
    raise HTTPException(status_code=401, detail="missing_token")

def require_admin(authorization: Optional[str] = Header(None)):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="admin_disabled")
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token or not hmac.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="forbidden")

# ----- helpers -----
def _effective_plan_and_quota(db: Session, user: models.User) -> dict:
    """
    Returns {"plan": str, "daily_quota": Optional[int], "expires_at": datetime|None, "is_active": bool}
    Plan->quota default, then apply per-user override, then referral boost (if any).
    """
    base = crud.plan_defaults(user.plan)
    quota = base["daily_quota"]
    if user.daily_quota is not None:
        quota = user.daily_quota

    # Optional referral boost
    boost = crud.get_active_referral_boost(db, user.id)
    if boost:
        boosted = crud.plan_defaults(boost.boost_to)["daily_quota"]  # may be None (unlimited)
        quota = boosted

    return {
        "plan": user.plan,
        "daily_quota": quota,   # None means unlimited
        "expires_at": user.expires_at,
        "is_active": user.is_active,
    }

ACTIONABLE = {"buy", "sell", "adjust_sl", "adjust_tp", "close", "hold"}

def _require_sender(user: models.User):
    if user.username != SENDER_USERNAME or not user.is_active:
        raise HTTPException(status_code=403, detail="forbidden")

# ------------------ Routes ------------------

@app.post("/signals", response_model=TradeSignalOut)
def post_signal(payload: TradeSignalCreate, token: str = Depends(get_api_token), db: Session = Depends(get_db)):
    user = crud.user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    _require_sender(user)

    if payload.action not in ACTIONABLE:
        raise HTTPException(status_code=400, detail="unsupported_action")

    # Publishing by the sender is not quota-limited. Quotas apply to GET consumers.
    sig = crud.create_signal(db, user_id=user.id, s=payload)
    return sig

@app.get("/signals", response_model=List[TradeSignalOut])
def get_signals(
    limit: int = Query(100, ge=1, le=500),
    max_age_minutes: int = Query(3, ge=1, le=60),
    token: str = Depends(get_api_token),
    db: Session = Depends(get_db),
    resp: Response = None,
):
    # authenticate
    user = crud.user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")

    # signals come from the designated sender
    sender = db.query(models.User).filter(models.User.username == SENDER_USERNAME, models.User.is_active == True).one_or_none()
    if not sender:
        raise HTTPException(status_code=500, detail="sender_user_not_found")

    eff = _effective_plan_and_quota(db, user)
    quota = eff["daily_quota"]

    if quota is None:
        rows = crud.list_signals(db, sender.id, limit=limit, max_age_minutes=max_age_minutes)
        if resp:
            resp.headers["X-Quota-Limit"] = "unlimited"
            resp.headers["X-Quota-Remaining"] = "unlimited"
        return rows

    # token-bound quota check
    granted = crud.check_and_consume_quota(db, token, limit, quota)
    if granted == 0:
        if resp:
            resp.headers["X-Quota-Limit"] = str(quota)
            resp.headers["X-Quota-Remaining"] = "0"
        # IMPORTANT: we send **no actionable signals** when exhausted
        raise HTTPException(status_code=429, detail="daily_quota_exhausted")

    rows = crud.list_signals(db, sender.id, limit=granted, max_age_minutes=max_age_minutes)
    crud.consume_quota_for_signals(db, token, len(rows))

    if resp:
        used = crud.get_daily_consumption(db, token)
        remaining = max(quota - used, 0)
        resp.headers["X-Quota-Limit"] = str(quota)
        resp.headers["X-Quota-Remaining"] = str(remaining)
    return rows

@app.get("/signals/latest", response_model=List[LatestSignalOut])
def get_latest(
    limit: int = Query(50, ge=1, le=200),
    max_age_minutes: int = Query(3, ge=1, le=60),
    token: str = Depends(get_api_token),
    db: Session = Depends(get_db),
    resp: Response = None,
):
    user = crud.user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")

    sender = db.query(models.User).filter(models.User.username == SENDER_USERNAME, models.User.is_active == True).one_or_none()
    if not sender:
        raise HTTPException(status_code=500, detail="sender_user_not_found")

    eff = _effective_plan_and_quota(db, user)
    quota = eff["daily_quota"]

    if quota is None:
        rows = crud.list_latest_signals(db, sender.id, limit=limit, max_age_minutes=max_age_minutes)
        if resp:
            resp.headers["X-Quota-Limit"] = "unlimited"
            resp.headers["X-Quota-Remaining"] = "unlimited"
        return rows

    granted = crud.check_and_consume_quota(db, token, limit, quota)
    if granted == 0:
        if resp:
            resp.headers["X-Quota-Limit"] = str(quota)
            resp.headers["X-Quota-Remaining"] = "0"
        raise HTTPException(status_code=429, detail="daily_quota_exhausted")

    rows = crud.list_latest_signals(db, sender.id, limit=granted, max_age_minutes=max_age_minutes)
    crud.consume_quota_for_signals(db, token, len(rows))

    if resp:
        used = crud.get_daily_consumption(db, token)
        remaining = max(quota - used, 0)
        resp.headers["X-Quota-Limit"] = str(quota)
        resp.headers["X-Quota-Remaining"] = str(remaining)
    return rows

# ---- Trades (optional) ----
@app.post("/trades", response_model=TradeRecordOut)
def post_trade(payload: TradeRecordCreate, token: str = Depends(get_api_token), db: Session = Depends(get_db)):
    user = crud.user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    row = crud.create_trade_record(db, payload, user_id=user.id)
    return row

# ---- Admin ----
@app.post("/admin/issue_token", response_model=AdminIssueTokenResponse, dependencies=[Depends(require_admin)])
def admin_issue_token(req: AdminIssueTokenRequest, db: Session = Depends(get_db)):
    user = crud.issue_or_update_user(
        db,
        email=req.email,
        username=req.username,
        plan=req.plan,
        daily_quota_override=req.daily_quota,
        months_valid=req.months_valid,
        rotate_token=True,  # rotate so the new plan's quota applies instantly
    )
    return AdminIssueTokenResponse(
        email=user.email,
        username=user.username,
        plan=user.plan,
        token=user.api_key,
        api_key=user.api_key,
        daily_quota=user.daily_quota,
        expires_at=user.expires_at,
        is_active=user.is_active,
    )

# ---- Validate (EA) ----
@app.post("/validate", response_model=ValidateResponse)
def validate(req: ValidateRequest, db: Session = Depends(get_db)):
    user = crud.user_by_email(db, req.email)
    if not user or not hmac.compare_digest(user.api_key, req.api_key):
        return ValidateResponse(ok=False)
    eff = _effective_plan_and_quota(db, user)
    return ValidateResponse(
        ok=True,
        plan=eff["plan"],
        daily_quota=eff["daily_quota"],
        expires_at=eff["expires_at"],
        is_active=eff["is_active"],
    )

# ---- EA sync open positions (optional) ----
@app.post("/ea/sync_open_positions", response_model=EASyncResponse)
def ea_sync_positions(payload: EASyncRequest, token: str = Depends(get_api_token), db: Session = Depends(get_db)):
    user = crud.user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    act = crud.touch_activation(db, user.id, payload.account_id, payload.broker_server, payload.hwid)
    count = crud.upsert_open_positions(db, user.id, payload.account_id, payload.broker_server, payload.positions or [])
    return EASyncResponse(updated=count)

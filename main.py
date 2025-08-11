import os
import secrets
from datetime import datetime, timezone
from typing import List
from enum import Enum

from fastapi import FastAPI, Depends, HTTPException, Header, Request, Query, Body, Response
from sqlalchemy.orm import Session

from database import SessionLocal, engine
import models
import crud
from schemas import (
    TradeSignalCreate,
    TradeSignalOut,
    TradeRecordCreate,
    TradeRecordOut,
    LatestSignalOut,
    AdminIssueTokenRequest,
    AdminIssueTokenResponse,
    ValidateRequest,
    ValidateResponse,
)

models.Base.metadata.create_all(bind=engine)

app = FastAPI()

# ---------------- Database Dependency ----------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------------- Auth Helpers ----------------
def get_current_user(api_key: str = Query(None), db: Session = Depends(get_db)):
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API token")
    user = crud.get_user_by_api_key(db, api_key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API token")

    # Apply expiry/active checks for non-farm users
    if user.username != "farm_robot":
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account inactive")
        now = datetime.now(timezone.utc)
        if user.expires_at and user.expires_at <= now:
            raise HTTPException(status_code=401, detail="Token expired")

    return user

def require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != os.getenv("TRADE_SERVER_API_KEY", "b0e216c8d199091f36aaada01a056211"):
        raise HTTPException(status_code=403, detail="Forbidden")
    return True

# Utility
def _is_actionable(action: str) -> bool:
    return (action or "").lower() in ("buy", "sell")

# ---------------- Admin: Issue/Renew Token ----------------
@app.post("/admin/tokens/issue", response_model=AdminIssueTokenResponse)
def admin_issue_token(
    req: AdminIssueTokenRequest,
    db: Session = Depends(get_db),
    admin_ok: bool = Depends(require_admin)
):
    user = crud.issue_or_rotate_token(
        db,
        email=req.email,
        username=req.username or req.email.split("@")[0],
        plan=req.plan.lower(),
        daily_quota_override=req.daily_quota,
        months_valid=req.months_valid
    )
    return AdminIssueTokenResponse(
        email=user.email,
        username=user.username,
        plan=user.plan,
        token=user.api_key,          # keep "token" for backwards compatibility
        api_key=user.api_key,
        daily_quota=user.daily_quota,
        expires_at=user.expires_at,
        is_active=user.is_active
    )

# ---------------- EA Validate (and consume quota) ----------------
@app.post("/auth/validate", response_model=ValidateResponse)
def validate(req: ValidateRequest, db: Session = Depends(get_db)):
    ok, user, reason = crud.validate_and_consume(db, email=req.email, api_key=req.api_key, count=1)
    if not ok:
        raise HTTPException(status_code=401, detail=reason or "Unauthorized")

    remaining = crud.remaining_today(user)
    # remaining None => unlimited
    return ValidateResponse(
        ok=True,
        plan=user.plan or "free",
        daily_quota=user.daily_quota,
        remaining_today=remaining,
        expires_at=user.expires_at,
        is_active=user.is_active
    )

# ---------------- Signal Endpoints ----------------

@app.post("/signals", response_model=TradeSignalOut)
def post_signal(signal: TradeSignalCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.username != "farm_robot":
        raise HTTPException(status_code=403, detail="Not authorized to POST signals")
    signal.user_id = user.id
    created_signal = crud.create_signal(db, signal)
    crud.upsert_latest_signal(db, signal)
    return created_signal

@app.get("/signals", response_model=List[LatestSignalOut])
def get_all_latest_signals(
    response: Response,
    db: Session = Depends(get_db),
    user=Depends(get_current_user)
):
    # Always get the canonical, ordered list
    latest: List[models.LatestSignal] = (
        db.query(models.LatestSignal).order_by(models.LatestSignal.symbol.asc()).all()
    )

    if user.username == "farm_robot":
        # Bot: no limits
        return latest

    # Split actionable/non-actionable
    # We will return ALL non-actionable (modifications etc.) without counting,
    # and up to N actionable where N = remaining_today (None => unlimited).
    remaining = crud.remaining_today(user)  # None => unlimited
    actionable_left = float("inf") if remaining is None else int(remaining)

    filtered: List[models.LatestSignal] = []
    consumed = 0

    for s in latest:
        if _is_actionable(s.action):
            if actionable_left > 0:
                filtered.append(s)
                consumed += 1
                if actionable_left != float("inf"):
                    actionable_left -= 1
            else:
                # skip extra actionable beyond quota
                continue
        else:
            # keep non-actionable always
            filtered.append(s)

    # Consume only the number of actionable items we actually returned
    if consumed > 0 and remaining is not None:
        ok, _, reason = crud.validate_and_consume(
            db, email=user.email or user.username, api_key=user.api_key, count=consumed
        )
        if not ok:
            # In case of race or double-consume, just block
            raise HTTPException(status_code=429, detail=reason or "Rate limited")

    # Expose helpful headers
    new_remaining = crud.remaining_today(user)
    response.headers["X-Quota-Daily"] = str(user.daily_quota) if user.daily_quota is not None else "unlimited"
    response.headers["X-Quota-Remaining"] = str(new_remaining) if new_remaining is not None else "unlimited"
    response.headers["X-Actionable-Returned"] = str(consumed)

    return filtered

@app.get("/signals/{symbol}", response_model=TradeSignalOut)
def get_signal(
    symbol: str,
    response: Response,
    db: Session = Depends(get_db),
    user=Depends(get_current_user)
):
    latest_signal = crud.get_latest_signal(db, symbol)
    if not latest_signal:
        raise HTTPException(status_code=404, detail="No signal for this symbol")

    if user.username != "farm_robot":
        if _is_actionable(latest_signal.action):
            # Need at least 1 remaining to serve this actionable
            ok, _, reason = crud.validate_and_consume(
                db, email=user.email or user.username, api_key=user.api_key, count=1
            )
            if not ok:
                raise HTTPException(status_code=429, detail=reason or "Rate limited")
        # Non-actionable: do not consume

        # Helpful headers
        rem = crud.remaining_today(user)
        response.headers["X-Quota-Daily"] = str(user.daily_quota) if user.daily_quota is not None else "unlimited"
        response.headers["X-Quota-Remaining"] = str(rem) if rem is not None else "unlimited"
        response.headers["X-Actionable-Returned"] = "1" if _is_actionable(latest_signal.action) else "0"

    return latest_signal

# ---------------- Trade Endpoints ----------------
@app.post("/trades", response_model=TradeRecordOut)
def post_trade(trade: TradeRecordCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    trade.user_id = user.id
    trade.timestamp = datetime.utcnow()
    return crud.create_trade_record(db, trade)

@app.get("/trades", response_model=List[TradeRecordOut])
def get_all_trades(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return db.query(models.TradeRecord).order_by(models.TradeRecord.id.desc()).all()

# ---------------- Debug Endpoints ----------------
@app.get("/debug_headers")
async def debug_headers(request: Request):
    return dict(request.headers)

@app.get("/debug_cookies")
async def debug_cookies(request: Request):
    return dict(request.cookies)

@app.get("/me")
def me(user=Depends(get_current_user)):
    return {
        "username": user.username,
        "email": user.email,
        "plan": user.plan,
        "daily_quota": user.daily_quota,
        "used_today": user.used_today,
        "expires_at": user.expires_at,
        "is_active": user.is_active
    }

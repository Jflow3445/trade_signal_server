import os
import secrets
from datetime import datetime, timezone
from typing import List
from enum import Enum

from fastapi import FastAPI, Depends, HTTPException, Header, Request, Query, Body
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
        raise HTTPException(status_code=401, detail="Missing API Key")
    user = crud.get_user_by_api_key(db, api_key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API Key")

    # Only apply expiry/active checks for non-farm users here if you want
    # For compatibility with existing callers to /signals/... that pass api_key:
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
        username=req.username,
        plan=req.plan.lower(),
        daily_quota_override=req.daily_quota,
        months_valid=req.months_valid
    )
    return AdminIssueTokenResponse(
        email=user.email,
        username=user.username,
        plan=user.plan,
        api_key=user.api_key,
        daily_quota=user.daily_quota,
        expires_at=user.expires_at
    )

# ---------------- EA Validate (and consume quota) ----------------
@app.post("/auth/validate", response_model=ValidateResponse)
def validate(req: ValidateRequest, db: Session = Depends(get_db)):
    ok, user, reason = crud.validate_and_consume(db, email=req.email, api_key=req.api_key)
    if not ok:
        raise HTTPException(status_code=401, detail=reason or "Unauthorized")

    # remaining calc (None => unlimited)
    remaining = None
    if user.daily_quota is not None:
        remaining = max(0, int(user.daily_quota) - int(user.used_today))

    return ValidateResponse(
        ok=True,
        plan=user.plan or "free",
        remaining_today=remaining,
        expires_at=user.expires_at
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
def get_all_latest_signals(db: Session = Depends(get_db), user=Depends(get_current_user)):
    # consume quota here for non-farm users
    if user.username != "farm_robot":
        ok, _, reason = crud.validate_and_consume(db, email=user.email or user.username, api_key=user.api_key)
        if not ok:
            raise HTTPException(status_code=429, detail=reason or "Rate limited")
    return db.query(models.LatestSignal).order_by(models.LatestSignal.symbol.asc()).all()

@app.get("/signals/{symbol}", response_model=TradeSignalOut)
def get_signal(symbol: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.username != "farm_robot":
        ok, _, reason = crud.validate_and_consume(db, email=user.email or user.username, api_key=user.api_key)
        if not ok:
            raise HTTPException(status_code=429, detail=reason or "Rate limited")
    latest_signal = crud.get_latest_signal(db, symbol)
    if not latest_signal:
        raise HTTPException(status_code=404, detail="No signal for this symbol")
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

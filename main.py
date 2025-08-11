import os
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Header, Request, Query
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

# ---------------- Helpers ----------------
def _resolve_token(authorization: Optional[str], api_key_qs: Optional[str]) -> Optional[str]:
    if api_key_qs:
        return api_key_qs
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return None

# ---------------- Auth Helpers ----------------
def get_current_user(
    authorization: Optional[str] = Header(None),
    api_key: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    token = _resolve_token(authorization, api_key)
    if not token:
        raise HTTPException(status_code=401, detail="Missing API token")
    user = crud.get_user_by_api_key(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API token")

    # Enforce basic checks for non-publisher users
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
        months_valid=req.months,
    )
    return AdminIssueTokenResponse(
        email=user.email,
        username=user.username,
        plan=user.plan,
        token=user.api_key,            # alias
        api_key=user.api_key,          # compatibility
        daily_quota=user.daily_quota,
        expires_at=user.expires_at,
        is_active=user.is_active
    )

# ---------------- EA Validate (optionally consume) ----------------
@app.post("/auth/validate", response_model=ValidateResponse)
def validate(req: ValidateRequest, db: Session = Depends(get_db)):
    if req.consume:
        ok, user, reason = crud.validate_and_consume(db, email=req.email, api_key=req.api_key)
        if not ok:
            raise HTTPException(status_code=401, detail=reason or "Unauthorized")
        remaining = None if user.daily_quota is None else max(0, int(user.daily_quota) - int(user.used_today))
        return ValidateResponse(
            ok=True,
            plan=user.plan or "free",
            daily_quota=user.daily_quota,
            remaining_today=remaining,
            expires_at=user.expires_at,
            is_active=user.is_active
        )
    else:
        ok, user, reason = crud.check_credentials(db, email=req.email, api_key=req.api_key)
        if not ok:
            raise HTTPException(status_code=401, detail=reason or "Unauthorized")
        # compute without consuming
        crud.ensure_daily_reset(user)
        remaining = None if user.daily_quota is None else max(0, int(user.daily_quota) - int(user.used_today))
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
def get_all_latest_signals(db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.username != "farm_robot":
        ok, reason, _rem = crud.consume_for_user(db, user)
        if not ok:
            # use 429 for quota; 403 for inactive/expired already blocked in auth
            code = 429 if reason == "quota_exhausted" else 403
            raise HTTPException(status_code=code, detail=f"Access blocked: {reason}")
    return db.query(models.LatestSignal).order_by(models.LatestSignal.symbol.asc()).all()

@app.get("/signals/{symbol}", response_model=TradeSignalOut)
def get_signal(symbol: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.username != "farm_robot":
        ok, reason, _rem = crud.consume_for_user(db, user)
        if not ok:
            code = 429 if reason == "quota_exhausted" else 403
            raise HTTPException(status_code=code, detail=f"Access blocked: {reason}")
    latest_signal = crud.get_latest_signal(db, symbol)
    if not latest_signal:
        raise HTTPException(status_code=404, detail="No signal for this symbol")
    return latest_signal

# ---------------- Trade Endpoints ----------------
@app.post("/trades", response_model=TradeRecordOut)
def post_trade(trade: TradeRecordCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    # (optional) attach server-side timestamp; your CRUD stores open/close in record details
    trade.user_id = user.id
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

import os
import hmac
import logging
from datetime import datetime, timezone
from typing import List, Set, Dict, Any, Optional
from collections.abc import Generator  # <-- for get_db type
from fastapi import FastAPI, Depends, HTTPException, Header, Request, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy.orm import Session
from database import SessionLocal, engine, Base
import models
import crud
from schemas import (
    TradeSignalCreate, TradeSignalOut, LatestSignalOut,
    TradeRecordCreate, TradeRecordOut,
    AdminIssueTokenRequest, AdminIssueTokenResponse,
    ValidateRequest, ValidateResponse,
    EASyncRequest, EASyncResponse,
    ActivationsList,
)

# ----------------
# App & CORS
# ----------------
app = FastAPI(title="Trade Signals API", version="1.0.0")

origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins, allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

# ----------------
# DB dependency
# ----------------
def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ----------------
# Middleware: simple request logging
# ----------------
class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            resp = await call_next(request)
            return resp
        except Exception as e:
            logging.exception("Unhandled error")
            raise

app.add_middleware(LoggingMiddleware)

# ----------------
# Startup - create tables
# ----------------
@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)

# ----------------
# Health
# ----------------
@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

# ----------------
# Utility: extract bearer token
# ----------------
def require_bearer(auth: Optional[str]) -> str:
    if not auth or not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    return auth.split(" ", 1)[1].strip()

# ----------------
# Plan coercion:
# Only "silver" or "gold" are considered paid plans.
# Any other incoming plan string (including pending_* or typos) becomes "free".
# ----------------
def _coerce_plan(raw: Optional[str]) -> str:
    p = (raw or "").strip().lower()
    if p in ("silver", "gold"):
        return p
    return "free"

# ----------------
# POST /signals  (sender posts)
# ----------------
@app.post("/signals", response_model=TradeSignalOut)
def post_signal(
    payload: TradeSignalCreate,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    token = require_bearer(authorization)
    sender = crud.user_by_api_token(db, token)
    if not sender or not sender.is_active:
        raise HTTPException(status_code=401, detail="Invalid sender token")

    sig = crud.create_signal(
        db=db,
        sender=sender,
        symbol=payload.symbol,
        action=payload.action,
        sl_pips=payload.sl_pips,
        tp_pips=payload.tp_pips,
        lot_size=payload.lot_size,
        details=payload.details
    )
    return sig

# ----------------
# GET /signals  (receiver pulls)
# ----------------
@app.get("/signals", response_model=List[TradeSignalOut])
def get_signals(
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    response: Response = None,
    limit: int = Query(50, ge=1, le=200)
):
    token = require_bearer(authorization)
    try:
        receiver, signals, meta = crud.latest_signals_for_token(db, token, limit=limit)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid or inactive API token")

    # Expose meta in headers
    if response is not None and isinstance(meta, dict):
        if meta.get("unlimited", False):
            response.headers["X-Plan"] = "gold"
            response.headers["X-Quota-Daily"] = "unlimited"
            response.headers["X-Quota-Remaining"] = "unlimited"
        else:
            response.headers["X-Plan"] = str(meta.get("plan"))
            response.headers["X-Quota-Daily"] = str(meta.get("daily_quota"))
            response.headers["X-Quota-Remaining"] = str(meta.get("remaining"))
        if "used_today" in meta:
            response.headers["X-Used-Today"] = str(meta["used_today"])
        if "token_used_today" in meta:
            response.headers["X-Token-Used-Today"] = str(meta["token_used_today"])

    return signals

# ----------------
# POST /records  (receiver posts trade execution, optional)
# ----------------
@app.post("/records", response_model=TradeRecordOut)
def post_record(
    payload: TradeRecordCreate,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    token = require_bearer(authorization)
    receiver = crud.user_by_api_token(db, token)
    if not receiver or not receiver.is_active:
        raise HTTPException(status_code=401, detail="Invalid token")

    rec = crud.create_trade_record(db, receiver, action=payload.action, symbol=payload.symbol, details=payload.details)
    return rec

# ----------------
# POST /validate (email + api_key)
# ----------------
@app.post("/validate", response_model=ValidateResponse)
def validate(payload: ValidateRequest, db: Session = Depends(get_db)):
    ok, user, info = crud.validate_email_token(db, payload.email, payload.api_key)
    if not ok:
        return {
            "ok": False,
            "is_active": False,
            "plan": None,
            "daily_quota": None,
            "expires_at": None
        }
    return {
        "ok": True,
        "is_active": True,
        "plan": info.get("plan"),
        "daily_quota": info.get("daily_quota"),
        "expires_at": info.get("expires_at")
    }

# ----------------
# Admin: issue/upgrade a token to a plan (helper)
# NOTE: plan is coerced so only "silver"/"gold" are honored; everything else => "free"
# ----------------
@app.post("/admin/issue_token", response_model=AdminIssueTokenResponse)
def admin_issue_token(
    payload: AdminIssueTokenRequest,
    db: Session = Depends(get_db),
    x_admin_secret: Optional[str] = Header(None, alias="X-Admin-Secret")
):
    # very simple guard
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or x_admin_secret != admin_secret:
        raise HTTPException(status_code=403, detail="Forbidden")

    # enforce strict plan handling
    safe_plan = _coerce_plan(payload.plan)

    try:
        user, tok = crud.admin_issue_token(db, payload.username_or_email, payload.token, safe_plan)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {
        "username": user.username,
        "email": user.email,
        "token": tok.token,
        "plan": tok.plan,        # this will reflect the coerced plan stored
        "is_active": tok.is_active
    }

# ----------------
# EA sync (optional endpoint your EA might call after login)
# ----------------
@app.post("/ea_sync", response_model=EASyncResponse)
def ea_sync(payload: EASyncRequest, db: Session = Depends(get_db)):
    ok, user, info = crud.validate_email_token(db, payload.email, payload.api_key)
    return {
        "ok": ok,
        "plan": info.get("plan") if ok else None,
        "daily_quota": info.get("daily_quota") if ok else None,
        "unlimited": info.get("unlimited") if ok else None
    }

# ----------------
# Activations list (optional)
# ----------------
@app.get("/activations", response_model=ActivationsList)
def activations(db: Session = Depends(get_db)):
    items = []
    for u in db.query(models.User).filter(models.User.is_active == True).all():
        items.append({
            "username": u.username,
            "email": u.email,
            "plan": u.plan
        })
    return {"items": items}

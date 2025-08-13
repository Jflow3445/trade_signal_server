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

# ---------------- App / DB init ----------------
Base.metadata.create_all(bind=engine)  # Use Alembic for real migrations in prod
app = FastAPI(title="Nister Trade Server", version="2.0.1")

# ---------------- Logging ----------------
logger = logging.getLogger("trade_server")
logger.setLevel(logging.INFO)
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(h)

# ---------------- Middleware ----------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],   # deny by default
    allow_methods=[],
    allow_headers=[],
    allow_credentials=False,
)

class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = "geolocation=()"
        return resp
app.add_middleware(SecurityHeaders)

# ---------------- DI: DB session ----------------
def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------------- Auth helpers ----------------
ADMIN_KEY_ENV = "ADMIN_KEY"
ACTIONABLE: Set[str] = {
    "buy","sell","adjust_sl","adjust_tp","close","close_all","hold","do_nothing"
}

def get_api_token(authorization: Optional[str] = Header(None)) -> str:
    token: Optional[str] = None
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing API token")
    return token

def require_admin(x_admin_key: str = Header(...)) -> None:
    want = os.getenv(ADMIN_KEY_ENV)
    if not want:
        raise HTTPException(status_code=500, detail="admin_key_not_configured")
    if not hmac.compare_digest(x_admin_key, want):
        raise HTTPException(status_code=403, detail="Forbidden")

# ---------------- User context helpers ----------------
def _effective_plan_and_quota(db: Session, user: models.User) -> Dict[str, Any]:
    forced_free = (not user.is_active) or (user.expires_at and datetime.now(timezone.utc) >= user.expires_at)
    base_plan = ("free" if forced_free else (user.plan or "free")).lower()
    boost = crud.get_active_referral_boost(db, user.id)
    if boost:
        if base_plan == "free":
            eff_plan = "silver"
        elif base_plan == "silver":
            eff_plan = "gold"
        else:
            eff_plan = "gold"
    else:
        eff_plan = base_plan

    if user.daily_quota is not None:
        quota = user.daily_quota
    else:
        quota = crud.plan_defaults(eff_plan).get("daily_quota")

    return {"plan": eff_plan, "daily_quota": quota, "expires_at": user.expires_at, "is_active": user.is_active}

# ---------------- Routes ----------------
@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"ok": "true"}

# ---- Signals ----
@app.post("/signals", response_model=TradeSignalOut)
def post_signal(payload: TradeSignalCreate, token: str = Depends(get_api_token), db: Session = Depends(get_db)):
    user = crud.user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.action not in ACTIONABLE:
        raise HTTPException(status_code=400, detail="Unsupported action")
    sig = crud.create_signal(db, user_id=user.id, s=payload)
    return sig

@app.get("/signals", response_model=List[TradeSignalOut])
def get_signals(
    limit: int = Query(100, ge=1, le=1000),
    token: str = Depends(get_api_token),
    db: Session = Depends(get_db),
    resp: Response = None,
):
    user = crud.user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")

    eff = _effective_plan_and_quota(db, user)
    quota = eff["daily_quota"]  # None means unlimited
    if quota is None:
        rows = crud.list_signals(db, user.id, limit=limit)
        if resp:
            resp.headers["X-Quota-Limit"] = "unlimited"
            resp.headers["X-Quota-Remaining"] = "unlimited"
        return rows

    used = crud.count_signals_created_today(db, user.id)
    remaining = max(quota - used, 0)
    if remaining == 0:
        # hard stop once quota is consumed
        raise HTTPException(status_code=429, detail="quota_exhausted")

    # cap page size to remaining
    eff_limit = min(limit, remaining)
    rows = crud.list_signals(db, user.id, limit=eff_limit)
    if resp:
        resp.headers["X-Quota-Limit"] = str(quota)
        resp.headers["X-Quota-Remaining"] = str(remaining - len(rows))
    return rows

@app.get("/signals/latest", response_model=List[LatestSignalOut])
def get_latest(
    limit: int = Query(50, ge=1, le=200),
    token: str = Depends(get_api_token),
    db: Session = Depends(get_db),
    resp: Response = None,
):
    user = crud.user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")

    eff = _effective_plan_and_quota(db, user)
    quota = eff["daily_quota"]
    if quota is None:
        rows = crud.list_latest_signals(db, user.id, limit=limit)
        if resp:
            resp.headers["X-Quota-Limit"] = "unlimited"
            resp.headers["X-Quota-Remaining"] = "unlimited"
        return rows

    used = crud.count_signals_created_today(db, user.id)
    remaining = max(quota - used, 0)
    if remaining == 0:
        raise HTTPException(status_code=429, detail="quota_exhausted")

    # latest is just a summary, but still bound by what's left today
    eff_limit = min(limit, remaining)
    rows = crud.list_latest_signals(db, user.id, limit=eff_limit)
    if resp:
        resp.headers["X-Quota-Limit"] = str(quota)
        resp.headers["X-Quota-Remaining"] = str(remaining - len(rows))
    return rows

# ---- Trades ----
@app.post("/trades", response_model=TradeRecordOut)
def post_trade(payload: TradeRecordCreate, token: str = Depends(get_api_token), db: Session = Depends(get_db)):
    user = crud.user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    row = crud.create_trade_record(db, payload, user_id=user.id)
    return row

# ---- Admin: issue/rotate token ----
@app.post("/admin/issue_token", response_model=AdminIssueTokenResponse, dependencies=[Depends(require_admin)])
def admin_issue_token(req: AdminIssueTokenRequest, db: Session = Depends(get_db)):
    try:
        user = crud.issue_or_update_user(
            db,
            email=req.email, username=req.username, plan=req.plan,
            daily_quota_override=req.daily_quota,
            months_valid=req.months_valid,
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
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

# ---- Validate (EA) ----
@app.post("/validate", response_model=ValidateResponse)
def validate(req: ValidateRequest, db: Session = Depends(get_db)):
    user = crud.user_by_email(db, req.email)
    if not user or not hmac.compare_digest(user.api_key, req.api_key):
        return ValidateResponse(ok=False)
    eff = _effective_plan_and_quota(db, user)
    return ValidateResponse(ok=True, plan=eff["plan"], daily_quota=eff["daily_quota"],
                            expires_at=eff["expires_at"], is_active=eff["is_active"])

# ---- EA: sync open positions ----
@app.post("/ea/sync_open_positions", response_model=EASyncResponse)
def ea_sync_positions(payload: EASyncRequest, token: str = Depends(get_api_token), db: Session = Depends(get_db)):
    user = crud.user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    crud.touch_activation(db, user_id=user.id, account_id=payload.account_id,
                          broker_server=payload.broker_server, hwid=None)
    upserted = crud.upsert_open_positions(
        db, user_id=user.id, account_id=payload.account_id, broker_server=payload.broker_server,
        positions=payload.positions
    )
    return EASyncResponse(ok=True, upserted=upserted)

# ---- Optional: admin view activations for a user ----
@app.get("/admin/activations/{username}", response_model=ActivationsList, dependencies=[Depends(require_admin)])
def admin_list_activations(username: str, db: Session = Depends(get_db)):
    u = db.query(models.User).filter(models.User.username == username.lower()).one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="user_not_found")
    items = db.query(models.Activation).filter(models.Activation.user_id == u.id)\
            .order_by(models.Activation.last_seen_at.desc()).all()
    return ActivationsList(
        email=u.email, plan=u.plan,
        used=len(items), limit=None,
        items=items
    )

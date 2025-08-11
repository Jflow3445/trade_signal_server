import os
import json
from datetime import datetime, timezone
from typing import List

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
    ActivationsList,
    ActivationOut,
    EAOpenPosition,
    EASyncRequest,
    EASyncResponse,
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
def get_api_token(
    authorization: str = Header(None),
    api_key: str = Query(None)
) -> str:
    token = None

    # Header first
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()

    # Fallback to query
    if not token and api_key:
        token = api_key.strip()

    # Strip <...> if user pasted with angle brackets
    if token and token.startswith("<") and token.endswith(">"):
        token = token[1:-1].strip()

    if not token:
        raise HTTPException(status_code=401, detail="Missing API token")
    return token

def get_current_user(token: str = Depends(get_api_token), db: Session = Depends(get_db)):
    user = crud.get_user_by_api_key(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API token")

    # Keep your existing non-farm checks
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
        months_valid=(req.months_valid if req.months_valid is not None else getattr(req, "months", None))
    )
    return AdminIssueTokenResponse(
        email=user.email,
        username=user.username,
        plan=user.plan,
        token=user.api_key,
        api_key=user.api_key,
        daily_quota=user.daily_quota,
        expires_at=user.expires_at,
        is_active=user.is_active
    )

# (Optional) Admin view activations for a user
@app.get("/admin/activations", response_model=ActivationsList)
def admin_activations(
    email: str,
    db: Session = Depends(get_db),
    admin_ok: bool = Depends(require_admin)
):
    user = crud.get_user_by_email(db, email)
    if not user:
        raise HTTPException(status_code=404, detail="No such user")
    items = crud.list_activations(db, user.id)
    return ActivationsList(
        email=email,
        plan=user.plan or "free",
        used=len(items),
        limit=crud.plan_activation_limit(user.plan),
        items=[ActivationOut.from_orm(a) for a in items]
    )

# ---------------- EA Validate (and consume quota) ----------------
@app.post("/auth/validate", response_model=ValidateResponse)
def validate(req: ValidateRequest, db: Session = Depends(get_db)):
    # reuse the /signals flow logic: only counts actionable
    user = crud.get_user_by_email(db, req.email)
    if not user or user.api_key != req.api_key:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if user.username != "farm_robot":
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account inactive")
        now = datetime.now(timezone.utc)
        if user.expires_at and user.expires_at <= now:
            raise HTTPException(status_code=401, detail="Token expired")

    remaining = crud.get_remaining_today(user)
    return ValidateResponse(
        ok=True,
        plan=user.plan or "free",
        daily_quota=user.daily_quota,
        remaining_today=remaining if remaining is not None else None,
        expires_at=user.expires_at,
        is_active=user.is_active,
    )

# ---------------- Parse activation headers ----------------
def read_activation_headers(
    account_id: str = Header(None, alias="X-Account-Id"),
    broker_server: str = Header(None, alias="X-Broker-Server"),
    hwid: str = Header(None, alias="X-Hwid")
):
    return account_id, broker_server, hwid

# ---------------- EA: sync open positions ----------------
@app.post("/ea/positions/sync", response_model=EASyncResponse)
def sync_positions(
    payload: EASyncRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    act_hdrs=Depends(read_activation_headers)
):
    if user.username == "farm_robot":
        raise HTTPException(status_code=403, detail="farm_account_cannot_sync")

    account_id, broker_server, hwid = act_hdrs
    if not account_id or not broker_server:
        raise HTTPException(status_code=400, detail="Missing activation headers")

    received = len(payload.positions)
    upserted = 0
    keep = set()

    for p in payload.positions:
        if not p.ticket or not p.symbol:
            continue
        ok = crud.upsert_open_position(
            db, user.id, str(account_id), str(broker_server), str(hwid) if hwid else None, p
        )
        if ok:
            upserted += 1
        keep.add(p.ticket)

    pruned = crud.prune_open_positions(db, user.id, str(account_id), str(broker_server), keep)
    return EASyncResponse(received=received, upserted=upserted, pruned=pruned)

# ---------------- Helpers ----------------
ACTIONABLE = {"buy", "sell"}

def _coerce_details(obj):
    """Avoid 500s from Pydantic when details is a JSON string in DB."""
    try:
        if isinstance(obj.details, str):
            obj.details = json.loads(obj.details)
    except Exception:
        # leave it as-is; not fatal
        pass

# ---------------- Signal Endpoints ----------------
@app.post("/signals", response_model=TradeSignalOut)
def post_signal(signal: TradeSignalCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.username != "farm_robot":
        raise HTTPException(status_code=403, detail="Not authorized to POST signals")
    signal.user_id = user.id
    created_signal = crud.create_signal(db, signal)
    crud.upsert_latest_signal(db, signal)
    _coerce_details(created_signal)
    return created_signal

@app.get("/signals", response_model=List[LatestSignalOut])
def get_all_latest_signals(
    response: Response,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    act_hdrs=Depends(read_activation_headers)
):
    account_id, broker_server, hwid = act_hdrs

    # Activation enforcement for non-farm users
    if user.username != "farm_robot":
        if not account_id or not broker_server:
            raise HTTPException(status_code=400, detail="Missing activation headers")
        ok, used, limit = crud.ensure_activation(db, user, str(account_id), str(broker_server), str(hwid) if hwid else None)
        if not ok:
            raise HTTPException(status_code=403, detail="activation_limit")

    # Fetch and partition
    all_signals = db.query(models.LatestSignal).order_by(models.LatestSignal.symbol.asc()).all()
    for s in all_signals:
        _coerce_details(s)

    actionable = [s for s in all_signals if (s.action or "").lower() in ACTIONABLE]
    maintenance = [s for s in all_signals if (s.action or "").lower() not in ACTIONABLE]

    actionable_count = len(actionable)
    remaining = crud.get_remaining_today(user)

    # Determine open symbols for this user/account (for maintenance filtering)
    open_symbols = set()
    if user.username != "farm_robot" and account_id and broker_server:
        open_symbols = crud.get_open_symbols(db, user.id, str(account_id), str(broker_server))

    # Decide delivery + consume
    deliver_actionable = actionable
    to_consume = actionable_count

    if user.username != "farm_robot":
        if remaining is not None:
            if remaining <= 0:
                deliver_actionable = []
                to_consume = 0
            elif remaining < actionable_count:
                deliver_actionable = actionable[:remaining]
                to_consume = remaining

        # consume only how many actionable we return
        if to_consume > 0:
            if not crud.consume_n(db, user, to_consume):
                deliver_actionable = []
                to_consume = 0

    # Always filter maintenance to only the user's open symbols
    filtered_maintenance = [m for m in maintenance if (m.symbol or "").upper() in open_symbols] if open_symbols else maintenance

    out = deliver_actionable + filtered_maintenance

    # headers
    response.headers["X-Quota-Daily"] = str(user.daily_quota) if user.daily_quota is not None else "unlimited"
    rem_after = crud.get_remaining_today(user)
    response.headers["X-Quota-Remaining"] = str(rem_after) if rem_after is not None else "unlimited"
    response.headers["X-Actionable-Returned"] = str(len(deliver_actionable))
    if user.username != "farm_robot":
        response.headers["X-Activations-Used"] = str(crud.count_activations(db, user.id))
        lim = crud.plan_activation_limit(user.plan)
        response.headers["X-Activations-Limit"] = str(lim) if lim is not None else "unlimited"

    return out

@app.get("/signals/{symbol}", response_model=TradeSignalOut)
def get_signal(
    symbol: str,
    response: Response,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    act_hdrs=Depends(read_activation_headers)
):
    account_id, broker_server, hwid = act_hdrs

    if user.username != "farm_robot":
        if not account_id or not broker_server:
            raise HTTPException(status_code=400, detail="Missing activation headers")
        ok, used, limit = crud.ensure_activation(db, user, str(account_id), str(broker_server), str(hwid) if hwid else None)
        if not ok:
            raise HTTPException(status_code=403, detail="activation_limit")

    latest_signal = crud.get_latest_signal(db, symbol)
    if not latest_signal:
        raise HTTPException(status_code=404, detail="No signal for this symbol")

    _coerce_details(latest_signal)

    # Count/consume only if actionable
    is_actionable = (latest_signal.action or "").lower() in ACTIONABLE
    if user.username != "farm_robot" and is_actionable:
        remaining = crud.get_remaining_today(user)
        if remaining is not None and remaining <= 0:
            raise HTTPException(status_code=429, detail="Access blocked: quota_exhausted")
        if not crud.consume_n(db, user, 1):
            raise HTTPException(status_code=429, detail="Access blocked: quota_exhausted")

    # headers
    response.headers["X-Quota-Daily"] = str(user.daily_quota) if user.daily_quota is not None else "unlimited"
    rem_after = crud.get_remaining_today(user)
    response.headers["X-Quota-Remaining"] = str(rem_after) if rem_after is not None else "unlimited"
    response.headers["X-Actionable-Returned"] = "1" if is_actionable else "0"
    if user.username != "farm_robot":
        response.headers["X-Activations-Used"] = str(crud.count_activations(db, user.id))
        lim = crud.plan_activation_limit(user.plan)
        response.headers["X-Activations-Limit"] = str(lim) if lim is not None else "unlimited"

    return latest_signal

# ---------------- Trade Endpoints ----------------
@app.post("/trades", response_model=TradeRecordOut)
def post_trade(trade: TradeRecordCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    trade.user_id = user.id
    trade.timestamp = datetime.utcnow()
    created = crud.create_trade_record(db, trade)
    return created

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

@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": datetime.utcnow().isoformat()}

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

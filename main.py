# main.py
import os
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Set, Optional, Tuple

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
    # New (must exist in your schemas.py)
    OpenPositionIn,
    SyncOpenPositionsRequest,
    SyncOpenPositionsResponse,
)

# ---------------- App & DB ----------------
models.Base.metadata.create_all(bind=engine)
app = FastAPI()

logger = logging.getLogger("trade_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

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
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
    if not token and api_key:
        token = api_key.strip()
    if token and token.startswith("<") and token.endswith(">"):
        token = token[1:-1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing API token")
    return token

def get_current_user(token: str = Depends(get_api_token), db: Session = Depends(get_db)):
    user = crud.get_user_by_api_key(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API token")

    # farm robot bypasses normal checks
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

# ---------------- Activation Headers ----------------
def read_activation_headers(
    account_id: Optional[str] = Header(None, alias="X-Account-Id"),
    broker_server: Optional[str] = Header(None, alias="X-Broker-Server"),
    hwid: Optional[str] = Header(None, alias="X-Hwid"),
) -> Tuple[str, str, Optional[str]]:
    # normalize & validate here (avoid blowing up later)
    aid = (account_id or "").strip()
    bsv = (broker_server or "").strip()
    hw  = (hwid or None)
    if not aid or not bsv:
        raise HTTPException(status_code=400, detail="Missing activation headers")
    return aid, bsv, (hw.strip() if isinstance(hw, str) and hw.strip() else None)

# ---------------- In-memory open positions snapshot ----------------
# We keep a brief cache so quota-exhausted users still get modify signals
# for ONLY the symbols they actually have open.
OPEN_POS: Dict[int, Dict[str, object]] = {}  # user_id -> {"symbols": set[str], "ts": datetime, "raw": list}
OPEN_POS_TTL = timedelta(minutes=7)

def set_open_positions(user_id: int, symbols: Set[str], raw_list: List[dict]):
    OPEN_POS[user_id] = {
        "symbols": set(s.upper() for s in symbols),
        "ts": datetime.now(timezone.utc),
        "raw": raw_list,
    }

def get_open_symbols(user_id: int) -> Set[str]:
    blob = OPEN_POS.get(user_id)
    if not blob:
        return set()
    ts = blob.get("ts")
    if not isinstance(ts, datetime) or (datetime.now(timezone.utc) - ts) > OPEN_POS_TTL:
        # stale
        OPEN_POS.pop(user_id, None)
        return set()
    return set(blob.get("symbols", set()))

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

# ---------------- Accept client open positions ----------------
@app.post("/client/open_positions", response_model=SyncOpenPositionsResponse)
def sync_open_positions(
    payload: SyncOpenPositionsRequest,
    user=Depends(get_current_user),
):
    """
    EA posts a snapshot of currently open positions. We'll keep
    a short-lived in-memory view keyed by user. This lets us serve
    modify/maintenance signals for *just those symbols* even when
    actionable quota is exhausted.
    """
    try:
        positions = payload.positions or []
        syms = { (p.symbol or "").upper().strip() for p in positions if (p.symbol or "").strip() }
        set_open_positions(user.id, syms, [p.dict() for p in positions])
        return SyncOpenPositionsResponse(accepted=len(positions), tracked=list(syms))
    except Exception as e:
        err_id = str(uuid.uuid4())[:8]
        logger.exception("open_positions error [%s] user=%s", err_id, getattr(user, "id", "?"))
        # 400 because it's likely a payload issue if we get here
        raise HTTPException(status_code=400, detail=f"bad_open_positions:{err_id}")

# ---------------- Signal Endpoints ----------------
ACTIONABLE = {"buy", "sell"}

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
    user=Depends(get_current_user),
    act_hdrs=Depends(read_activation_headers)
):
    try:
        # Activation enforcement for non-farm users
        if user.username != "farm_robot":
            account_id, broker_server, hwid = act_hdrs
            ok, used, limit = crud.ensure_activation(db, user, str(account_id), str(broker_server), str(hwid) if hwid else None)
            if not ok:
                raise HTTPException(status_code=403, detail="activation_limit")

        # Fetch cache of latest signals
        all_signals = db.query(models.LatestSignal).order_by(models.LatestSignal.symbol.asc()).all()
        actionable = [s for s in all_signals if (s.action or "").lower() in ACTIONABLE]
        maintenance = [s for s in all_signals if (s.action or "").lower() not in ACTIONABLE]

        actionable_count = len(actionable)
        remaining = crud.get_remaining_today(user)
        if remaining is not None and remaining < 0:
            remaining = 0

        deliver_actionable: List[models.LatestSignal] = actionable
        to_consume = actionable_count

        deliver_maintenance = maintenance

        if user.username != "farm_robot":
            if remaining is not None:
                if remaining <= 0:
                    # No actionable when quota is out
                    deliver_actionable = []
                    to_consume = 0
                    # Filter maintenance to only user's open symbols (if we have a snapshot)
                    open_syms = get_open_symbols(user.id)
                    if open_syms:
                        deliver_maintenance = [m for m in maintenance if (m.symbol or "").upper() in open_syms]
                    else:
                        deliver_maintenance = []
                elif remaining < actionable_count:
                    deliver_actionable = actionable[:remaining]
                    to_consume = remaining

            # consume only how many actionable we return
            if to_consume > 0:
                if not crud.consume_n(db, user, to_consume):
                    # Safety: if another concurrent request consumed quota, fallback to maintenance filtering only
                    deliver_actionable = []
                    to_consume = 0
                    open_syms = get_open_symbols(user.id)
                    deliver_maintenance = [m for m in maintenance if (m.symbol or "").upper() in open_syms]

        out = deliver_actionable + deliver_maintenance

        # headers
        response.headers["X-Quota-Daily"] = str(user.daily_quota) if user.daily_quota is not None else "unlimited"
        rem_after = crud.get_remaining_today(user)
        response.headers["X-Quota-Remaining"] = str(rem_after) if rem_after is not None else "unlimited"
        response.headers["X-Actionable-Returned"] = str(len(deliver_actionable))
        if user.username != "farm_robot":
            response.headers["X-Activations-Used"] = str(crud.count_activations(db, user.id))
            lim = crud.plan_activation_limit(user.plan)
            response.headers["X-Activations-Limit"] = str(lim) if lim is not None else "unlimited"
            response.headers["X-OpenSymbols"] = ",".join(sorted(get_open_symbols(user.id))) or ""

        return out

    except HTTPException:
        raise
    except Exception as e:
        err_id = str(uuid.uuid4())[:8]
        logger.exception("signals GET error [%s] user=%s", err_id, getattr(user, "id", "?"))
        # return a JSON 500 instead of plain text
        raise HTTPException(status_code=500, detail=f"server_error:{err_id}")

@app.get("/signals/{symbol}", response_model=TradeSignalOut)
def get_signal(
    symbol: str,
    response: Response,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    act_hdrs=Depends(read_activation_headers)
):
    try:
        if user.username != "farm_robot":
            account_id, broker_server, hwid = act_hdrs
            ok, used, limit = crud.ensure_activation(db, user, str(account_id), str(broker_server), str(hwid) if hwid else None)
            if not ok:
                raise HTTPException(status_code=403, detail="activation_limit")

        latest_signal = crud.get_latest_signal(db, symbol)
        if not latest_signal:
            raise HTTPException(status_code=404, detail="No signal for this symbol")

        is_actionable = (latest_signal.action or "").lower() in ACTIONABLE

        if user.username != "farm_robot" and is_actionable:
            remaining = crud.get_remaining_today(user)
            if remaining is not None and remaining <= 0:
                # allow maintenance always; actionable blocked at 0
                raise HTTPException(status_code=429, detail="Access blocked: quota_exhausted")
            if remaining is not None:
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

    except HTTPException:
        raise
    except Exception as e:
        err_id = str(uuid.uuid4())[:8]
        logger.exception("signals/{symbol} error [%s] user=%s sym=%s", err_id, getattr(user, "id", "?"), symbol)
        raise HTTPException(status_code=500, detail=f"server_error:{err_id}")

# ---------------- Trade Endpoints ----------------
@app.post("/trades", response_model=TradeRecordOut)
def post_trade(trade: TradeRecordCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    trade.user_id = user.id
    trade.timestamp = datetime.utcnow()
    return crud.create_trade_record(db, trade)

@app.get("/trades", response_model=List[TradeRecordOut])
def get_all_trades(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return db.query(models.TradeRecord).order_by(models.TradeRecord.id.desc()).all()

# ---------------- Debug / Health ----------------
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

@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}

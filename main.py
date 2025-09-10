import os
import json
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Depends, HTTPException, Header, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from database import SessionLocal, engine, Base
import models
import crud
from schemas import (
    TradeSignalCreate, TradeSignalOut, LatestSignalOut,
    TradeRecordCreate, TradeRecordOut,
    PlanChangeIn, PlanChangeOut, VerifyOut, ActivationsList,
    AdminIssueTokenRequest, AdminIssueTokenResponse, UserOut
)

APP_NAME = "Nister Trade Server"

app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=3600,
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)

@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

# ---------------- Auth helpers ----------------
def _coerce_plan(p: Optional[str]) -> str:
    return crud.normalize_plan(p)

def _require_admin_bearer(authorization: Optional[str]) -> None:
    admin = os.getenv("ADMIN_TOKEN") or os.getenv("ADMIN_SECRET")
    if not admin or not authorization or authorization != f"Bearer {admin}":
        raise HTTPException(status_code=401, detail="Unauthorized")

def _verify_webhook(header_sig: Optional[str], payload: bytes) -> None:
    secret = os.getenv("WEBHOOK_SECRET")
    if not secret:
        return
    if not header_sig:
        raise HTTPException(status_code=401, detail="Missing signature")
    mac = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, header_sig):
        raise HTTPException(status_code=401, detail="Bad signature")

# ---------------- Public: verify token ----------------
@app.get("/auth/verify", response_model=VerifyOut)
def verify_token(authorization: Optional[str] = Header(None, alias="Authorization"), db: Session = Depends(get_db)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization")
    token = authorization.split(" ", 1)[1].strip()
    ok, user, meta = crud.verify_token(db, token)
    if not ok or not user:
        raise HTTPException(status_code=401, detail="Invalid or inactive token")
    return {"ok": True, "plan": meta.get("plan"), "daily_quota": meta.get("daily_quota"), "unlimited": meta.get("unlimited")}

# ---------------- Webhook: plan change from WP ----------------
@app.post("/webhook/payment-approved", response_model=PlanChangeOut)
async def webhook_payment_approved(
    request: Request,
    x_signature: Optional[str] = Header(None, alias="X-Signature"),
    db: Session = Depends(get_db),
):
    payload = await request.body()
    # Try HMAC
    try:
        _verify_webhook(x_signature, payload)
    except HTTPException:
        # Legacy secret in body/query
        data_probe: Dict[str, Any] = {}
        try:
            data_probe = json.loads(payload or b"{}")
        except Exception:
            pass
        legacy_secret = (data_probe.get("secret") if isinstance(data_probe, dict) else None) or request.query_params.get("secret")
        if not legacy_secret or legacy_secret != os.getenv("WEBHOOK_SECRET"):
            raise

    # Parse JSON or form/query
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    data: Dict[str, Any] = {}
    if content_type == "application/json":
        try:
            data = json.loads(payload or b"{}")
        except Exception:
            data = {}
    else:
        data = dict(request.query_params)
        # naive body parse for x-www-form-urlencoded without starlette's parser
        if not data and payload:
            try:
                kv = payload.decode()
                for pair in kv.split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        data.setdefault(k, v)
            except Exception:
                pass

    user_id = int(data.get("user_id") or 0) or None
    username = data.get("username")
    email = data.get("email")
    plan_raw = data.get("plan") or data.get("tier") or data.get("subscription") or "free"
    rotate = str(data.get("rotate", "false")).lower() in ("1", "true", "yes")

    plan = _coerce_plan(plan_raw)  # pending_* or junk -> free

    user = crud.ensure_user(db, user_id, username, email)
    token_obj, rotated = crud.upsert_active_token(db, user, plan=plan, rotate=rotate)
    limits = crud.plan_limits(token_obj.plan)

    return {"ok": True, "user": user, "plan": token_obj.plan, "rotated": rotated, "api_key": token_obj.token, **limits}

# ---------------- Admin: change plan (keep or rotate key) ----------------
@app.post("/admin/plan", response_model=PlanChangeOut)
def admin_change_plan(
    payload: PlanChangeIn,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    _require_admin_bearer(authorization)
    user = crud.ensure_user(db, payload.user_id, payload.username, payload.email)
    plan = _coerce_plan(payload.plan)
    token_obj, rotated = crud.upsert_active_token(db, user, plan=plan, rotate=bool(payload.rotate))
    limits = crud.plan_limits(token_obj.plan)
    return {"ok": True, "user": user, "plan": token_obj.plan, "rotated": rotated, "api_key": token_obj.token, **limits}

# ---------------- Admin: issue/rotate token (kept) ----------------
@app.post("/admin/issue_token", response_model=AdminIssueTokenResponse)
def admin_issue_token(
    payload: AdminIssueTokenRequest,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    _require_admin_bearer(authorization)
    user = crud.ensure_user(db, payload.user_id, payload.username, payload.email)
    plan = _coerce_plan(payload.plan)
    tok, rotated = crud.upsert_active_token(db, user, plan=plan, rotate=bool(payload.rotate))
    return {"username": user.username, "email": user.email, "plan": tok.plan, "api_key": tok.token, "rotated": rotated}

# ---------------- Signals: publish by sender ----------------
@app.post("/signals/publish", response_model=TradeSignalOut)
def publish_signal(
    payload: TradeSignalCreate,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization")
    token = authorization.split(" ", 1)[1].strip()
    ok, sender, _ = crud.verify_token(db, token)
    if not ok or not sender:
        raise HTTPException(status_code=401, detail="Invalid token")
    sig = crud.create_signal(
        db, sender,
        symbol=payload.symbol, action=payload.action,
        sl_pips=payload.sl_pips, tp_pips=payload.tp_pips,
        lot_size=payload.lot_size, details=payload.details
    )
    return sig

# ---------------- Signals: fetch latest for receiver (quota enforced) ----------------
@app.get("/signals/latest", response_model=LatestSignalOut)
def latest_signals(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization")
    token = authorization.split(" ", 1)[1].strip()
    ok, receiver, meta = crud.verify_token(db, token)
    if not ok or not receiver:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Enforce quotas
    unlimited = bool(meta.get("unlimited"))
    daily_quota = meta.get("daily_quota")
    token_hash = crud.hash_token_for_read(token)

    if not unlimited:
        used = crud.count_reads_today(db, receiver, token_hash=token_hash)
        remaining = max(0, int(daily_quota) - used) if daily_quota is not None else 0
        if remaining <= 0:
            return {"items": []}
        limit = min(limit, remaining)

    signals = crud.get_latest_signals_for_receiver(db, receiver, limit=limit)
    # Record reads
    for s in signals:
        crud.record_signal_read(db, s.id, receiver, token_hash)
    return {"items": signals}

# ---------------- Trades: record (optional) ----------------
@app.post("/trades/record", response_model=TradeRecordOut)
def record_trade(
    payload: TradeRecordCreate,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization")
    token = authorization.split(" ", 1)[1].strip()
    ok, receiver, _ = crud.verify_token(db, token)
    if not ok or not receiver:
        raise HTTPException(status_code=401, detail="Invalid token")
    tr = crud.record_trade(db, receiver, payload.symbol, payload.action, payload.details)
    return tr

# ---------------- Subscriptions (admin helper) ----------------
@app.post("/admin/subscribe")
def admin_subscribe(
    receiver_id: int,
    sender_id: int,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    _require_admin_bearer(authorization)
    r = db.query(models.User).filter(models.User.id == receiver_id).first()
    s = db.query(models.User).filter(models.User.id == sender_id).first()
    if not r or not s:
        raise HTTPException(status_code=404, detail="User not found")
    # upsert
    exists = db.query(models.Subscription).filter(models.Subscription.receiver_id == r.id, models.Subscription.sender_id == s.id).first()
    if not exists:
        db.add(models.Subscription(receiver_id=r.id, sender_id=s.id))
        db.flush()
    return {"ok": True, "receiver_id": r.id, "sender_id": s.id}

# ---------------- Activations list ----------------
@app.get("/activations", response_model=ActivationsList)
def activations(db: Session = Depends(get_db)):
    users = db.query(models.User).filter(models.User.is_active == True).all()
    return {"items": users}

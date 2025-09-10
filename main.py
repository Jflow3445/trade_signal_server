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
import os, logging, requests

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
@app.get("/auth/verify")
def verify_token(
    authorization: str | None = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    try:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing Authorization")

        token = authorization.split(" ", 1)[1].strip()
        ok, user, meta = crud.verify_token(db, token)

        if not ok or not user:
            raise HTTPException(status_code=401, detail="Invalid or inactive token")

        # Normalize output to avoid validation edge cases
        plan = (meta.get("plan") or "free")
        daily_quota = meta.get("daily_quota", None)
        unlimited = bool(meta.get("unlimited"))

        return {"ok": True, "plan": plan, "daily_quota": daily_quota, "unlimited": unlimited}

    except HTTPException:
        raise
    except Exception as e:
        logging.exception("verify_token crashed")
        # Surface real cause instead of empty 500
        raise HTTPException(status_code=500, detail=f"verify_token crash: {e.__class__.__name__}: {e}")
# ---------------- Webhook: plan change from WP ----------------
@app.post("/webhook/payment-approved")
async def webhook_payment_approved(request: Request, db: Session = Depends(get_db)):
    try:
        # tolerant parsing (JSON/form/query)
        body = await request.body()
        ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        data = {}
        if ct == "application/json":
            try:
                data = await request.json()
            except Exception:
                data = {}
        else:
            try:
                form = await request.form()
                data = dict(form)
            except Exception:
                data = {}
        if not data:
            data = dict(request.query_params)

        # identity
        raw_uid = data.get("user_id")
        user_id = None
        if raw_uid not in (None, ""):
            try: user_id = int(raw_uid)
            except: user_id = None
        username = (data.get("username") or data.get("user") or None)
        email    = (data.get("email") or None)
        if not (user_id or username or email):
            raise HTTPException(status_code=400, detail="Need one of user_id, username, or email")

        # plan
        plan = (data.get("plan") or data.get("tier") or data.get("subscription") or "").strip().lower()
        if plan not in ("free", "silver", "gold"):
            plan = "free"

        # ensure user
        user = crud.ensure_user(db, user_id, username, email)

        # Decide rotation: rotate if the effective plan is changing
        # (compute current effective plan from active token if present; else from user.plan)
        active = db.query(models.APIToken).filter(models.APIToken.user_id == user.id, models.APIToken.is_active == True).first()
        current_plan = crud.normalize_plan(active.plan if active else user.plan)
        need_rotate = (crud.normalize_plan(plan) != current_plan)

        tok, rotated = crud.upsert_active_token(db, user, plan=plan, rotate=need_rotate)
        limits = crud.plan_limits(tok.plan)

        db.commit()  # persist rotation and plan update
        return {
            "ok": True,
            "user": {"id": user.id, "username": user.username, "email": user.email, "plan": tok.plan},
            "plan": tok.plan,
            "rotated": rotated,
            "api_key": tok.token,
            **limits
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logging.exception("webhook_payment_approved crashed")
        raise HTTPException(status_code=500, detail=f"webhook crash: {e.__class__.__name__}: {e}")

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

def notify_wordpress(user, token):
    url = os.getenv("WP_CALLBACK_URL")
    key = os.getenv("WP_CALLBACK_KEY")
    if not url or not key:
        return
    try:
        requests.post(
            url,
            json={"username": user.username, "email": user.email, "plan": token.plan, "api_key": token.token},
            headers={"X-Callback-Key": key},
            timeout=5,
        )
    except Exception as e:
        logging.warning("WP callback failed: %s", e)
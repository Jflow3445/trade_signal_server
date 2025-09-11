import json
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Optional, Dict, Any  # +Dict, Any
from fastapi import FastAPI, Depends, HTTPException, Header, Request, Query, Body 
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from database import SessionLocal, engine, Base
import models
import crud
import os, logging, requests

from schemas import (
    TradeSignalCreate, TradeSignalOut, LatestSignalOut,
    TradeRecordCreate, TradeRecordOut,
    PlanChangeIn, PlanChangeOut, ActivationsList,
    AdminIssueTokenRequest, AdminIssueTokenResponse
)

APP_NAME = "Nister Trade Server"
SENDER_USERNAME = os.getenv("SENDER_USERNAME", "farm_robot")

app = FastAPI(title=APP_NAME)

# ---- CORS: restrict via env ----
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o],
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
    # Best-effort purge at boot
    try:
        db = SessionLocal()
        purged = crud.purge_expired_tokens(db)
        if purged:
            db.commit()
    except Exception:
        db.rollback()
        logging.exception("startup purge_expired_tokens failed")
    finally:
        db.close()


@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}


# ---------------- Auth helpers ----------------
def _coerce_plan(p: Optional[str]) -> str:
    return crud.normalize_plan(p)

def _user_can_send(user: models.User) -> bool:
    return (user.username or "").strip().lower() == "farm_robot"


def _require_admin_bearer(authorization: Optional[str]) -> None:
    # Accept any of the envs for backwards compatibility
    admin = os.getenv("ADMIN_TOKEN") or os.getenv("ADMIN_SECRET") or os.getenv("ADMIN_KEY")
    if not admin or not authorization or authorization != f"Bearer {admin}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def _verify_webhook(header_sig: Optional[str], payload: bytes) -> None:
    """
    Verify WP->server webhook:
      - header: X-Webhook-Signature
      - algo: HMAC-SHA256 over the raw JSON body
      - secret: WEBHOOK_SECRET (env)
    """
    secret = os.getenv("WEBHOOK_SECRET")
    if not secret:
        # If not set, reject to avoid unauthenticated plan changes in prod
        raise HTTPException(status_code=401, detail="Webhook secret not configured")
    if not header_sig:
        raise HTTPException(status_code=401, detail="Missing signature")
    mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, header_sig):
        raise HTTPException(status_code=401, detail="Bad signature")


def notify_wordpress(user, token):
    """
    Optional server -> WP callback.
    Env needed on this server:
      WP_CALLBACK_URL=https://nister.org/wp-json/nister/v1/trade-callback
      WP_CALLBACK_KEY=<same hex set as NISTER_TRADE_CALLBACK_KEY in wp-config.php>
    """
    url = os.getenv("WP_CALLBACK_URL")
    key = os.getenv("WP_CALLBACK_KEY")
    if not url or not key:
        return
    try:
        requests.post(
            url,
            json={"username": user.username, "email": user.email, "plan": token.plan, "api_key": token.token},
            headers={"X-Callback-Key": key},
            timeout=3,  # short to avoid tying up request threads
        )
    except Exception as e:
        logging.warning("WP callback failed: %s", e)


# ---------------- Public: verify token ----------------
@app.get("/auth/verify")
def verify_token(
    authorization: str | None = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    # opportunistic purge to keep the table clean
    try:
        purged = crud.purge_expired_tokens(db)
        if purged:
            db.commit()
    except Exception:
        db.rollback()

    try:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing Authorization")

        token = authorization.split(" ", 1)[1].strip()
        ok, user, meta = crud.verify_token(db, token)

        if not ok or not user:
            raise HTTPException(status_code=401, detail="Invalid or inactive token")

        # Normalize output
        plan = (meta.get("plan") or "free")
        daily_quota = meta.get("daily_quota", None)
        unlimited = bool(meta.get("unlimited"))

        # Compute remaining_today WITHOUT consuming quota
        remaining_today = None
        if not unlimited and daily_quota is not None:
            token_hash = crud.hash_token_for_read(token)
            used = crud.count_reads_today(db, user, token_hash=token_hash)
            remaining_today = max(0, int(daily_quota) - used)

        # include expiry (from crud meta) and current server time
        expires_at = meta.get("expires_at")
        server_time = datetime.now(timezone.utc).isoformat()

        return {
            "ok": True,
            "username": user.username,
            "plan": plan,
            "daily_quota": daily_quota,
            "unlimited": unlimited,
            "remaining_today": remaining_today,  # None for unlimited
            "expires_at": expires_at,            # ISO string or None
            "server_time": server_time,          # ISO string
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.exception("verify_token crashed")
        raise HTTPException(status_code=500, detail=f"verify_token crash: {e.__class__.__name__}: {e}")

# ---------------- EA-friendly: validate (email + api_key) ----------------
@app.post("/validate")
async def validate_credentials(
    body: Dict[str, Any] = Body(default={}),
    db: Session = Depends(get_db),
):
    """
    Credential check used by both EAs.
    - Requires api_key
    - If email provided, must match token owner's email (case-insensitive)
    - Refuses expired/inactive tokens
    - Returns quota & expiry info for client UX
    NOTE: Returns 200 with {"ok": false, ...} on failures by default (non-breaking).
          To return HTTP 401 instead, set env VALIDATE_STRICT_401=1.
    """
    # Best-effort purge (non-fatal)
    try:
        purged = crud.purge_expired_tokens(db)
        if purged:
            db.commit()
    except Exception:
        db.rollback()

    email_in = (body or {}).get("email") or ""
    api_key  = (body or {}).get("api_key") or ""

    if not isinstance(email_in, str): email_in = ""
    if not isinstance(api_key, str):  api_key  = ""
    email_norm = email_in.strip().lower()
    api_key    = api_key.strip()

    # Require api_key
    if not api_key:
        if os.getenv("VALIDATE_STRICT_401"):
            raise HTTPException(status_code=401, detail="api_key required")
        return {"ok": False, "error": "api_key required"}

    # Look up live token to enforce expiry and capture expiry time
    now = crud.utc_now()
    tok = db.query(models.APIToken).filter(
        models.APIToken.token == api_key,
        models.APIToken.is_active == True,
        models.APIToken.expires_at > now
    ).first()

    if not tok or not tok.user or not tok.user.is_active:
        if os.getenv("VALIDATE_STRICT_401"):
            raise HTTPException(status_code=401, detail="invalid_or_expired_token")
        return {"ok": False, "error": "invalid_or_expired_token"}

    # If email supplied, bind token to that email
    user_email_norm = (tok.user.email or "").strip().lower()
    if email_norm and user_email_norm and email_norm != user_email_norm:
        if os.getenv("VALIDATE_STRICT_401"):
            raise HTTPException(status_code=401, detail="email_mismatch")
        return {"ok": False, "error": "email_mismatch"}

    limits = crud.plan_limits(tok.plan)
    plan = tok.plan
    daily_quota = limits.get("daily_quota")
    unlimited   = bool(limits.get("unlimited"))

    # Remaining today (computed, not consumed)
    remaining_today = None
    if not unlimited and daily_quota is not None:
        token_hash = crud.hash_token_for_read(api_key)
        used = crud.count_reads_today(db, tok.user, token_hash=token_hash)
        remaining_today = max(0, int(daily_quota) - used)

    return {
        "ok": True,
        "username": tok.user.username,
        "plan": plan,
        "daily_quota": daily_quota,
        "unlimited": unlimited,
        "remaining_today": remaining_today,
        "expires_at": tok.expires_at.isoformat() if tok.expires_at else None,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }

# ---------------- Webhook: plan change from WP ----------------
@app.post("/webhook/payment-approved")
async def webhook_payment_approved(request: Request, db: Session = Depends(get_db)):
    # Authenticate FIRST using HMAC header
    raw = await request.body()
    _verify_webhook(request.headers.get("x-webhook-signature"), raw)

    # Also purge expired tokens before touching state
    try:
        purged = crud.purge_expired_tokens(db)
        if purged:
            db.commit()
    except Exception:
        db.rollback()

    try:
        # tolerant parsing (JSON/form/query)
        ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        data = {}
        if ct == "application/json":
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
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
            try:
                user_id = int(raw_uid)
            except Exception:
                user_id = None
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
        crud.ensure_subscription_to_sender(db, user, os.getenv("DEFAULT_SIGNAL_SENDER", "farm_robot"))
        # rotate iff effective plan changes
        active = db.query(models.APIToken).filter(
            models.APIToken.user_id == user.id,
            models.APIToken.is_active == True
        ).first()
        current_plan = crud.normalize_plan(active.plan if active else user.plan)
        need_rotate = (crud.normalize_plan(plan) != current_plan)

        tok, rotated = crud.upsert_active_token(db, user, plan=plan, rotate=need_rotate)
        limits = crud.plan_limits(tok.plan)

        db.commit()  # persist rotation and plan update

        # notify WP (optional; non-blocking with short timeout)
        notify_wordpress(user, tok)

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
    # purge first
    try:
        purged = crud.purge_expired_tokens(db)
        if purged:
            db.commit()
    except Exception:
        db.rollback()

    try:
        user = crud.ensure_user(db, payload.user_id, payload.username, payload.email)
        crud.ensure_subscription_to_sender(db, user, os.getenv("DEFAULT_SIGNAL_SENDER", "farm_robot"))
        plan = _coerce_plan(payload.plan)
        token_obj, rotated = crud.upsert_active_token(db, user, plan=plan, rotate=bool(payload.rotate))
        limits = crud.plan_limits(token_obj.plan)

        db.commit()

        notify_wordpress(user, token_obj)

        return {"ok": True, "user": user, "plan": token_obj.plan, "rotated": rotated, "api_key": token_obj.token, **limits}
    except:
        db.rollback()
        raise


# ---------------- Admin: issue/rotate token (kept) ----------------
@app.post("/admin/issue_token", response_model=AdminIssueTokenResponse)
def admin_issue_token(
    payload: AdminIssueTokenRequest,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    _require_admin_bearer(authorization)
    # purge first
    try:
        purged = crud.purge_expired_tokens(db)
        if purged:
            db.commit()
    except Exception:
        db.rollback()

    try:
        user = crud.ensure_user(db, payload.user_id, payload.username, payload.email)
        crud.ensure_subscription_to_sender(db, user, os.getenv("DEFAULT_SIGNAL_SENDER", "farm_robot"))
        plan = _coerce_plan(payload.plan)
        tok, rotated = crud.upsert_active_token(db, user, plan=plan, rotate=bool(payload.rotate))

        db.commit()

        notify_wordpress(user, tok)

        return {"username": user.username, "email": user.email, "plan": tok.plan, "api_key": tok.token, "rotated": rotated}
    except:
        db.rollback()
        raise


# ---------------- Signals: publish by sender ----------------
@app.post("/signals/publish", response_model=TradeSignalOut)
def publish_signal(
    payload: TradeSignalCreate,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    # purge
    try:
        purged = crud.purge_expired_tokens(db)
        if purged:
            db.commit()
    except Exception:
        db.rollback()

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization")
    token = authorization.split(" ", 1)[1].strip()
    ok, sender, _ = crud.verify_token(db, token)
    if not ok or not sender:
        raise HTTPException(status_code=401, detail="Invalid token")
    # Only farm_robot is allowed to publish signals
    if not _user_can_send(sender):
        raise HTTPException(status_code=403, detail="Not allowed to publish signals")

    sig = crud.create_signal(
        db, sender,
        symbol=payload.symbol, action=payload.action,
        sl_pips=payload.sl_pips, tp_pips=payload.tp_pips,
        lot_size=payload.lot_size, details=payload.details
    )
    db.commit()  # ensure signal is persisted
    return sig

# Back-compat for sender EA posting to /signals (instead of /signals/publish)
@app.post("/signals")
def publish_signal_compat(
    payload: TradeSignalCreate,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    return publish_signal(payload, authorization, db)

# ---------------- Signals: fetch latest for receiver (quota enforced) ----------------
@app.get("/signals/latest", response_model=LatestSignalOut)
def latest_signals(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    # purge
    try:
        purged = crud.purge_expired_tokens(db)
        if purged:
            db.commit()
    except Exception:
        db.rollback()

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

    db.commit()  # persist read counters
    return {"items": signals}

# Back-compat for receiver EA that expects a top-level array instead of {"items":[...]}
@app.get("/signals")
def latest_signals_array(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    # purge
    try:
        purged = crud.purge_expired_tokens(db)
        if purged:
            db.commit()
    except Exception:
        db.rollback()

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization")
    token = authorization.split(" ", 1)[1].strip()
    ok, receiver, meta = crud.verify_token(db, token)
    if not ok or not receiver:
        raise HTTPException(status_code=401, detail="Invalid token")

    unlimited = bool(meta.get("unlimited"))
    daily_quota = meta.get("daily_quota")
    token_hash = crud.hash_token_for_read(token)

    if not unlimited:
        used = crud.count_reads_today(db, receiver, token_hash=token_hash)
        remaining = max(0, int(daily_quota) - used) if daily_quota is not None else 0
        if remaining <= 0:
            return []  # IMPORTANT: plain array
        limit = min(limit, remaining)

    signals = crud.get_latest_signals_for_receiver(db, receiver, limit=limit)
    for s in signals:
        crud.record_signal_read(db, s.id, receiver, token_hash)

    db.commit()
    return signals

# ---------------- Trades: record (optional) ----------------
@app.post("/trades/record", response_model=TradeRecordOut)
def record_trade(
    payload: TradeRecordCreate,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    # purge
    try:
        purged = crud.purge_expired_tokens(db)
        if purged:
            db.commit()
    except Exception:
        db.rollback()

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization")
    token = authorization.split(" ", 1)[1].strip()
    ok, receiver, _ = crud.verify_token(db, token)
    if not ok or not receiver:
        raise HTTPException(status_code=401, detail="Invalid token")
    tr = crud.record_trade(db, receiver, payload.symbol, payload.action, payload.details)
    db.commit()
    return tr

# Back-compat for sender EA: accepts rich trade record payload
@app.post("/trades")
async def record_trade_compat(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    # Purge (non-fatal)
    try:
        purged = crud.purge_expired_tokens(db)
        if purged:
            db.commit()
    except Exception:
        db.rollback()

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization")

    token = authorization.split(" ", 1)[1].strip()
    ok, user, _ = crud.verify_token(db, token)
    if not ok or not user:
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        data = await request.json()
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    symbol = (data.get("symbol") or "").strip()
    if not symbol:
        # keep non-breaking response
        return {"ok": False, "error": "symbol required"}

    action = (data.get("side") or data.get("action") or "record").strip().lower()
    details = data  # store full payload for auditing

    tr = crud.record_trade(db, user, symbol, action, details)
    db.commit()
    return {"ok": True, "id": tr.id}

# ---------------- Subscriptions (admin helper) ----------------
@app.post("/admin/subscribe")
def admin_subscribe(
    receiver_id: int,
    sender_id: int,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    _require_admin_bearer(authorization)
    # purge
    try:
        purged = crud.purge_expired_tokens(db)
        if purged:
            db.commit()
    except Exception:
        db.rollback()

    try:
        r = db.query(models.User).filter(models.User.id == receiver_id).first()
        s = db.query(models.User).filter(models.User.id == sender_id).first()
        if not r or not s:
            raise HTTPException(status_code=404, detail="User not found")
        # upsert
        exists = db.query(models.Subscription).filter(
            models.Subscription.receiver_id == r.id,
            models.Subscription.sender_id == s.id
        ).first()
        if not exists:
            db.add(models.Subscription(receiver_id=r.id, sender_id=s.id))
            db.flush()

        db.commit()
        return {"ok": True, "receiver_id": r.id, "sender_id": s.id}
    except:
        db.rollback()
        raise


# ---------------- Activations list ----------------
@app.get("/activations", response_model=ActivationsList)
def activations(db: Session = Depends(get_db)):
    users = db.query(models.User).filter(models.User.is_active == True).all()
    return {"items": users}

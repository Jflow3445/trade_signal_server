import os
import datetime
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Header, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from sqlalchemy.orm import Session

from .database import SessionLocal
from . import models, schemas, crud

# ----------------------------
# App & Middleware
# ----------------------------

app = FastAPI(title="Trade Signal Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tune in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# DB Dependency
# ----------------------------

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ----------------------------
# Auth helpers
# ----------------------------

def bearer_token(auth_header: Optional[str]) -> Optional[str]:
    if not auth_header:
        return None
    parts = auth_header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None

def authenticate_by_api_key(db: Session, token: str) -> models.User:
    user = crud.get_user_by_api_key(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API token")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User not active")
    if user.expires_at and user.expires_at < datetime.datetime.utcnow():
        raise HTTPException(status_code=403, detail="Subscription expired")
    return user

# ----------------------------
# Health
# ----------------------------

@app.get("/health")
def health() -> dict:
    return {"ok": True, "time": datetime.datetime.now(datetime.timezone.utc).isoformat()}

# ----------------------------
# Validate
# ----------------------------

class ValidateReq(BaseModel):
    email: str
    api_key: str

@app.post("/validate")
def validate(req: ValidateReq, db: Session = Depends(get_db)) -> dict:
    u = crud.get_user_by_email(db, req.email)
    if not u or u.api_key != req.api_key:
        return {"ok": False}
    return {
        "ok": True,
        "is_active": u.is_active,
        "plan": u.plan,
        "daily_quota": u.daily_quota,
        "expires_at": u.expires_at.isoformat() if u.expires_at else None,
    }

# ----------------------------
# Signals
# ----------------------------

@app.post("/signals", response_model=schemas.Signal)
def post_signal(
    signal: schemas.SignalCreate,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    token = bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    sender = authenticate_by_api_key(db, token)

    # Create signal authored by sender
    s = crud.create_signal(db, sender_id=sender.id, signal=signal)
    return s

@app.get("/signals", response_model=List[schemas.Signal])
def get_signals(
    request: Request,
    response: Response,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    limit: int = 50,
    offset: int = 0,
    since_id: Optional[int] = None,
    symbols: Optional[str] = None,
    actions: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Fetch signals for the authenticated receiver (who follows senders).
    Note: symbols/actions are comma-separated lists in query.
    """
    token = bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    receiver = authenticate_by_api_key(db, token)

    sym_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    act_list = [a.strip().lower() for a in actions.split(",")] if actions else None

    # Apply quota headers for visibility (not enforcement)
    response.headers["X-Plan"] = receiver.plan or ""
    response.headers["X-Daily-Quota"] = str(receiver.daily_quota) if receiver.daily_quota is not None else "unlimited"

    signals = crud.list_signals_for_receiver(
        db, receiver_id=receiver.id, limit=limit, offset=offset, since_id=since_id, symbols=sym_list, actions=act_list
    )
    return signals

# ----------------------------
# Admin (minimal)
# ----------------------------

class UpgradeReq(BaseModel):
    email: str
    plan: str
    daily_quota: Optional[int] = None
    days: Optional[int] = 30

@app.post("/admin/upgrade")
def admin_upgrade(req: UpgradeReq, db: Session = Depends(get_db)):
    u = crud.get_user_by_email(db, req.email)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")

    expires = None
    if req.days and req.days > 0:
        expires = datetime.datetime.utcnow() + datetime.timedelta(days=req.days)

    crud.update_user_plan(db, user=u, plan=req.plan, daily_quota=req.daily_quota, expires_at=expires, is_active=True)
    return {"ok": True, "user": {"email": u.email, "plan": u.plan, "daily_quota": u.daily_quota}}

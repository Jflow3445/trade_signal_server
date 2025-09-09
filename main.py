import os
import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, Depends, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from sqlalchemy.orm import Session

from database import engine, SessionLocal, Base
from models import User, TradeSignal
from schemas import ValidateRequest, ValidateResponse, SignalCreate, SignalOut
from crud import (
    get_user_by_email_and_token,
    get_user_by_token,
    create_signal,
    fetch_signals_for_receiver_with_quota,
)

# ---------------- App / DB init ----------------
Base.metadata.create_all(bind=engine)  # in prod use Alembic
app = FastAPI(title="Nister Trade Server", version="2.1.0")

# ---------------- CORS (safe default) ----------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["*"],
    allow_methods=["*"],
)

# ---------------- Dependency ----------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------- Utilities ----------------
def bearer_token(auth_header: Optional[str]) -> str:
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    return auth_header.split(" ", 1)[1].strip()


def signal_to_out(sig: TradeSignal) -> SignalOut:
    return SignalOut(
        id=sig.id,
        symbol=sig.symbol,
        action=sig.action,
        sl_pips=sig.sl_pips,
        tp_pips=sig.tp_pips,
        lot_size=float(sig.lot_size) if sig.lot_size is not None else None,
        details=sig.details,
        created_at=(sig.created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                    if sig.created_at else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
    )


# ---------------- Health ----------------
@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}


# ---------------- Validate (email + api_key) ----------------
@app.post("/validate", response_model=ValidateResponse)
def validate(req: ValidateRequest, db: Session = Depends(get_db)):
    user = get_user_by_email_and_token(db, req.email.lower(), req.api_key)
    if not user:
        return ValidateResponse(ok=False, is_active=False, plan=None, daily_quota=None, expires_at=None)

    # daily_quota None => unlimited
    expires_at = None  # keep field for backward compat (you can compute if you have an expiry field)
    return ValidateResponse(
        ok=True,
        is_active=bool(user.is_active),
        plan=user.plan or "free",
        daily_quota=user.daily_quota,
        expires_at=expires_at,
    )


# ---------------- Post a signal (sender) ----------------
@app.post("/signals", response_model=SignalOut)
def post_signal(
    payload: SignalCreate,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None, convert_underscores=False),
):
    token = bearer_token(authorization)
    sender = get_user_by_token(db, token)
    if not sender:
        raise HTTPException(status_code=401, detail="Invalid API token")

    action = (payload.action or "").lower()
    if action not in {"buy", "sell", "adjust_sl", "adjust_tp", "close", "hold"}:
        raise HTTPException(status_code=422, detail=f"Invalid action '{payload.action}'")

    ts = create_signal(db, sender, payload.dict())
    db.commit()
    return signal_to_out(ts)


# ---------------- Fetch signals (receiver, quota-respecting per token) ----------------
@app.get("/signals", response_model=List[SignalOut])
def get_signals(
    response: Response,
    limit: int = 200,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None, convert_underscores=False),
):
    token = bearer_token(authorization)
    receiver = get_user_by_token(db, token)
    if not receiver:
        raise HTTPException(status_code=401, detail="Invalid API token")

    signals, used, remaining = fetch_signals_for_receiver_with_quota(db, receiver, token, limit=limit)
    db.commit()  # persist any read markers

    # Set helpful headers
    response.headers["X-Plan"] = (receiver.plan or "free")
    response.headers["X-Daily-Quota"] = str(receiver.daily_quota) if receiver.daily_quota is not None else "unlimited"
    response.headers["X-Used-Today"] = str(used)
    response.headers["X-Remaining"] = str(remaining) if remaining is not None else "unlimited"

    return [signal_to_out(s) for s in signals]

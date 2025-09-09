import hashlib
import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, Depends, Request, Response, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session

from database import SessionLocal
import crud


app = FastAPI(title="Trade Signal Server", version="1.0.0")

# CORS (adjust as you wish)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------------------------
# DB dependency
# -------------------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def bearer_from_request(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token")
    token = auth.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Bearer token")
    return token


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# -------------------------------------------------------------------
# Schemas
# -------------------------------------------------------------------
class ValidateIn(BaseModel):
    email: str
    api_key: str


class SignalIn(BaseModel):
    symbol: str = Field(..., min_length=3, max_length=32)
    action: str = Field(..., regex=r"^(buy|sell|adjust_sl|adjust_tp|close|hold)$")
    sl_pips: int = Field(..., ge=1)
    tp_pips: int = Field(..., ge=1)
    lot_size: float = Field(..., gt=0)
    details: Optional[dict] = None

    @validator("symbol")
    def norm_symbol(cls, v: str) -> str:
        return v.strip().upper()


class SignalOut(BaseModel):
    id: int
    symbol: str
    action: str
    sl_pips: int
    tp_pips: int
    lot_size: float
    details: Optional[dict]
    created_at: datetime


# -------------------------------------------------------------------
# /validate  (email + api_key)
# -------------------------------------------------------------------
@app.post("/validate")
def validate(payload: ValidateIn, db: Session = Depends(get_db)):
    user = crud.get_user_by_email_and_token(db, email=payload.email.strip().lower(), api_key=payload.api_key.strip())
    if not user or not user.is_active:
        return {
            "ok": False,
            "is_active": False,
        }

    eff = crud.effective_plan_for_user(user)
    return {
        "ok": True,
        "is_active": True,
        "plan": eff["plan"],
        "daily_quota": eff["daily_quota"],  # None => unlimited
        "expires_at": (user.expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                       if getattr(user, "expires_at", None) else None),
    }


# -------------------------------------------------------------------
# POST /signals  (sender posts a signal)
# -------------------------------------------------------------------
@app.post("/signals", response_model=SignalOut)
def post_signal(payload: SignalIn, request: Request, db: Session = Depends(get_db)):
    token = bearer_from_request(request)
    sender = crud.get_user_by_token(db, api_key=token)
    if not sender or not sender.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    signal = crud.create_signal(
        db,
        user_id=sender.id,
        symbol=payload.symbol,
        action=payload.action,
        sl_pips=payload.sl_pips,
        tp_pips=payload.tp_pips,
        lot_size=payload.lot_size,
        details=payload.details or {},
    )
    return SignalOut(
        id=signal.id,
        symbol=signal.symbol,
        action=signal.action,
        sl_pips=signal.sl_pips,
        tp_pips=signal.tp_pips,
        lot_size=float(signal.lot_size),
        details=signal.details,
        created_at=signal.created_at.replace(tzinfo=timezone.utc),
    )


# -------------------------------------------------------------------
# GET /signals  (receiver pulls feed; QUOTA ENFORCED PER TOKEN)
# -------------------------------------------------------------------
@app.get("/signals", response_model=List[SignalOut])
def get_signals(request: Request, response: Response, db: Session = Depends(get_db)):
    token = bearer_from_request(request)
    token_hash = sha256_hex(token)

    receiver = crud.get_user_by_token(db, api_key=token)
    if not receiver or not receiver.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    # Plan/quota
    eff = crud.effective_plan_for_user(receiver)  # {'plan': 'free'|'silver'|'gold', 'daily_quota': int|None}
    plan = eff["plan"]
    quota = eff["daily_quota"]  # None => unlimited

    # How many actionable (buy/sell) we have already delivered TODAY to THIS token
    opens_today = crud.count_actionables_today_for_token(db, token_hash=token_hash)

    # Build feed (all signals the receiver is entitled to, based on subscriptions)
    all_feed = crud.list_latest_signals_for_receiver(db, receiver_id=receiver.id)

    deliver: List[SignalOut] = []
    actionable_remaining: Optional[int] = None if quota is None else max(quota - opens_today, 0)

    for row in all_feed:
        is_actionable = row["action"] in ("buy", "sell")

        if is_actionable:
            if actionable_remaining is None:
                # unlimited plan
                seen = crud.track_signal_read(db, token_hash=token_hash, signal_id=row["id"])
                if seen:  # if it was a new delivery record, we could count it; but for headers we already expose opens_today
                    pass
                deliver.append(SignalOut(**row))
            elif actionable_remaining > 0:
                seen = crud.track_signal_read(db, token_hash=token_hash, signal_id=row["id"])
                if seen:
                    actionable_remaining -= 1  # only decrement when we actually mark a new delivery
                deliver.append(SignalOut(**row))
            else:
                # Quota exhausted: SKIP actionable
                continue
        else:
            # Non-actionables (adjust_sl/adjust_tp/close/hold) are not counted; always deliver
            deliver.append(SignalOut(**row))

    # Recompute effective opens-today if we decremented (for headers)
    new_opens_today = crud.count_actionables_today_for_token(db, token_hash=token_hash)

    # Debug headers
    response.headers["X-Plan"] = plan
    response.headers["X-Daily-Quota"] = "unlimited" if quota is None else str(quota)
    response.headers["X-Opens-Today"] = str(new_opens_today)
    response.headers["X-Remaining"] = "unlimited" if quota is None else str(max(quota - new_opens_today, 0))

    return deliver

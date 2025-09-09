import hashlib
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, Depends, Request, Response, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import SessionLocal
import crud


app = FastAPI(title="Trade Signal Server", version="1.0.0")

# CORS (adjust as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- DB dependency ----------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------- Helpers ----------------
def bearer_from_request(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token")
    token = auth.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Bearer token")
    return token


def sha256_hex(s: str) -> str:
    import hashlib as _h
    return _h.sha256(s.encode("utf-8")).hexdigest()


# ---------------- Schemas ----------------
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


class SignalOut(BaseModel):
    id: int
    symbol: str
    action: str
    sl_pips: int
    tp_pips: int
    lot_size: float
    details: Optional[dict]
    created_at: datetime


# ---------------- Health ----------------
@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}


# ---------------- /validate ----------------
@app.post("/validate")
def validate(payload: ValidateIn, db: Session = Depends(get_db)):
    user = crud.get_user_by_email_and_token(
        db, email=payload.email.strip().lower(), api_key=payload.api_key.strip()
    )
    if not user or not user.is_active:
        return {"ok": False, "is_active": False}

    eff = crud.effective_plan_for_user(user)
    return {
        "ok": True,
        "is_active": True,
        "plan": eff["plan"],
        "daily_quota": eff["daily_quota"],  # None => unlimited
        "expires_at": (
            user.expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if getattr(user, "expires_at", None)
            else None
        ),
    }


# ---------------- POST /signals (sender) ----------------
@app.post("/signals", response_model=SignalOut)
def post_signal(payload: SignalIn, request: Request, db: Session = Depends(get_db)):
    token = bearer_from_request(request)
    sender = crud.get_user_by_token(db, api_key=token)
    if not sender or not sender.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    # Normalize symbol here (avoid Pydantic validators for v2-compat)
    sym = (payload.symbol or "").strip().upper()

    signal = crud.create_signal(
        db,
        user_id=sender.id,
        symbol=sym,
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


# ---------------- GET /signals (receiver; QUOTA PER TOKEN) ----------------
@app.get("/signals", response_model=List[SignalOut])
def get_signals(request: Request, response: Response, db: Session = Depends(get_db)):
    token = bearer_from_request(request)
    token_hash = sha256_hex(token)

    receiver = crud.get_user_by_token(db, api_key=token)
    if not receiver or not receiver.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    eff = crud.effective_plan_for_user(receiver)  # {"plan":..., "daily_quota": int|None}
    plan = eff["plan"]
    quota = eff["daily_quota"]  # None => unlimited

    opens_today = crud.count_actionables_today_for_token(db, token_hash=token_hash)

    rows = crud.list_latest_signals_for_receiver(db, receiver_id=receiver.id)

    deliver: List[SignalOut] = []
    remaining = None if quota is None else max(quota - opens_today, 0)

    for r in rows:
        is_actionable = r["action"] in ("buy", "sell")

        if is_actionable:
            if remaining is None:
                # unlimited plan
                crud.track_signal_read(db, token_hash=token_hash, signal_id=r["id"])
                deliver.append(SignalOut(**r))
            elif remaining > 0:
                inserted = crud.track_signal_read(db, token_hash=token_hash, signal_id=r["id"])
                if inserted:
                    remaining -= 1
                deliver.append(SignalOut(**r))
            else:
                # quota exhausted => skip actionable
                continue
        else:
            # non-actionables always included and not counted
            deliver.append(SignalOut(**r))

    # Refresh for headers
    new_opens = crud.count_actionables_today_for_token(db, token_hash=token_hash)

    response.headers["X-Plan"] = plan
    response.headers["X-Daily-Quota"] = "unlimited" if quota is None else str(quota)
    response.headers["X-Opens-Today"] = str(new_opens)
    response.headers["X-Remaining"] = (
        "unlimited" if quota is None else str(max(quota - new_opens, 0))
    )

    return deliver

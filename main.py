# main.py
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from typing import Optional, List
from sqlalchemy.orm import Session
from database import get_db
from models import User, TradeSignal
from schemas import (
    ValidateRequest, ValidateResponse,
    SignalIn, SignalOut, SignalsResponse
)
import crud

app = FastAPI(title="Trade Signal Server", version="1.0")

# ---------------- Root ----------------
@app.get("/")
def root():
    return {"ok": True, "service": "trade-signal-server"}


# ---------------- Validate ----------------
@app.post("/validate", response_model=ValidateResponse)
def validate(req: ValidateRequest, db: Session = Depends(get_db)):
    """
    Validate with {email, api_key}. Always 200; EAs look for "ok": true.
    """
    ok, user = crud.validate_user(db, req.email.strip().lower(), req.api_key.strip())
    if not user:
        # unknown key or mismatch email -> ok=false, default plan
        return ValidateResponse(ok=False, is_active=False, plan="unknown", daily_quota=0, expires_at=None)

    plan_name, daily_quota = crud.plan_for_user(user)
    return ValidateResponse(
        ok=ok,
        is_active=bool(user.is_active),
        plan=plan_name,
        daily_quota=daily_quota,
        expires_at=user.expires_at,
    )


# ---------------- Auth helper ----------------
def bearer_or_api_key(authorization: Optional[str] = Header(None)) -> str:
    """
    Accept "Authorization: Bearer <api_key>".
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    raise HTTPException(status_code=401, detail="Invalid Authorization header")


def get_current_user(api_key: str = Depends(bearer_or_api_key), db: Session = Depends(get_db)) -> User:
    user = crud.get_user_by_api_key(db, api_key)
    if not user:
        # For EAs, GET /signals can return 401 properly (they'll retry)
        raise HTTPException(status_code=401, detail="Invalid API key")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Inactive user")
    if user.expires_at and user.expires_at <= crud.now_utc():
        raise HTTPException(status_code=403, detail="Subscription expired")
    return user


# ---------------- Signals: POST (sender) ----------------
@app.post("/signals", response_model=SignalOut)
def post_signal(
    payload: SignalIn,
    api_key: str = Depends(bearer_or_api_key),
    db: Session = Depends(get_db),
):
    """
    Sender posts signals. Authorization: Bearer <sender api_key>.
    """
    sender = crud.get_user_by_api_key(db, api_key)
    if not sender or not sender.is_active:
        raise HTTPException(status_code=401, detail="Invalid or inactive sender")

    sig = crud.create_trade_signal(
        db=db,
        user_id=sender.id,
        symbol=(payload.symbol or "").strip().upper(),
        action=(payload.action or "").strip().lower(),
        sl_pips=payload.sl_pips,
        tp_pips=payload.tp_pips,
        lot_size=payload.lot_size,
        details=payload.details,
    )
    return SignalOut(
        id=sig.id,
        symbol=sig.symbol,
        action=sig.action,
        sl_pips=sig.sl_pips,
        tp_pips=sig.tp_pips,
        lot_size=sig.lot_size,
        details=sig.details,
        created_at=sig.created_at,
    )


# ---------------- Signals: GET (receiver) ----------------
@app.get("/signals", response_model=SignalsResponse)
def get_signals(
    current: User = Depends(get_current_user),
    api_key: str = Depends(bearer_or_api_key),
    db: Session = Depends(get_db),
):
    """
    Receiver fetches signals. We:
      - filter by subscriptions
      - enforce daily quota per API key (buy/sell only)
      - record deliveries in signal_reads
    """
    deliver = crud.list_signals_for_receiver(db, current, api_key, max_fetch=200)
    # format
    out: List[SignalOut] = [
        SignalOut(
            id=s.id,
            symbol=s.symbol,
            action=s.action,
            sl_pips=s.sl_pips,
            tp_pips=s.tp_pips,
            lot_size=s.lot_size,
            details=s.details,
            created_at=s.created_at,
        )
        for s in deliver
    ]
    return SignalsResponse(signals=out)


# ---------------- Optional: peek latest (admin-ish) ----------------
@app.get("/latest")
def latest(
    limit: int = 20,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Quick peek at latest signals posted by *your* api key (sender view).
    """
    rows = (
        db.query(TradeSignal)
        .filter(TradeSignal.user_id == current.id)
        .order_by(TradeSignal.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return {
        "ok": True,
        "count": len(rows),
        "signals": [
            {
                "id": r.id,
                "symbol": r.symbol,
                "action": r.action,
                "sl_pips": r.sl_pips,
                "tp_pips": r.tp_pips,
                "lot_size": r.lot_size,
                "created_at": r.created_at,
                "details": r.details,
            }
            for r in rows
        ],
    }

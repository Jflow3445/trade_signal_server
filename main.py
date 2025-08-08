import os
import secrets
from fastapi import FastAPI, Depends, HTTPException, Header, Request, Query
from sqlalchemy.orm import Session
from datetime import datetime
from typing import List
from enum import Enum
from pydantic import BaseModel, Field

from database import SessionLocal, engine
import models
import crud
from schemas import (
    TradeSignalCreate,
    TradeSignalOut,
    TradeRecordCreate,
    TradeRecordOut,
    LatestSignalOut
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
def get_current_user(api_key: str = Query(None), db: Session = Depends(get_db)):
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API Key")
    user = crud.get_user_by_api_key(db, api_key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return user

def require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != os.getenv("TRADE_SERVER_API_KEY", "b0e216c8d199091f36aaada01a056211"):
        raise HTTPException(status_code=403, detail="Forbidden")
    return True

# ---------------- User Create Endpoint ----------------
class Tier(str, Enum):
    free = "free"
    silver = "silver"
    gold = "gold"

class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    tier: Tier = Tier.free
    quota: int = 1

@app.post("/users")
def create_user(
    req: UserCreateRequest,
    db: Session = Depends(get_db),
    admin_ok: bool = Depends(require_admin)
):
    if db.query(models.User).filter_by(username=req.username).first():
        raise HTTPException(status_code=400, detail="Username exists")
    api_key = secrets.token_hex(16)
    user = models.User(username=req.username, api_key=api_key, tier=req.tier, quota=req.quota)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {
        "username": user.username,
        "api_key": user.api_key,
        "tier": user.tier,
        "quota": user.quota
    }

# ---------------- Signal Endpoints ----------------
@app.post("/signals", response_model=TradeSignalOut)
def post_signal(signal: TradeSignalCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user.username != "farm_robot":
        raise HTTPException(status_code=403, detail="Not authorized to POST signals")
    signal.user_id = user.id
    created_signal = crud.create_signal(db, signal)
    crud.upsert_latest_signal(db, signal)
    return created_signal

@app.get("/signals", response_model=List[LatestSignalOut])
def get_all_latest_signals(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return db.query(models.LatestSignal).order_by(models.LatestSignal.symbol.asc()).all()

@app.get("/signals/{symbol}", response_model=TradeSignalOut)
def get_signal(symbol: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    latest_signal = crud.get_latest_signal(db, symbol)
    if not latest_signal:
        raise HTTPException(status_code=404, detail="No signal for this symbol")
    return latest_signal

# ---------------- Trade Endpoints ----------------
@app.post("/trades", response_model=TradeRecordOut)
def post_trade(trade: TradeRecordCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    trade.user_id = user.id
    trade.timestamp = datetime.utcnow()
    return crud.create_trade_record(db, trade)

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

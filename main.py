import os
from fastapi import FastAPI, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from database import SessionLocal, engine
import models
from schemas import TradeSignalCreate, TradeSignalOut, TradeRecordCreate, TradeRecordOut
import crud
from datetime import datetime
from typing import List
from schemas import LatestSignalOut
models.Base.metadata.create_all(bind=engine)

app = FastAPI()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(
    x_api_key: str = Header(None),
    x_api_key_alt: str = Header(None, alias="x-api-key"),
    db: Session = Depends(get_db)
):
    key = x_api_key or x_api_key_alt
    if not key:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    user = crud.get_user_by_api_key(db, key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return user

@app.post("/signals", response_model=TradeSignalOut)
def post_signal(
    signal: TradeSignalCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user)
):
    if user.username != "farm_robot":
        raise HTTPException(status_code=403, detail="Not authorized to POST signals")

    signal.user_id = user.id

    # 1. Save to history (all signals)
    created_signal = crud.create_signal(db, signal)
    # 2. Upsert latest (replace per symbol)
    crud.upsert_latest_signal(db, signal)

    return created_signal 

@app.get("/signals", response_model=List[LatestSignalOut])
def get_all_latest_signals(
    db: Session = Depends(get_db), 
    user=Depends(get_current_user)
):
    # Return all latest signals for all symbols
    return db.query(models.LatestSignal).order_by(models.LatestSignal.symbol.asc()).all()

@app.get("/signals/{symbol}", response_model=TradeSignalOut)
def get_signal(symbol: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    # Return only the latest signal for the given symbol
    latest_signal = crud.get_latest_signal(db, symbol)
    if not latest_signal:
        raise HTTPException(status_code=404, detail="No signal for this symbol")
    return latest_signal

@app.post("/trades", response_model=TradeRecordOut)
def post_trade(
    trade: TradeRecordCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user)
):
    # Record a new closed trade (open/close, PnL, side, duration, etc)
    # Allow both system and human users
    trade.user_id = user.id
    trade.timestamp = datetime.utcnow()
    created_trade = crud.create_trade_record(db, trade)
    return created_trade

@app.get("/trades", response_model=list[TradeRecordOut])
def get_all_trades(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return db.query(models.TradeRecord).order_by(models.TradeRecord.id.desc()).all()


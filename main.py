import os
from fastapi import FastAPI, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from database import SessionLocal, engine
import models
from schemas import TradeSignalCreate, TradeSignalOut
import crud
from datetime import datetime

models.Base.metadata.create_all(bind=engine)

app = FastAPI()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(x_api_key: str = Header(...), db: Session = Depends(get_db)):
    user = crud.get_user_by_api_key(db, x_api_key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return user

@app.post("/signals", response_model=TradeSignalOut)
def post_signal(
    signal: TradeSignalCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user)
):
    # Only allow the 'system' user (AI bot) to POST signals
    if user.username != "farm_robot":
        raise HTTPException(status_code=403, detail="Not authorized to POST signals")

    # You may skip quota logic for the system user, or keep as needed
    signal.user_id = user.id
    return crud.create_signal(db, signal)

@app.get("/signals/{symbol}", response_model=TradeSignalOut)
def get_signal(symbol: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return crud.get_latest_signal(db, symbol)

from sqlalchemy.orm import Session
from models import TradeSignal, User
from schemas import TradeSignalCreate

def create_signal(db: Session, signal: TradeSignalCreate):
    db_signal = TradeSignal(**signal.dict())
    db.add(db_signal)
    db.commit()
    db.refresh(db_signal)
    return db_signal

def get_latest_signal(db: Session, symbol: str):
    return db.query(TradeSignal).filter(TradeSignal.symbol == symbol).order_by(TradeSignal.created_at.desc()).first()

def get_signal_history(db: Session, symbol: str, limit=50):
    return db.query(TradeSignal).filter(TradeSignal.symbol == symbol).order_by(TradeSignal.created_at.desc()).limit(limit).all()

def get_user_by_api_key(db: Session, api_key: str):
    return db.query(User).filter(User.api_key == api_key).first()

def count_user_signals_today(db: Session, user_id: int, today):
    from datetime import datetime, timedelta
    from sqlalchemy import and_
    start_of_day = datetime.combine(today, datetime.min.time())
    end_of_day = datetime.combine(today, datetime.max.time())
    return db.query(TradeSignal).filter(
        TradeSignal.user_id == user_id,
        TradeSignal.created_at >= start_of_day,
        TradeSignal.created_at <= end_of_day
    ).count()

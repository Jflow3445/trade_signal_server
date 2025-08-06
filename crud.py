from datetime import datetime
from sqlalchemy.orm import Session
from models import TradeSignal, User, LatestSignal, TradeRecord
from schemas import TradeSignalCreate, TradeRecordCreate

def create_signal(db: Session, signal: TradeSignalCreate):
    db_signal = TradeSignal(**signal.dict())
    db.add(db_signal)
    db.commit()
    db.refresh(db_signal)
    return db_signal

def upsert_latest_signal(db: Session, signal: TradeSignalCreate):
    db_signal = db.query(LatestSignal).filter(LatestSignal.symbol == signal.symbol).first()
    if db_signal:
        for field, value in signal.dict().items():
            setattr(db_signal, field, value)
        db_signal.updated_at = datetime.utcnow()
    else:
        db_signal = LatestSignal(**signal.dict())
        db.add(db_signal)
    db.commit()
    db.refresh(db_signal)
    return db_signal

def get_latest_signal(db: Session, symbol: str):
    return db.query(LatestSignal).filter(LatestSignal.symbol == symbol).first()

def get_user_by_api_key(db: Session, api_key: str):
    return db.query(User).filter(User.api_key == api_key).first()

# --- Trade record CRUD ---
def create_trade_record(db: Session, trade: TradeRecordCreate):
    db_trade = TradeRecord(**trade.dict())
    db.add(db_trade)
    db.commit()
    db.refresh(db_trade)
    return db_trade

def get_trades_by_symbol(db: Session, symbol: str):
    return db.query(TradeRecord).filter(TradeRecord.symbol == symbol).order_by(TradeRecord.close_time.desc()).all()

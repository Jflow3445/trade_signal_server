# list_signals.py

from database import SessionLocal
from models import TradeSignal

db = SessionLocal()
signals = db.query(TradeSignal).order_by(TradeSignal.created_at.desc()).limit(10).all()
if not signals:
    print("No signals found.")
else:
    for sig in signals:
        print(f"{sig.id}: {sig.symbol} | {sig.action} | SL: {sig.sl_pips} | TP: {sig.tp_pips} | Lot: {sig.lot_size} | User: {sig.user_id} | {sig.created_at}")
db.close()

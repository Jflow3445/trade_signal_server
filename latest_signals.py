# list_latest_signals.py

from database import SessionLocal
from models import LatestSignal

db = SessionLocal()
signals = db.query(LatestSignal).order_by(LatestSignal.updated_at.desc()).all()
if not signals:
    print("No signals found.")
else:
    for sig in signals:
        print(f"{sig.id}: {sig.symbol} | {sig.action} | SL: {sig.sl_pips} | TP: {sig.tp_pips} | Lot: {sig.lot_size} | User: {sig.user_id} | {sig.updated_at}")
db.close()

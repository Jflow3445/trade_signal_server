# list_all_trades.py

from database import SessionLocal
from models import TradeRecord

db = SessionLocal()
trades = db.query(TradeRecord).order_by(TradeRecord.id.desc()).all()
if not trades:
    print("No trades found.")
else:
    for t in trades:
        print(
            f"ID: {t.id} | {t.symbol} | {t.side} | Entry: {t.entry_price} | Exit: {t.exit_price} | "
            f"PnL: {t.pnl} | Vol: {t.volume} | User: {t.user_id} | "
            f"Opened: {t.open_time} | Closed: {t.close_time}"
        )
db.close()

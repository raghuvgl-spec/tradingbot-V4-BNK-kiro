"""Clear old backtest trades and re-run."""
from app.trade_db import get_connection
conn = get_connection()
deleted = conn.execute("DELETE FROM trades WHERE trade_mode='BACKTEST'").rowcount
conn.commit()
print(f"Cleared {deleted} old backtest trades")

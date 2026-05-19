"""Check open positions."""
from app.trade_db import get_connection
conn = get_connection()

rows = conn.execute(
    "SELECT trade_id, entry_time, symbol, instrument, entry_price, status, entry_type "
    "FROM trades WHERE status='OPEN'"
).fetchall()

print(f"Open positions: {len(rows)}")
for r in rows:
    print(r)

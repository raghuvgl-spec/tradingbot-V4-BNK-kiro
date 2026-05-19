"""Check pattern matcher progress — trades per entry_type + instrument."""
from app.trade_db import get_connection

conn = get_connection()

rows = conn.execute(
    "SELECT entry_type, instrument, COUNT(*) as total, "
    "SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins, "
    "SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as losses "
    "FROM trades WHERE status='CLOSED' "
    "GROUP BY entry_type, instrument ORDER BY total DESC"
).fetchall()

print(f"{'Entry Type':<20} {'Instrument':<6} {'Total':>6} {'Wins':>5} {'Losses':>7} {'Need':>5}")
print("-" * 55)
for r in rows:
    need = max(0, 20 - r[2])
    status = "READY" if need == 0 else f"{need} more"
    print(f"{r[0]:<20} {r[1]:<6} {r[2]:>6} {r[3]:>5} {r[4]:>7}   {status}")

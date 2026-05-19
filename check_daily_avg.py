"""Check average trades per day."""
from app.trade_db import get_connection
conn = get_connection()

rows = conn.execute("""
    SELECT DATE(entry_time) as day, COUNT(*) as trades,
           SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
           ROUND(SUM(pnl), 0) as pnl
    FROM trades WHERE status='CLOSED' AND trade_mode='LIVE'
    GROUP BY DATE(entry_time)
    ORDER BY day
""").fetchall()

print(f"{'Date':<12} {'Trades':>7} {'Wins':>5} {'PnL':>10}")
print("-" * 38)
total_trades = 0
total_days = 0
for r in rows:
    print(f"{r[0]:<12} {r[1]:>7} {r[2]:>5} {r[3]:>+10,.0f}")
    total_trades += r[1]
    total_days += 1

if total_days > 0:
    print(f"\nAverage: {total_trades/total_days:.1f} trades/day over {total_days} days")

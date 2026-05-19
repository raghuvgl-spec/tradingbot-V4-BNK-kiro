"""Check market direction for time periods."""
from app.trade_db import get_connection

conn = get_connection()

periods = [
    ('10:00', '12:15'),
    ('12:30', '13:30'),
    ('13:30', '14:15'),
    ('14:30', '15:30'),
]

today = '2026-05-05'

print(f"{'Period':<14} {'Start':>10} {'End':>10} {'Move':>8} {'Direction'}")
print("-" * 55)

for start, end in periods:
    start_row = conn.execute(
        f"SELECT time, close FROM market_data_banknifty "
        f"WHERE time >= '{today} {start}:00' ORDER BY time ASC LIMIT 1"
    ).fetchone()
    end_row = conn.execute(
        f"SELECT time, close FROM market_data_banknifty "
        f"WHERE time <= '{today} {end}:00' ORDER BY time DESC LIMIT 1"
    ).fetchone()
    
    if start_row and end_row:
        move = end_row[1] - start_row[1]
        direction = 'BULLISH' if move > 0 else 'BEARISH' if move < 0 else 'FLAT'
        print(f"{start}-{end:<8} {start_row[1]:>10.2f} {end_row[1]:>10.2f} {move:>+8.2f} {direction}")
    else:
        print(f"{start}-{end:<8} NO DATA")

# Trades by side today
print("\n=== TRADES BY SIDE (Today) ===")
rows = conn.execute(
    "SELECT instrument, COUNT(*) as cnt, "
    "SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins, "
    "SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as losses, "
    "ROUND(SUM(pnl),2) as net_pnl "
    "FROM trades WHERE entry_time LIKE ? AND status='CLOSED' "
    "GROUP BY instrument",
    (f"{today}%",)
).fetchall()
print(f"{'Instrument':<12} {'Trades':>7} {'Wins':>6} {'Losses':>7} {'Net PnL':>10}")
print("-" * 45)
for r in rows:
    print(f"{r[0]:<12} {r[1]:>7} {r[2]:>6} {r[3]:>7} {r[4]:>+10.2f}")

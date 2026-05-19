"""Compare: when prebuy was missed, did the bot enter later at a worse price?"""
from app.trade_db import get_connection

conn = get_connection()

# Find prebuy rejections that were followed by an actual trade within 30 minutes
print("=== PREBUY MISS → LATER ENTRY COMPARISON ===")
print("(Did the bot enter the same move at a worse price?)\n")

rows = conn.execute("""
    SELECT r.candle_time, r.candle_close as prebuy_price, r.ema_gap_atr,
           t.entry_time, t.entry_price, t.entry_type, t.instrument, t.pnl, t.result,
           t.entry_price - r.candle_close as price_diff
    FROM signal_rejections r
    JOIN trades t ON t.entry_time > r.candle_time 
        AND t.entry_time <= datetime(r.candle_time, '+30 minutes')
        AND t.status = 'CLOSED'
        AND t.trade_mode = 'LIVE'
    WHERE r.signal_type = 'PREBUY'
    GROUP BY t.trade_id
    ORDER BY r.candle_time
""").fetchall()

print(f"{'Prebuy Time':<20} {'PB Price':>9} {'Entry Time':<20} {'Entry Price':>11} {'Diff':>7} {'Type':<18} {'PnL':>8} {'Result'}")
print("-" * 120)

total_worse = 0
total_better = 0
total_diff = 0

for r in rows:
    diff = r[9] if r[9] else 0
    marker = "⬆️WORSE" if diff > 0 else "⬇️BETTER" if diff < 0 else "="
    print(f"{r[0]:<20} {r[1]:>9.2f} {r[3]:<20} {r[4]:>11.2f} {diff:>+7.2f} {r[5]:<18} {r[7]:>+8,.0f} {r[8]} {marker}")
    
    if diff > 0:
        total_worse += 1
        total_diff += diff
    elif diff < 0:
        total_better += 1

print(f"\n{'=' * 60}")
print(f"  Trades that entered AFTER a prebuy miss: {len(rows)}")
print(f"  Entered at WORSE price (prebuy would have been better): {total_worse}")
print(f"  Entered at BETTER price: {total_better}")
if total_worse > 0:
    print(f"  Average price disadvantage: {total_diff/total_worse:.2f} pts")
    print(f"  Total points lost by missing prebuy: {total_diff:.2f} pts")
    print(f"  In rupees (60 qty): ₹{total_diff * 60:,.0f}")

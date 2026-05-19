"""Analyze missed PREBUY signals — how much profit was available."""
from app.trade_db import get_connection

conn = get_connection()

# Find all PREBUY rejections and check what happened after
print("=== MISSED PREBUY SIGNALS — Profit Analysis ===\n")

rows = conn.execute("""
    SELECT r.candle_time, r.side, r.rejection_reason, r.candle_close, r.atr_value,
           r.ema_cycle_phase, r.leading_phase, r.ema_gap_atr,
           (SELECT MAX(m.high) FROM market_data_banknifty m 
            WHERE m.time > r.candle_time AND m.time <= datetime(r.candle_time, '+30 minutes')) as max_high_30m,
           (SELECT MIN(m.low) FROM market_data_banknifty m 
            WHERE m.time > r.candle_time AND m.time <= datetime(r.candle_time, '+30 minutes')) as min_low_30m
    FROM signal_rejections r
    WHERE r.signal_type = 'PREBUY' AND r.atr_value > 0
    ORDER BY r.created_at
""").fetchall()

total_potential = 0
profitable_count = 0
total_checked = 0

print(f"{'Time':<20} {'Side':<5} {'Price':>8} {'Max Move':>9} {'Potential':>10} {'Reason'}")
print("-" * 85)

for r in rows:
    if r[8] is None or r[9] is None:
        continue
    total_checked += 1
    
    entry_price = r[3]
    atr = r[4]
    
    if r[1] == "BUY":
        max_move = r[8] - entry_price  # how far price went up in 30 min
    else:
        max_move = entry_price - r[9]  # how far price went down in 30 min
    
    # Would this have been profitable? (moved more than SL = 20 pts)
    if max_move > 20:
        profitable_count += 1
        potential_pnl = max_move * 60  # 60 qty
        total_potential += potential_pnl
        print(f"{r[0]:<20} {r[1]:<5} {entry_price:>8.2f} {max_move:>+9.2f} {potential_pnl:>+10,.0f} {r[2][:40]}")

print(f"\n{'=' * 60}")
print(f"  Total PREBUY rejections checked: {total_checked}")
print(f"  Profitable misses (moved > 20 pts): {profitable_count}")
print(f"  Total potential profit missed: ₹{total_potential:,.0f}")
print(f"  Average per missed trade: ₹{total_potential/profitable_count:,.0f}" if profitable_count > 0 else "")
print(f"  Hit rate: {profitable_count/total_checked*100:.1f}%" if total_checked > 0 else "")

# What were the rejection reasons?
print(f"\n=== REJECTION REASONS FOR PROFITABLE MISSES ===")
rows2 = conn.execute("""
    SELECT r.rejection_reason, COUNT(*) as cnt
    FROM signal_rejections r
    WHERE r.signal_type = 'PREBUY'
    GROUP BY r.rejection_reason
    ORDER BY cnt DESC
""").fetchall()

for r in rows2:
    print(f"  {r[0]:<50} {r[1]:>4} times")

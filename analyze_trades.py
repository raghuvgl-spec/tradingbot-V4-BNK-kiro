"""Analyze trades — left on table, missed trades, late SL hits."""
from app.trade_db import get_connection

conn = get_connection()

# 1. LEFT ON TABLE — how much profit was available but not captured
print("=" * 60)
print("  1. PROFIT LEFT ON TABLE")
print("=" * 60)

rows = conn.execute("""
    SELECT entry_type, instrument, 
           COUNT(*) as trades,
           ROUND(SUM(pnl), 2) as net_pnl,
           ROUND(SUM(peak_profit), 2) as total_peak_profit,
           ROUND(SUM(left_on_table), 2) as total_left,
           ROUND(AVG(capture_pct), 1) as avg_capture_pct
    FROM trades 
    WHERE status='CLOSED' AND peak_profit IS NOT NULL AND peak_profit > 0
    GROUP BY entry_type, instrument
    ORDER BY total_left DESC
""").fetchall()

print(f"{'Entry Type':<20} {'Inst':<4} {'Trades':>6} {'Net PnL':>10} {'Peak Avail':>12} {'Left on Table':>13} {'Capture%':>9}")
print("-" * 80)
total_left = 0
total_peak = 0
for r in rows:
    print(f"{r[0]:<20} {r[1]:<4} {r[2]:>6} {r[3]:>+10,.0f} {r[4]:>12,.0f} {r[5]:>13,.0f} {r[6]:>8.1f}%")
    total_left += r[5] or 0
    total_peak += r[4] or 0

print(f"\n  Total peak profit available: ₹{total_peak:,.0f}")
print(f"  Total left on table:         ₹{total_left:,.0f}")
print(f"  Overall capture rate:         {((total_peak - total_left) / total_peak * 100) if total_peak > 0 else 0:.1f}%")

# 2. WINNING TRADES WITH LOW CAPTURE — where we exited too early
print(f"\n{'=' * 60}")
print("  2. TRADES WITH HIGH PEAK BUT LOW CAPTURE (exited too early)")
print("=" * 60)

rows = conn.execute("""
    SELECT trade_id, entry_time, entry_type, instrument, 
           pnl, peak_profit, left_on_table, capture_pct, reason
    FROM trades 
    WHERE status='CLOSED' AND peak_profit > 500 AND capture_pct < 30
    ORDER BY left_on_table DESC
    LIMIT 15
""").fetchall()

print(f"{'Time':<20} {'Type':<18} {'I':<3} {'PnL':>8} {'Peak':>8} {'Left':>8} {'Cap%':>5} {'Exit Reason'}")
print("-" * 95)
for r in rows:
    print(f"{r[1]:<20} {r[2]:<18} {r[3]:<3} {r[4]:>+8,.0f} {r[5]:>8,.0f} {r[6]:>8,.0f} {r[7]:>4.0f}% {r[8]}")

# 3. TRADES WHERE SL HIT LATE (had profit first, then reversed to SL)
print(f"\n{'=' * 60}")
print("  3. SL HITS THAT HAD PROFIT FIRST (late reversals)")
print("=" * 60)

rows = conn.execute("""
    SELECT trade_id, entry_time, entry_type, instrument,
           pnl, peak_profit, left_on_table, reason
    FROM trades 
    WHERE status='CLOSED' AND result='LOSS' AND peak_profit > 0
    ORDER BY peak_profit DESC
    LIMIT 15
""").fetchall()

print(f"{'Time':<20} {'Type':<18} {'I':<3} {'PnL':>8} {'Had Peak':>9} {'Lost':>8} {'Exit'}")
print("-" * 80)
for r in rows:
    total_lost = r[4] * -1 + r[5]  # actual loss + peak they had
    print(f"{r[1]:<20} {r[2]:<18} {r[3]:<3} {r[4]:>+8,.0f} {r[5]:>+9,.0f} {r[6]:>8,.0f} {r[7]}")

# 4. SIGNAL REJECTIONS THAT WOULD HAVE BEEN PROFITABLE
print(f"\n{'=' * 60}")
print("  4. MISSED TRADES — rejected signals where price moved in favor")
print("=" * 60)

# Check rejections vs what happened next in market data
rows = conn.execute("""
    SELECT r.candle_time, r.signal_type, r.side, r.rejection_reason,
           r.candle_close, r.atr_value, r.ema_cycle_phase,
           (SELECT m.close FROM market_data_banknifty m 
            WHERE m.time > r.candle_time ORDER BY m.time LIMIT 1 OFFSET 9) as price_10_later
    FROM signal_rejections r
    WHERE r.atr_value > 0
    ORDER BY r.created_at DESC
    LIMIT 100
""").fetchall()

profitable_misses = 0
total_checked = 0
miss_by_type = {}

for r in rows:
    if r[7] is None or r[4] is None:
        continue
    total_checked += 1
    move = r[7] - r[4]  # price 10 candles later - price at rejection
    
    # Was the move in the signal direction?
    if r[2] == "BUY" and move > 20:  # moved 20+ pts in BUY direction
        profitable_misses += 1
        key = f"{r[1]}_{r[2]}"
        miss_by_type[key] = miss_by_type.get(key, 0) + 1
    elif r[2] == "SELL" and move < -20:  # moved 20+ pts in SELL direction
        profitable_misses += 1
        key = f"{r[1]}_{r[2]}"
        miss_by_type[key] = miss_by_type.get(key, 0) + 1

print(f"  Checked {total_checked} recent rejections")
print(f"  Profitable misses (moved 20+ pts in signal direction): {profitable_misses}")
print(f"  Miss rate: {profitable_misses/total_checked*100:.1f}%" if total_checked > 0 else "")
print(f"\n  By signal type:")
for k, v in sorted(miss_by_type.items(), key=lambda x: -x[1]):
    print(f"    {k:<25} missed {v} profitable signals")

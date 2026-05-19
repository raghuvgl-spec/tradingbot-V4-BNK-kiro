"""Analyze what conditions separate profitable vs unprofitable prebuy signals."""
from app.trade_db import get_connection

conn = get_connection()

# Look at prebuy rejections — which ones were profitable and which weren't
# Group by EMA gap range to find the sweet spot
print("=== PREBUY SIGNALS BY EMA GAP RANGE ===")
print("(Where should prebuy fire vs not fire?)\n")

rows = conn.execute("""
    SELECT r.candle_time, r.ema_gap_atr, r.candle_close, r.atr_value,
           r.ema_cycle_phase, r.leading_phase,
           (SELECT MAX(m.high) FROM market_data_banknifty m 
            WHERE m.time > r.candle_time AND m.time <= datetime(r.candle_time, '+20 minutes')) as max_high,
           (SELECT MIN(m.low) FROM market_data_banknifty m 
            WHERE m.time > r.candle_time AND m.time <= datetime(r.candle_time, '+20 minutes')) as min_low
    FROM signal_rejections r
    WHERE r.signal_type = 'PREBUY' AND r.atr_value > 0 AND r.ema_gap_atr IS NOT NULL
    ORDER BY r.created_at
""").fetchall()

# Bucket by ema_gap_atr
buckets = {
    "0.00-0.15": {"total": 0, "profitable": 0, "total_move": 0},
    "0.15-0.30": {"total": 0, "profitable": 0, "total_move": 0},
    "0.30-0.50": {"total": 0, "profitable": 0, "total_move": 0},
    "0.50-1.00": {"total": 0, "profitable": 0, "total_move": 0},
    "1.00+": {"total": 0, "profitable": 0, "total_move": 0},
}

for r in rows:
    if r[6] is None or r[7] is None:
        continue
    
    gap_atr = r[1]
    entry_price = r[2]
    max_move = r[6] - entry_price  # BUY side
    
    if gap_atr < 0.15:
        bucket = "0.00-0.15"
    elif gap_atr < 0.30:
        bucket = "0.15-0.30"
    elif gap_atr < 0.50:
        bucket = "0.30-0.50"
    elif gap_atr < 1.00:
        bucket = "0.50-1.00"
    else:
        bucket = "1.00+"
    
    buckets[bucket]["total"] += 1
    if max_move > 20:  # profitable if moved 20+ pts
        buckets[bucket]["profitable"] += 1
        buckets[bucket]["total_move"] += max_move

print(f"{'Gap/ATR Range':<14} {'Total':>7} {'Profitable':>11} {'Hit Rate':>9} {'Avg Move':>9}")
print("-" * 55)
for k, v in buckets.items():
    hit_rate = v["profitable"] / v["total"] * 100 if v["total"] > 0 else 0
    avg_move = v["total_move"] / v["profitable"] if v["profitable"] > 0 else 0
    print(f"{k:<14} {v['total']:>7} {v['profitable']:>11} {hit_rate:>8.1f}% {avg_move:>+8.1f}")

print("\n=== INSIGHT ===")
print("Prebuy should fire when gap/ATR is in the convergence zone (0.15-0.50)")
print("Below 0.15 = already sideways (too late)")
print("Above 0.50 = still trending (too early, counter-trend risk)")

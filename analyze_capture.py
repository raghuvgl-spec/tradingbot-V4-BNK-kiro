"""Analyze capture rate — separate winners from losers."""
from app.trade_db import get_connection

conn = get_connection()

# Winners only — what % of peak did we capture?
print("=== WINNING TRADES — Capture Analysis ===")
rows = conn.execute("""
    SELECT entry_type, instrument, COUNT(*) as cnt,
           ROUND(AVG(capture_pct), 1) as avg_capture,
           ROUND(SUM(pnl), 0) as actual_pnl,
           ROUND(SUM(peak_profit), 0) as peak_available,
           ROUND(SUM(left_on_table), 0) as left_on_table
    FROM trades 
    WHERE status='CLOSED' AND result='WIN' AND peak_profit > 0
    GROUP BY entry_type, instrument
    ORDER BY left_on_table DESC
""").fetchall()

print(f"{'Type':<20} {'I':<3} {'Wins':>5} {'Capture%':>9} {'Actual PnL':>11} {'Peak Avail':>11} {'Left':>10}")
print("-" * 75)
for r in rows:
    print(f"{r[0]:<20} {r[1]:<3} {r[2]:>5} {r[3]:>8.1f}% {r[4]:>+11,.0f} {r[5]:>11,.0f} {r[6]:>10,.0f}")

# Losers that had profit — the fixable ones
print("\n=== LOSING TRADES THAT HAD PROFIT (fixable with better exit) ===")
rows = conn.execute("""
    SELECT entry_type, instrument, COUNT(*) as cnt,
           ROUND(SUM(peak_profit), 0) as peak_they_had,
           ROUND(SUM(pnl), 0) as actual_loss,
           ROUND(SUM(peak_profit) + SUM(pnl), 0) as total_swing
    FROM trades 
    WHERE status='CLOSED' AND result='LOSS' AND peak_profit > 0
    GROUP BY entry_type, instrument
    ORDER BY peak_they_had DESC
""").fetchall()

print(f"{'Type':<20} {'I':<3} {'Losses':>6} {'Had Peak':>10} {'Actual Loss':>12} {'Total Swing':>12}")
print("-" * 70)
total_fixable = 0
for r in rows:
    print(f"{r[0]:<20} {r[1]:<3} {r[2]:>6} {r[3]:>+10,.0f} {r[4]:>+12,.0f} {r[5]:>+12,.0f}")
    total_fixable += r[3] or 0

print(f"\n  Total peak profit in LOSING trades: ₹{total_fixable:,.0f}")
print(f"  If we captured even 50% of these peaks instead of losing: +₹{total_fixable * 0.5:,.0f}")

# What exit reason caused the most left-on-table?
print("\n=== LEFT ON TABLE BY EXIT REASON ===")
rows = conn.execute("""
    SELECT reason, COUNT(*) as cnt,
           ROUND(SUM(left_on_table), 0) as total_left,
           ROUND(AVG(left_on_table), 0) as avg_left
    FROM trades 
    WHERE status='CLOSED' AND left_on_table > 0
    GROUP BY reason
    ORDER BY total_left DESC
""").fetchall()

print(f"{'Exit Reason':<15} {'Trades':>7} {'Total Left':>12} {'Avg Left':>10}")
print("-" * 50)
for r in rows:
    print(f"{r[0]:<15} {r[1]:>7} {r[2]:>+12,.0f} {r[3]:>+10,.0f}")

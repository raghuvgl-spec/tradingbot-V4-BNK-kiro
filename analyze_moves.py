"""Analyze big moves and small moves from market data."""
from app.trade_db import get_connection
import pandas as pd

conn = get_connection()

df = pd.read_sql_query(
    "SELECT time, open, high, low, close FROM market_data_banknifty ORDER BY time", conn
)
df["time"] = pd.to_datetime(df["time"])
df["date"] = df["time"].dt.date

# Find swing moves (crossover to crossover approximation)
# Use daily high-low range as proxy for moves
print("=== DAILY MOVES (High - Low) ===\n")

daily = df.groupby("date").agg(
    day_high=("high", "max"),
    day_low=("low", "min"),
    day_open=("open", "first"),
    day_close=("close", "last"),
).reset_index()

daily["range"] = daily["day_high"] - daily["day_low"]
daily["direction"] = daily.apply(lambda r: "BULL" if r["day_close"] > r["day_open"] else "BEAR", axis=1)

print(f"{'Date':<12} {'Range':>8} {'Open':>10} {'Close':>10} {'Direction'}")
print("-" * 55)
for _, r in daily.iterrows():
    print(f"{r['date']!s:<12} {r['range']:>8.2f} {r['day_open']:>10.2f} {r['day_close']:>10.2f} {r['direction']}")

# Categorize moves
big_moves = daily[daily["range"] >= 300]  # 300+ pts = big move
medium_moves = daily[(daily["range"] >= 150) & (daily["range"] < 300)]
small_moves = daily[daily["range"] < 150]

print(f"\n{'=' * 50}")
print(f"  MOVE CATEGORIES (daily range)")
print(f"{'=' * 50}")
print(f"  Big moves (300+ pts):    {len(big_moves)} days | Total range: {big_moves['range'].sum():.0f} pts")
print(f"  Medium moves (150-300):  {len(medium_moves)} days | Total range: {medium_moves['range'].sum():.0f} pts")
print(f"  Small moves (<150):      {len(small_moves)} days | Total range: {small_moves['range'].sum():.0f} pts")

# Now check trades by PnL size
print(f"\n{'=' * 50}")
print(f"  TRADES BY PNL SIZE")
print(f"{'=' * 50}")

all_trades = conn.execute(
    "SELECT pnl, entry_type, instrument FROM trades WHERE status='CLOSED' AND pnl IS NOT NULL AND trade_mode='LIVE'"
).fetchall()

big_wins = [t for t in all_trades if t[0] >= 3000]
medium_wins = [t for t in all_trades if 1500 <= t[0] < 3000]
small_wins = [t for t in all_trades if 0 < t[0] < 1500]
losses = [t for t in all_trades if t[0] < 0]

print(f"\n  Big wins (₹3000+):     {len(big_wins)} trades | Total: ₹{sum(t[0] for t in big_wins):,.0f}")
print(f"  Medium wins (₹1500-3000): {len(medium_wins)} trades | Total: ₹{sum(t[0] for t in medium_wins):,.0f}")
print(f"  Small wins (₹0-1500):  {len(small_wins)} trades | Total: ₹{sum(t[0] for t in small_wins):,.0f}")
print(f"  Losses:                {len(losses)} trades | Total: ₹{sum(t[0] for t in losses):,.0f}")
print(f"\n  Net: ₹{sum(t[0] for t in all_trades):,.0f}")

# Big wins breakdown
if big_wins:
    print(f"\n  --- Big Wins Breakdown ---")
    for t in sorted(big_wins, key=lambda x: -x[0])[:10]:
        print(f"    ₹{t[0]:>+8,.0f} | {t[1]:<20} | {t[2]}")

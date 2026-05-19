"""Load Kite CSV into database and run backtest."""
import pandas as pd
from app.trade_db import get_connection, save_market_candles_bulk
from backtest_strategy import compute_indicators, run_backtest, log_trades_to_db

# Load CSV
print("📂 Loading Kite CSV...")
df = pd.read_csv("data/banknifty_1min.csv")
df.rename(columns={"date": "time"}, inplace=True)
df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
df = df.sort_values("time").reset_index(drop=True)

print(f"✅ Loaded {len(df)} candles")
print(f"   Date range: {df['time'].min()} → {df['time'].max()}")
print(f"   Trading days: {df['time'].dt.date.nunique()}")

# Save to database
print("\n💾 Saving to market_data_banknifty...")
rows = []
for _, r in df.iterrows():
    rows.append((
        r["time"].strftime("%Y-%m-%d %H:%M:%S"),
        float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
        float(r.get("volume", 0)),
        None, None, None, None,
    ))
save_market_candles_bulk(rows)
print(f"✅ Saved {len(rows)} candles to database")

# Clear old backtest trades
conn = get_connection()
deleted = conn.execute("DELETE FROM trades WHERE trade_mode='BACKTEST'").rowcount
conn.commit()
print(f"🗑️  Cleared {deleted} old backtest trades")

# Compute indicators
print("\n📊 Computing indicators...")
df = compute_indicators(df)

# Run backtest
trades = run_backtest(df)

if not trades:
    print("\n❌ No trades generated")
else:
    # Summary
    wins = [t for t in trades if t["result"] == "WIN"]
    losses = [t for t in trades if t["result"] == "LOSS"]
    net_pnl = sum(t["pnl"] for t in trades)
    win_rate = len(wins) / len(trades) * 100

    print(f"\n{'=' * 60}")
    print(f"  90-DAY BACKTEST RESULTS (Kite Data)")
    print(f"{'=' * 60}")
    print(f"  Total Trades:  {len(trades)}")
    print(f"  Wins:          {len(wins)} ({win_rate:.1f}%)")
    print(f"  Losses:        {len(losses)}")
    print(f"  Net PnL:       ₹{net_pnl:,.0f}")
    print(f"  Avg Win:       ₹{sum(t['pnl'] for t in wins)/len(wins):,.0f}" if wins else "")
    print(f"  Avg Loss:      ₹{sum(t['pnl'] for t in losses)/len(losses):,.0f}" if losses else "")

    # By entry type
    print(f"\n  --- By Entry Type ---")
    entry_types = set(t["entry_type"] for t in trades)
    for et in sorted(entry_types):
        et_trades = [t for t in trades if t["entry_type"] == et]
        et_wins = len([t for t in et_trades if t["result"] == "WIN"])
        et_pnl = sum(t["pnl"] for t in et_trades)
        wr = et_wins / len(et_trades) * 100 if et_trades else 0
        print(f"  {et:<20} trades={len(et_trades):>4} wins={et_wins:>3} ({wr:.0f}%) pnl=₹{et_pnl:>+10,.0f}")

    # Big wins vs losses
    big_wins = [t for t in trades if t["pnl"] >= 3000]
    medium_wins = [t for t in trades if 1500 <= t["pnl"] < 3000]
    print(f"\n  Big wins (₹3000+):     {len(big_wins)} | ₹{sum(t['pnl'] for t in big_wins):,.0f}")
    print(f"  Medium wins (₹1500-3000): {len(medium_wins)} | ₹{sum(t['pnl'] for t in medium_wins):,.0f}")

    # Log to database
    log_trades_to_db(trades)

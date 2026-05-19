"""Backtest on 3-min and 5-min timeframes by resampling 1-min data."""
import pandas as pd
from app.trade_db import get_connection
from backtest_strategy import compute_indicators, run_backtest

conn = get_connection()

# Load 1-min data
df_1m = pd.read_sql_query(
    "SELECT time, open, high, low, close, volume FROM market_data_banknifty ORDER BY time",
    conn,
)
df_1m["time"] = pd.to_datetime(df_1m["time"])
df_1m = df_1m.set_index("time")

print(f"1-min candles: {len(df_1m)}")


def resample_candles(df, freq):
    """Resample to higher timeframe."""
    resampled = df.resample(freq).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    resampled = resampled.reset_index()
    return resampled


def run_for_timeframe(label, df):
    """Run backtest and print results."""
    df = compute_indicators(df)
    trades = run_backtest(df)

    if not trades:
        print(f"\n  {label}: No trades generated")
        return

    wins = [t for t in trades if t["result"] == "WIN"]
    losses = [t for t in trades if t["result"] == "LOSS"]
    net_pnl = sum(t["pnl"] for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0

    print(f"\n{'=' * 50}")
    print(f"  {label} RESULTS")
    print(f"{'=' * 50}")
    print(f"  Trades:    {len(trades)}")
    print(f"  Wins:      {len(wins)} ({win_rate:.1f}%)")
    print(f"  Losses:    {len(losses)}")
    print(f"  Net PnL:   ₹{net_pnl:,.0f}")
    print(f"  Avg Win:   ₹{avg_win:,.0f}")
    print(f"  Avg Loss:  ₹{avg_loss:,.0f}")

    # By entry type
    entry_types = set(t["entry_type"] for t in trades)
    for et in sorted(entry_types):
        et_trades = [t for t in trades if t["entry_type"] == et]
        et_wins = len([t for t in et_trades if t["result"] == "WIN"])
        et_pnl = sum(t["pnl"] for t in et_trades)
        print(f"    {et:<20} trades={len(et_trades):>3} wins={et_wins:>3} pnl=₹{et_pnl:>+10,.0f}")


# Run 1-min (already done, just for comparison)
print("\n" + "=" * 50)
print("  TIMEFRAME COMPARISON")
print("=" * 50)

df_1min = df_1m.reset_index()
run_for_timeframe("1-MIN", df_1min)

# 3-min
df_3m = resample_candles(df_1m, "3min")
print(f"\n3-min candles: {len(df_3m)}")
run_for_timeframe("3-MIN", df_3m)

# 5-min
df_5m = resample_candles(df_1m, "5min")
print(f"\n5-min candles: {len(df_5m)}")
run_for_timeframe("5-MIN", df_5m)

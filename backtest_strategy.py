"""
Strategy Backtester — replays historical candles through the real strategy logic
and logs trades to the database with trade_mode='BACKTEST'.

Usage:
    python backtest_strategy.py [--days 60]

Fetches historical 1-min candle data from Angel One API and simulates
the full strategy (crossover, prebuy, trend, higher_low, reentry, etc.)
with real SL/target exit logic.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, date
from dataclasses import dataclass

import pandas as pd

from app.config import (
    FAST_EMA_PERIOD, SLOW_EMA_PERIOD, INDEX_TOKEN, INDEX_EXCHANGE,
    SL_POINTS, TARGET_POINTS, QTY, ATR_SL_MULTIPLIER, ATR_TARGET_MULTIPLIER,
    MIN_EMA_GAP_ATR, MIN_CANDLE_RANGE_ATR, MIN_BODY_RATIO, MIN_ATR_THRESHOLD,
)
from app.indicators import calculate_ema, calculate_atr, safe_float
from app.trade_db import get_connection, save_market_candles_bulk


# ------------------------------------------------------------------
# Backtester Configuration
# ------------------------------------------------------------------

BACKTEST_DAYS = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 60
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MIN = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MIN = 30


# ------------------------------------------------------------------
# Candle Data Fetching
# ------------------------------------------------------------------

def fetch_all_historical_candles(days: int) -> pd.DataFrame:
    """Fetch historical 1-min candles from Angel One API in chunks."""
    from app.broker import login, fetch_historical_candles

    print("🔐 Logging in to broker...")
    login()
    print("✅ Login successful")

    all_candles = []
    end_date = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
    start_date = end_date - timedelta(days=days)

    # API typically allows max 5 days per request for 1-min data
    chunk_days = 5
    current_start = start_date

    while current_start < end_date:
        current_end = min(current_start + timedelta(days=chunk_days), end_date)

        print(f"📡 Fetching: {current_start.strftime('%Y-%m-%d')} → {current_end.strftime('%Y-%m-%d')}")

        candles = fetch_historical_candles(
            token=INDEX_TOKEN,
            from_dt=current_start,
            to_dt=current_end,
            interval="ONE_MINUTE",
            exchange=INDEX_EXCHANGE,
        )

        if candles:
            all_candles.extend(candles)
            print(f"   Got {len(candles)} candles")
        else:
            print(f"   No data for this period")

        current_start = current_end
        time.sleep(0.5)  # Rate limiting

    if not all_candles:
        print("❌ No historical data fetched")
        return pd.DataFrame()

    # Convert to DataFrame
    df = pd.DataFrame(all_candles, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    df = df.drop_duplicates(subset=["time"]).reset_index(drop=True)

    print(f"\n✅ Total candles fetched: {len(df)}")
    print(f"   Date range: {df['time'].min()} → {df['time'].max()}")

    return df


def load_candles_from_db() -> pd.DataFrame:
    """Load existing candles from market_data_banknifty table."""
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT time, open, high, low, close, volume FROM market_data_banknifty ORDER BY time",
        conn,
    )
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
    return df


# ------------------------------------------------------------------
# Indicator Computation
# ------------------------------------------------------------------

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add EMA and ATR columns to the DataFrame."""
    df = df.copy()
    df["ema9"] = df["close"].ewm(span=FAST_EMA_PERIOD, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=SLOW_EMA_PERIOD, adjust=False).mean()

    # ATR (14-period)
    df["prev_close"] = df["close"].shift(1)
    df["tr"] = df.apply(
        lambda r: max(
            r["high"] - r["low"],
            abs(r["high"] - r["prev_close"]) if pd.notna(r["prev_close"]) else r["high"] - r["low"],
            abs(r["low"] - r["prev_close"]) if pd.notna(r["prev_close"]) else r["high"] - r["low"],
        ),
        axis=1,
    )
    df["atr"] = df["tr"].rolling(window=14).mean()
    df.drop(columns=["prev_close", "tr"], inplace=True)

    # EMA gap
    df["ema_gap"] = abs(df["ema9"] - df["ema21"])
    df["ema_gap_atr"] = df["ema_gap"] / df["atr"].replace(0, pd.NA)

    return df


# ------------------------------------------------------------------
# Strategy Simulation
# ------------------------------------------------------------------

@dataclass
class BacktestPosition:
    trade_id: str
    entry_time: str
    symbol: str
    instrument: str
    side: str
    entry_price: float
    sl: float
    target: float
    entry_type: str
    atr: float
    highest_price: float
    lowest_price: float


def detect_crossover(row, prev_row) -> tuple[bool, bool]:
    """Detect EMA crossover."""
    if any(pd.isna(v) for v in [row["ema9"], row["ema21"], prev_row["ema9"], prev_row["ema21"]]):
        return False, False
    buy_cross = prev_row["ema9"] <= prev_row["ema21"] and row["ema9"] > row["ema21"]
    sell_cross = prev_row["ema9"] >= prev_row["ema21"] and row["ema9"] < row["ema21"]
    return buy_cross, sell_cross


def is_sideways(row) -> bool:
    """Check if market is sideways."""
    if pd.isna(row["atr"]) or row["atr"] <= 0:
        return True
    if pd.isna(row["ema_gap_atr"]):
        return True
    if row["ema_gap_atr"] < 0.30:  # Updated from 0.25 to 0.30
        return True
    candle_range = row["high"] - row["low"]
    if candle_range < row["atr"] * MIN_CANDLE_RANGE_ATR:
        return True
    body = abs(row["close"] - row["open"])
    body_ratio = body / candle_range if candle_range > 0 else 0
    if body_ratio < MIN_BODY_RATIO:
        return True
    return False


def get_entry_type(row, prev_row, trend_side, last_cross_candle, i) -> str | None:
    """Determine entry type based on strategy logic."""
    if pd.isna(row["atr"]) or row["atr"] <= 0 or row["atr"] < MIN_ATR_THRESHOLD:
        return None

    # Block overextended entries (price > 1.5x ATR from EMA9)
    if pd.notna(row["ema9"]):
        dist_from_ema9 = abs(row["close"] - row["ema9"])
        if dist_from_ema9 > row["atr"] * 1.5:
            return None  # OVEREXTENDED — skip

    buy_cross, sell_cross = detect_crossover(row, prev_row)

    # Crossover
    if buy_cross and not is_sideways(row):
        return "CROSSOVER_BUY"
    if sell_cross and not is_sideways(row):
        return "CROSSOVER_SELL"

    # Trend continuation (after crossover, in expanding phase)
    if trend_side == "BUY" and row["ema9"] > row["ema21"]:
        if not is_sideways(row) and row["close"] > row["ema9"]:
            if row["close"] > row["open"]:  # bullish candle
                candles_since_cross = i - last_cross_candle if last_cross_candle else 999
                if 3 <= candles_since_cross <= 30:
                    return "TREND_BUY"

    if trend_side == "SELL" and row["ema9"] < row["ema21"]:
        if not is_sideways(row) and row["close"] < row["ema9"]:
            if row["close"] < row["open"]:  # bearish candle
                candles_since_cross = i - last_cross_candle if last_cross_candle else 999
                if 3 <= candles_since_cross <= 30:
                    return "TREND_SELL"

    # Higher Low / Lower High
    if trend_side == "BUY" and row["ema9"] > row["ema21"]:
        if not is_sideways(row):
            # Price near EMA9 (within 0.5 ATR) and bouncing
            dist = abs(row["close"] - row["ema9"])
            if dist < row["atr"] * 0.5 and row["close"] > row["open"]:
                if row["low"] > prev_row["low"]:  # higher low
                    return "HIGHER_LOW_BUY"

    if trend_side == "SELL" and row["ema9"] < row["ema21"]:
        if not is_sideways(row):
            dist = abs(row["close"] - row["ema9"])
            if dist < row["atr"] * 0.5 and row["close"] < row["open"]:
                if row["high"] < prev_row["high"]:  # lower high
                    return "LOWER_HIGH_SELL"

    return None


def run_backtest(df: pd.DataFrame) -> list[dict]:
    """Run the strategy on historical data and return trade results."""
    trades = []
    position: BacktestPosition | None = None
    trend_side: str | None = None
    last_cross_candle: int | None = None
    trade_counter = 0
    cooldown_until: int | None = None

    # Filter to market hours only
    df = df[
        (df["time"].dt.hour * 60 + df["time"].dt.minute >= MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MIN)
        & (df["time"].dt.hour * 60 + df["time"].dt.minute <= MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN)
    ].reset_index(drop=True)

    print(f"\n🔄 Running backtest on {len(df)} candles...")

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i - 1]

        if pd.isna(row["atr"]) or row["atr"] <= 0:
            continue

        # --- Manage open position ---
        if position is not None:
            current_price = row["close"]

            # Update highest/lowest
            position.highest_price = max(position.highest_price, row["high"])
            position.lowest_price = min(position.lowest_price, row["low"])

            # Check SL/Target
            if position.side == "BUY":
                if row["low"] <= position.sl:
                    # SL hit
                    exit_price = position.sl
                    pnl = (exit_price - position.entry_price) * QTY
                    trades.append(_close_trade(position, exit_price, pnl, "LOSS", "SL", row["time"]))
                    position = None
                    cooldown_until = i + 3  # 3 candle cooldown
                    continue
                elif row["high"] >= position.target:
                    # Target hit
                    exit_price = position.target
                    pnl = (exit_price - position.entry_price) * QTY
                    trades.append(_close_trade(position, exit_price, pnl, "WIN", "TARGET", row["time"]))
                    position = None
                    cooldown_until = i + 3
                    continue
            else:  # SELL side (PE)
                if row["high"] >= position.sl:
                    exit_price = position.sl
                    pnl = (position.entry_price - exit_price) * QTY
                    trades.append(_close_trade(position, exit_price, pnl, "LOSS", "SL", row["time"]))
                    position = None
                    cooldown_until = i + 3
                    continue
                elif row["low"] <= position.target:
                    exit_price = position.target
                    pnl = (position.entry_price - exit_price) * QTY
                    trades.append(_close_trade(position, exit_price, pnl, "WIN", "TARGET", row["time"]))
                    position = None
                    cooldown_until = i + 3
                    continue

            # Force exit at market close
            if row["time"].hour == MARKET_CLOSE_HOUR and row["time"].minute >= MARKET_CLOSE_MIN - 5:
                exit_price = current_price
                if position.side == "BUY":
                    pnl = (exit_price - position.entry_price) * QTY
                else:
                    pnl = (position.entry_price - exit_price) * QTY
                result = "WIN" if pnl > 0 else "LOSS"
                trades.append(_close_trade(position, exit_price, pnl, result, "MARKET_CLOSE", row["time"]))
                position = None
                continue

            continue  # In position, skip entry logic

        # --- Cooldown ---
        if cooldown_until and i < cooldown_until:
            continue

        # --- Detect crossover for trend tracking ---
        buy_cross, sell_cross = detect_crossover(row, prev_row)
        if buy_cross:
            trend_side = "BUY"
            last_cross_candle = i
        elif sell_cross:
            trend_side = "SELL"
            last_cross_candle = i

        # --- Entry logic ---
        entry_type = get_entry_type(row, prev_row, trend_side, last_cross_candle, i)

        if entry_type is None:
            continue

        # Determine side and instrument
        if entry_type.endswith("_BUY"):
            side = "BUY"
            instrument = "CE"
        elif entry_type.endswith("_SELL"):
            side = "SELL"
            instrument = "PE"
        else:
            continue

        # Clean entry type name (remove _BUY/_SELL suffix for DB)
        clean_entry_type = entry_type.replace("_BUY", "").replace("_SELL", "")
        if clean_entry_type == "CROSSOVER":
            clean_entry_type = "CROSSOVER"
        elif clean_entry_type == "HIGHER_LOW":
            clean_entry_type = "HIGHER_LOW_BUY" if side == "BUY" else "LOWER_HIGH_SELL"
        elif clean_entry_type == "LOWER_HIGH":
            clean_entry_type = "LOWER_HIGH_SELL"
        elif clean_entry_type == "TREND":
            clean_entry_type = "TREND"

        # Calculate SL/Target
        atr = row["atr"]
        entry_price = row["close"]

        if side == "BUY":
            sl = entry_price - SL_POINTS
            target = entry_price + TARGET_POINTS
        else:
            sl = entry_price + SL_POINTS
            target = entry_price - TARGET_POINTS

        trade_counter += 1
        trade_id = f"BT_{trade_counter:06d}"

        position = BacktestPosition(
            trade_id=trade_id,
            entry_time=str(row["time"]),
            symbol="BANKNIFTY",
            instrument=instrument,
            side=side,
            entry_price=entry_price,
            sl=sl,
            target=target,
            entry_type=clean_entry_type,
            atr=atr,
            highest_price=row["high"],
            lowest_price=row["low"],
        )

    # Force close any remaining position
    if position is not None:
        last_row = df.iloc[-1]
        exit_price = last_row["close"]
        if position.side == "BUY":
            pnl = (exit_price - position.entry_price) * QTY
        else:
            pnl = (position.entry_price - exit_price) * QTY
        result = "WIN" if pnl > 0 else "LOSS"
        trades.append(_close_trade(position, exit_price, pnl, result, "END_OF_DATA", last_row["time"]))

    return trades


def _close_trade(pos: BacktestPosition, exit_price: float, pnl: float,
                 result: str, reason: str, exit_time) -> dict:
    """Create a trade result dict."""
    peak_profit = (pos.highest_price - pos.entry_price) * QTY if pos.side == "BUY" else (pos.entry_price - pos.lowest_price) * QTY
    left_on_table = max(0, peak_profit - pnl)
    capture_pct = (pnl / peak_profit * 100) if peak_profit > 0 else 0

    return {
        "trade_id": pos.trade_id,
        "entry_time": pos.entry_time,
        "exit_time": str(exit_time),
        "symbol": pos.symbol,
        "instrument": pos.instrument,
        "side": pos.side,
        "entry_price": pos.entry_price,
        "exit_price": exit_price,
        "pnl": round(pnl, 2),
        "result": result,
        "reason": reason,
        "entry_type": pos.entry_type,
        "atr": pos.atr,
        "sl": pos.sl,
        "target": pos.target,
        "highest_price": pos.highest_price,
        "lowest_price": pos.lowest_price,
        "peak_profit": round(peak_profit, 2),
        "left_on_table": round(left_on_table, 2),
        "capture_pct": round(capture_pct, 2),
    }


# ------------------------------------------------------------------
# Database Logging
# ------------------------------------------------------------------

def log_trades_to_db(trades: list[dict]):
    """Insert backtest trades into the database with trade_mode='BACKTEST'."""
    conn = get_connection()

    insert_sql = """
    INSERT OR IGNORE INTO trades (
        trade_id, entry_time, exit_time, symbol, instrument, side, qty,
        entry_price, exit_price, pnl, result, reason, status,
        atr, sl, target, entry_type, trade_mode,
        peak_profit, left_on_table, capture_pct, highest_price, lowest_price
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?, ?, ?, ?, 'BACKTEST', ?, ?, ?, ?, ?)
    """

    for t in trades:
        conn.execute(insert_sql, (
            t["trade_id"], t["entry_time"], t["exit_time"],
            t["symbol"], t["instrument"], t["side"], QTY,
            t["entry_price"], t["exit_price"], t["pnl"],
            t["result"], t["reason"],
            t["atr"], t["sl"], t["target"], t["entry_type"],
            t["peak_profit"], t["left_on_table"], t["capture_pct"],
            t["highest_price"], t["lowest_price"],
        ))

    conn.commit()
    print(f"\n✅ Logged {len(trades)} backtest trades to database (trade_mode='BACKTEST')")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    print(f"=" * 60)
    print(f"  STRATEGY BACKTESTER — {BACKTEST_DAYS} days")
    print(f"=" * 60)

    # Try to load from DB first, fetch from API if not enough
    df = load_candles_from_db()

    if len(df) < 1000:
        print("\nInsufficient data in DB, fetching from API...")
        df = fetch_all_historical_candles(BACKTEST_DAYS)
        if df.empty:
            print("❌ Could not fetch historical data. Exiting.")
            return

        # Save to market data table
        rows = []
        for _, r in df.iterrows():
            rows.append((
                r["time"].strftime("%Y-%m-%d %H:%M:%S"),
                float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                float(r.get("volume", 0)),
                None, None, None, None,
            ))
        save_market_candles_bulk(rows)
        print(f"✅ Saved {len(rows)} candles to market_data_banknifty")
    else:
        print(f"✅ Loaded {len(df)} candles from database")

    # Compute indicators
    df = compute_indicators(df)

    # Run backtest
    trades = run_backtest(df)

    if not trades:
        print("\n❌ No trades generated. Check strategy parameters.")
        return

    # Print summary
    wins = [t for t in trades if t["result"] == "WIN"]
    losses = [t for t in trades if t["result"] == "LOSS"]
    net_pnl = sum(t["pnl"] for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0

    print(f"\n{'=' * 60}")
    print(f"  BACKTEST RESULTS")
    print(f"{'=' * 60}")
    print(f"  Total Trades:  {len(trades)}")
    print(f"  Wins:          {len(wins)}")
    print(f"  Losses:        {len(losses)}")
    print(f"  Win Rate:      {win_rate:.1f}%")
    print(f"  Net PnL:       ₹{net_pnl:,.2f}")
    print(f"  Avg Win:       ₹{sum(t['pnl'] for t in wins)/len(wins):,.2f}" if wins else "  Avg Win:       N/A")
    print(f"  Avg Loss:      ₹{sum(t['pnl'] for t in losses)/len(losses):,.2f}" if losses else "  Avg Loss:      N/A")

    # By entry type
    print(f"\n  --- By Entry Type ---")
    entry_types = set(t["entry_type"] for t in trades)
    for et in sorted(entry_types):
        et_trades = [t for t in trades if t["entry_type"] == et]
        et_wins = len([t for t in et_trades if t["result"] == "WIN"])
        et_pnl = sum(t["pnl"] for t in et_trades)
        print(f"  {et:<20} trades={len(et_trades):>4} wins={et_wins:>3} pnl=₹{et_pnl:>+10,.2f}")

    # Log to database
    log_trades_to_db(trades)

    print(f"\n✅ Backtest complete. Pattern matcher can now use this data.")
    print(f"   Run 'python check_pattern_progress.py' to verify.")


if __name__ == "__main__":
    main()

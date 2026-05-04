from app.state import STATE
from app.files import write_market_data, write_state
from app.config import FAST_EMA_PERIOD, SLOW_EMA_PERIOD
import pandas as pd

def safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def calculate_ema(candles, period):
    closes = []

    for candle in candles:
        if len(candle) > 4:
            close = safe_float(candle[4])
            if close is not None:
                closes.append(close)

    if not closes:
        return None

    # If not enough candles, return simple average of available closes
    if len(closes) < period:
        return sum(closes) / len(closes)

    ema = sum(closes[:period]) / period
    multiplier = 2 / (period + 1)

    for price in closes[period:]:
        ema = ((price - ema) * multiplier) + ema

    return ema


def calculate_vwap_series(candles):
    vwap_values = []

    total_pv = 0.0
    total_volume = 0.0
    current_date = None

    for candle in candles:
        if len(candle) < 6:
            vwap_values.append(None)
            continue

        candle_time = candle[0]
        candle_date = str(candle_time)[:10] if candle_time else None

        # Reset VWAP each day
        if current_date is None:
            current_date = candle_date

        if candle_date != current_date:
            total_pv = 0.0
            total_volume = 0.0
            current_date = candle_date

        high = safe_float(candle[2])
        low = safe_float(candle[3])
        close = safe_float(candle[4])

        volume = safe_float(candle[5], 0.0)
        if high is None or low is None or close is None:
            vwap_values.append(None)
            continue

        typical_price = (high + low + close) / 3.0

        # ✅ FIX: handle zero volume correctly
        if volume is None or volume <= 0:
            if total_volume <= 0:
                vwap_values.append(None)
            else:
                vwap_values.append(total_pv / total_volume)
            continue

        total_pv += typical_price * volume
        total_volume += volume

        if total_volume <= 0:
            vwap_values.append(None)
        else:
            vwap_values.append(total_pv / total_volume)
    return vwap_values


def fmt(value):
    return f"{value:.2f}" if value is not None else "None"


def update_indicators():
    candles = getattr(STATE, "latest_closed_candles", [])

    if not candles:
        print("No local candles yet")
        write_state("NO_LOCAL_CANDLES")
        return False

    min_required = max(FAST_EMA_PERIOD, SLOW_EMA_PERIOD)
    if len(candles) < min_required:
        print(f"Not enough local candles yet: {len(candles)}/{min_required}")
        write_market_data()
        write_state("NOT_ENOUGH_LOCAL_CANDLES")
        return False

    lookback = max(SLOW_EMA_PERIOD * 3, 50)
    recent_candles = candles[-lookback:] if len(candles) >= lookback else candles
    prev_candles = recent_candles[:-1] if len(recent_candles) > 1 else []

    fast_ema = calculate_ema(recent_candles, FAST_EMA_PERIOD)
    slow_ema = calculate_ema(recent_candles, SLOW_EMA_PERIOD)

    prev_fast_ema = calculate_ema(prev_candles, FAST_EMA_PERIOD)
    prev_slow_ema = calculate_ema(prev_candles, SLOW_EMA_PERIOD)

    
    from app.files import _read_market_df
    df = _read_market_df()
    atr = calculate_atr(candles)
    if df.empty:
        vwap = None
    else:
        vwap = df["vwap"].iloc[-1]

    # Write indicator series back into CSV for the restored/runtime candle range
    try:
        from app.files import _read_market_df, _write_market_df

        df = _read_market_df()

        if not df.empty:
            closes = [safe_float(c[4]) for c in candles]
            candle_times = [str(c[0]) for c in candles]

            ema20_series = []
            ema = None
            multiplier = 2 / (FAST_EMA_PERIOD + 1)

            for price in closes:
                if price is None:
                    ema20_series.append(None)
                    continue
                if ema is None:
                    ema = price
                else:
                    ema = ((price - ema) * multiplier) + ema
                ema20_series.append(ema)

            ema50_series = []
            ema = None
            multiplier = 2 / (SLOW_EMA_PERIOD + 1)

            for price in closes:
                if price is None:
                    ema50_series.append(None)
                    continue
                if ema is None:
                    ema = price
                else:
                    ema = ((price - ema) * multiplier) + ema
                ema50_series.append(ema)

            series_map = {}
            for i, candle_time in enumerate(candle_times):
                series_map[candle_time] = {
                    "ema20": ema20_series[i] if i < len(ema20_series) else None,
                    "ema50": ema50_series[i] if i < len(ema50_series) else None,
                    
                }

            df_time_str = df["time"].dt.strftime("%Y-%m-%d %H:%M:%S")

            for idx, t in enumerate(df_time_str):
                if t in series_map:
                    df.at[idx, "ema20"] = series_map[t]["ema20"]
                    df.at[idx, "ema50"] = series_map[t]["ema50"]
                   

            _write_market_df(df)

    except Exception as e:
        print(f"⚠️ Indicator CSV update failed: {e}")

    # ✅ EMA/ATR should update even if VWAP is invalid
    STATE.ema20 = fast_ema
    STATE.ema50 = slow_ema
    STATE.prev_ema20 = prev_fast_ema
    STATE.prev_ema50 = prev_slow_ema
    STATE.vwap = vwap
    STATE.atr = atr

    if fast_ema is None or slow_ema is None or atr is None:
        print("EMA/ATR not ready yet")
        write_market_data()
        write_state("INDICATORS_NOT_READY")
        return False

    if vwap is None:
        print("⚠️ VWAP invalid → EMA/ATR still updated")
    
    print(
        "Indicators Updated | "
        f"FAST_EMA({FAST_EMA_PERIOD})={fmt(STATE.ema20)} "
        f"SLOW_EMA({SLOW_EMA_PERIOD})={fmt(STATE.ema50)} "
        f"PrevFAST_EMA({FAST_EMA_PERIOD})={fmt(STATE.prev_ema20)} "
        f"PrevSLOW_EMA({SLOW_EMA_PERIOD})={fmt(STATE.prev_ema50)} "
        f"VWAP={fmt(STATE.vwap)} "
        f"ATR={fmt(STATE.atr)}"
    )

    print("FAST_EMA_PERIOD =", FAST_EMA_PERIOD)
    print("SLOW_EMA_PERIOD =", SLOW_EMA_PERIOD)

    write_market_data()
    write_state("INDICATORS_UPDATED")
    return True

def calculate_atr(candles, period=14):
    true_ranges = []

    for i in range(1, len(candles)):
        try:
            high = safe_float(candles[i][2])
            low = safe_float(candles[i][3])
            prev_close = safe_float(candles[i - 1][4])

            if high is None or low is None or prev_close is None:
                continue

            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )

            true_ranges.append(tr)

        except Exception:
            continue

    if not true_ranges:
        return None

    if len(true_ranges) < period:
        return sum(true_ranges) / len(true_ranges)

    # Simple ATR (you can later upgrade to Wilder)
    atr = sum(true_ranges[-period:]) / period
    return atr

from app.files import _read_market_df, _write_market_df

def recompute_indicators_full():
    df = _read_market_df()
    if df.empty:
        return

    df = df.sort_values("time").copy()

    df["ema20"] = df["close"].ewm(span=FAST_EMA_PERIOD, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=SLOW_EMA_PERIOD, adjust=False).mean()

    df["date"] = df["time"].dt.date
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3.0
    df["tpv"] = df["tp"] * df["volume"]

    df["cum_tpv"] = df.groupby("date")["tpv"].cumsum()
    df["cum_vol"] = df.groupby("date")["volume"].cumsum()
    df["vwap"] = df["cum_tpv"] / df["cum_vol"].replace(0, pd.NA)
    df["vwap"] = df.groupby("date")["vwap"].ffill()

    _write_market_df(df[["time","open","high","low","close","volume","ema20","ema50","vwap","signal"]])

    STATE.ema20 = df["ema20"].iloc[-1]
    STATE.ema50 = df["ema50"].iloc[-1]
    STATE.vwap = df["vwap"].iloc[-1]
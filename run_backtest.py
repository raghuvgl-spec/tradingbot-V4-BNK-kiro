import os
import pandas as pd
from backtest import backtest
from app.strategy import generate_signals


def load_market_data():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, "data", "market_data.csv")

    df = pd.read_csv(file_path)

    print("\n--- RAW COLUMNS ---")
    print(df.columns.tolist())

    print("\n--- RAW TOP 5 ---")
    print(df.head(5))

    # Clean column names
    df.columns = [str(c).strip().lower() for c in df.columns]

    required = ["time", "open", "high", "low", "close"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing required column: {col}")

    print("\n--- RAW TIME SAMPLE ---")
    print(df["time"].head(10).tolist())

    # Try multiple time formats
    parsed_time = pd.to_datetime(df["time"], format="%Y-%m-%d %H:%M:%S", errors="coerce")

    if parsed_time.isna().all():
        parsed_time = pd.to_datetime(df["time"], format="%d-%m-%Y %H:%M", errors="coerce")

    if parsed_time.isna().all():
        parsed_time = pd.to_datetime(df["time"], errors="coerce")

    df["time"] = parsed_time
    print("Min time:", df["time"].min())
    print("Max time:", df["time"].max())
    print("Total rows:", len(df))

    # Numeric conversion
    numeric_cols = ["open", "high", "low", "close"]

    if "volume" not in df.columns:
        df["volume"] = 1.0

    numeric_cols.append("volume")

    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["volume"] = df["volume"].fillna(1.0)

    print("\n--- PARSED SAMPLE ---")
    print(df[["time", "open", "high", "low", "close", "volume"]].head(10))

    print("\n--- NULL COUNTS BEFORE DROP ---")
    print(df[["time", "open", "high", "low", "close"]].isna().sum())

    df = df.dropna(subset=["time", "open", "high", "low", "close"]).copy()
    df = df.sort_values("time").reset_index(drop=True)

    return df


def add_indicators(df):
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    # Session-based VWAP
    df["trade_date"] = df["time"].dt.date
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical_price * df["volume"]

    df["cum_pv"] = pv.groupby(df["trade_date"]).cumsum()
    df["cum_vol"] = df["volume"].groupby(df["trade_date"]).cumsum()
    df["cum_vol"] = df["cum_vol"].replace(0, 1.0)
    df["vwap"] = df["cum_pv"] / df["cum_vol"]

    df.drop(columns=["trade_date", "cum_pv", "cum_vol"], inplace=True)

    return df
    
def main():
    df = load_market_data()

    print("\n--- CLEAN ROW COUNT ---")
    print(len(df))

    if df.empty:
        print("\nNo valid rows found in market_data.csv after parsing.")
        return

    # First indicators, then signals
    df = add_indicators(df)
    df = generate_signals(df)
    print("\n--- SIGNAL COUNTS ---")
    print(df["signal"].value_counts(dropna=False))
    
    buy_signals = df[df["signal"] == "BUY"]
    sell_signals = df[df["signal"] == "SELL"]
    
    print("\n--- DATA SAMPLE ---")
    print(df.head(5))

    print("\n--- LAST 10 ROWS ---")
    print(df[["time", "close", "ema20", "ema50", "vwap", "signal"]].tail(50))

    print("\n--- NULL COUNTS ---")
    print(df[["ema20", "ema50", "vwap"]].isna().sum())

    print("\n--- NON-NULL COUNTS ---")
    print(df[["ema20", "ema50", "vwap"]].notna().sum())

    bt_df = df.dropna(subset=["time", "ema20", "ema50", "vwap", "close"]).copy()

    print("\n--- ROWS AVAILABLE FOR BACKTEST ---")
    print(len(bt_df))

    result = backtest(bt_df, capital=200000, risk_per_trade_pct=2)

    print("\n--- SIGNAL COUNTS ---")
    print(df["signal"].value_counts(dropna=False))
    buy_signals = df[df["signal"] == "BUY"]
    sell_signals = df[df["signal"] == "SELL"]

    print("\n--- BUY SIGNAL SUMMARY ---")
    print("Total BUY signals:", len(buy_signals))
    if not buy_signals.empty:
        print("First BUY:", buy_signals["time"].iloc[0])
        print("Last BUY :", buy_signals["time"].iloc[-1])

    print("\n--- SELL SIGNAL SUMMARY ---")
    print("Total SELL signals:", len(sell_signals))
    if not sell_signals.empty:
        print("First SELL:", sell_signals["time"].iloc[0])
        print("Last SELL :", sell_signals["time"].iloc[-1])

    print("\n--- SIGNAL ROWS ---")
    print(df[df["signal"].notna()][["time", "close", "ema20", "ema50", "vwap", "signal"]].tail(50))
    print("\n--- DATA SAMPLE ---")
    print(df.head(5))

    print("\n--- LAST 10 ROWS ---")
    print(df[["time", "close", "ema20", "ema50", "vwap", "signal"]].tail(10))

    print("\n--- NULL COUNTS ---")
    print(df[["ema20", "ema50", "vwap"]].isna().sum())

    print("\n--- NON-NULL COUNTS ---")
    print(df[["ema20", "ema50", "vwap"]].notna().sum())

    bt_df = df.dropna(subset=["time", "ema20", "ema50", "vwap", "close"]).copy()

       
    print("\n--- ROWS AVAILABLE FOR BACKTEST ---")
    print(len(bt_df))
    result = backtest(bt_df, capital=200000, risk_per_trade_pct=2)
    print("\n--- BACKTEST RESULT ---")
    print(result)

    print("\n--- BACKTEST RESULT ---")
    print(result)

    if "Trades" in result and result["Trades"]:
        print("\n--- TRADE LOG ---")
        for t in result["Trades"]:
            print(t)


if __name__ == "__main__":
    main()
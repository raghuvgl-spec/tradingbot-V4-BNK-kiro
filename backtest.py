import pandas as pd


def backtest(df, capital=200000, risk_per_trade_pct=2, hold_candles=3):
    balance = capital
    peak = capital
    max_drawdown = 0

    trades = []
    position = None

    for i in range(len(df)):
        row = df.iloc[i]

        needed_cols = ["time", "close", "signal"]
        if any(pd.isna(row[col]) for col in needed_cols):
            continue

        signal = str(row["signal"]).upper() if row["signal"] is not None else ""
        price = float(row["close"])

        # ================= ENTRY =================
        if position is None:
            if signal in ["BUY", "SELL"]:
                risk_amount = balance * (risk_per_trade_pct / 100)

                position = {
                    "type": signal,
                    "entry": price,
                    "entry_time": row["time"],
                    "entry_index": i,
                    "capital_at_entry": balance,
                    "risk": risk_amount,
                }

                print(f"ENTRY {position['type']} at {row['time']} | price={price:.2f}")

        # ================= EXIT AFTER FIXED CANDLES =================
        elif position is not None:
            candles_held = i - position["entry_index"]

            if candles_held >= hold_candles:
                exit_price = price

                move = (
                    exit_price - position["entry"]
                    if position["type"] == "BUY"
                    else position["entry"] - exit_price
                )

                qty = position["risk"] / position["entry"]
                pnl = move * qty
                balance += pnl

                trades.append({
                    "type": position["type"],
                    "entry_time": position["entry_time"],
                    "exit_time": row["time"],
                    "entry": position["entry"],
                    "exit": exit_price,
                    "pnl": pnl,
                    "balance": balance,
                    "candles_held": candles_held,
                })

                print(
                    f"EXIT {position['type']} at {row['time']} | "
                    f"held={candles_held} | pnl={pnl:.2f}"
                )

                peak = max(peak, balance)
                dd = peak - balance
                max_drawdown = max(max_drawdown, dd)

                position = None

                # optional same-candle re-entry if signal still exists
                if signal in ["BUY", "SELL"]:
                    risk_amount = balance * (risk_per_trade_pct / 100)

                    position = {
                        "type": signal,
                        "entry": price,
                        "entry_time": row["time"],
                        "entry_index": i,
                        "capital_at_entry": balance,
                        "risk": risk_amount,
                    }

                    print(f"RE-ENTRY {position['type']} at {row['time']} | price={price:.2f}")

    # ================= FORCE EXIT AT END =================
    if position is not None:
        last_row = df.iloc[-1]
        last_price = float(last_row["close"])
        candles_held = len(df) - 1 - position["entry_index"]

        move = (
            last_price - position["entry"]
            if position["type"] == "BUY"
            else position["entry"] - last_price
        )

        qty = position["risk"] / position["entry"]
        pnl = move * qty
        balance += pnl

        trades.append({
            "type": position["type"],
            "entry_time": position["entry_time"],
            "exit_time": last_row["time"],
            "entry": position["entry"],
            "exit": last_price,
            "pnl": pnl,
            "balance": balance,
            "candles_held": candles_held,
        })

        print(
            f"FORCED EXIT {position['type']} at {last_row['time']} | "
            f"held={candles_held} | pnl={pnl:.2f}"
        )

        peak = max(peak, balance)
        dd = peak - balance
        max_drawdown = max(max_drawdown, dd)

        position = None

    total = len(trades)
    wins = len([t for t in trades if t["pnl"] > 0])
    losses = len([t for t in trades if t["pnl"] < 0])
    win_rate = (wins / total * 100) if total else 0
    net_profit = balance - capital

    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    max_drawdown_pct = (max_drawdown / capital * 100) if capital else 0

    return {
        "Starting Capital": capital,
        "Ending Capital": round(balance, 2),
        "Net Profit": round(net_profit, 2),
        "Total Trades": total,
        "Wins": wins,
        "Losses": losses,
        "Win Rate %": round(win_rate, 2),
        "Max Drawdown": round(max_drawdown, 2),
        "Max Drawdown %": round(max_drawdown_pct, 2),
        "Profit Factor": round(profit_factor, 2),
        "Trades": trades,
    }
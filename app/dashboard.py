import json
import time

import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from openpyxl import load_workbook

from app.config import BOT_CONTROL_FILE, BOT_STATE_FILE, MARKET_DATA_FILE, TRADE_LOG_FILE
from app.state import STATE

import streamlit as st
from streamlit_autorefresh import st_autorefresh
import subprocess
import sys
from pathlib import Path
from app.files import write_control, read_market_data
from app.state import STATE

st.set_page_config(page_title="Trading Bot Dashboard", layout="wide")
st_autorefresh(interval=3000, key="dashboard_refresh")

# ---------- CONTROLS ----------
st.subheader("Bot Controls")

VISIBLE_CANDLES = 40    
FUTURE_EMPTY_CANDLES = 20   # empty space on right side
CHART_HEIGHT = 900
#-------------------------------------------------------------------------------------------------
#Dashboard buttons (Start/Stop) update this file
#BOT_CONTROL_FILE is used to control the bot externally (start/stop) without killing the program.
#run_bot: True → bot is allowed to run
#run_bot: False → bot should stop safely
def ensure_control_file():
    if not BOT_CONTROL_FILE.exists():
        BOT_CONTROL_FILE.write_text(json.dumps({"run_bot": True}, indent=2))
#-------------------------------------------------------------------------------------------------
#ensure_control_file() → makes sure the file exists (creates it if missing)
#json.loads(...) → reads the file and returns the value (e.g., {"run_bot": True})
#It stays there permanently until changed (e.g., dashboard sets it to False or True again)
def read_control():
    ensure_control_file()
    try:
        return json.loads(BOT_CONTROL_FILE.read_text())	
    except Exception:
        return {"run_bot": True}
#-------------------------------------------------------------------------------------------------
#It writes/overwrites the file with True or False
def write_control(run_bot: bool):
    BOT_CONTROL_FILE.write_text(json.dumps({"run_bot": run_bot}, indent=2))

#--------------------------------------------------------------------------------------------------
#Functions like write_state() (or inside orders.py / bot.py) writes the Contant , Here reading Only
def read_state():
    
    default_state = {
        "ws_connected": False,
        "ltp": None,
        "trade_count": 0,
        "max_trades": 5,
        "current_position": None,
        "last_update": None,
        "status": "WAITING",
        "realized_pnl": 0,
        "consecutive_sl": 0,
        "bot_block_reason": None,
        "last_signal": None,
        "last_action": None,
        "live_candle": None,
    }

    if not BOT_STATE_FILE.exists():
        return default_state

    try:
        content = BOT_STATE_FILE.read_text().strip()
        if not content:
            return default_state | {"status": "STATE_EMPTY"}
        return json.loads(content)
    except Exception:
        return default_state | {"status": "STATE_READ_ERROR"}
#--------------------------------------------------------------------------------------------------
#--------------------------------------------------------------------------------------------------
#Excel → converted into DataFrame
def read_trade_log():
    if not TRADE_LOG_FILE.exists():
        return pd.DataFrame()

    try:
        wb = load_workbook(TRADE_LOG_FILE, data_only=True)
        ws = wb.active
        rows = list(ws.values)

        if not rows:
            return pd.DataFrame()

        headers = rows[0]
        data = rows[1:]
        return pd.DataFrame(data, columns=headers)

    except Exception:
        return pd.DataFrame()
#--------------------------------------------------------------------------------------------------
# Purpose of function Convert trade log (Excel) into chart markers (buy/sell points)
def get_trade_markers_from_log(df, trade_df):
    if df.empty or trade_df.empty:
        empty = pd.DataFrame()
        return empty, empty, empty, empty

    tdf = trade_df.copy()

    tdf["EntryTime"] = pd.to_datetime(tdf["EntryTime"], errors="coerce")
    tdf["ExitTime"] = pd.to_datetime(tdf["ExitTime"], errors="coerce")
    tdf["Instrument"] = tdf["Instrument"].astype(str).str.upper().str.strip()

    # keep only today's trades
    today = pd.Timestamp.now().date()
    tdf = tdf[
        (tdf["EntryTime"].dt.date == today) |
        (tdf["ExitTime"].dt.date == today)
    ].copy()

    if tdf.empty:
        empty = pd.DataFrame()
        return empty, empty, empty, empty

    def nearest_candle_row(ts):
        if pd.isna(ts) or df.empty:
            return None
        idx = (df["time"] - ts).abs().idxmin()
        return df.loc[idx]

    entry_rows = []
    exit_rows = []

    for _, row in tdf.iterrows():
        inst = str(row.get("Instrument", "")).upper().strip()

        entry_candle = nearest_candle_row(row.get("EntryTime"))
        if entry_candle is not None:
            entry_rows.append({
                "Instrument": inst,
                "marker_time": entry_candle["time"],
                "marker_y": float(entry_candle["low"]) - 10,   # buy marker below candle
            })

        exit_candle = nearest_candle_row(row.get("ExitTime"))
        if exit_candle is not None:
            exit_rows.append({
                "Instrument": inst,
                "marker_time": exit_candle["time"],
                "marker_y": float(exit_candle["high"]) + 10,  # sell marker above candle
            })

    entry_df = pd.DataFrame(entry_rows)
    exit_df = pd.DataFrame(exit_rows)

    ce_buy = entry_df[entry_df["Instrument"] == "CE"] if not entry_df.empty else pd.DataFrame()
    pe_buy = entry_df[entry_df["Instrument"] == "PE"] if not entry_df.empty else pd.DataFrame()

    ce_sell = exit_df[exit_df["Instrument"] == "CE"] if not exit_df.empty else pd.DataFrame()
    pe_sell = exit_df[exit_df["Instrument"] == "PE"] if not exit_df.empty else pd.DataFrame()

    return ce_buy, ce_sell, pe_buy, pe_sell
#---------------------------------------------------------------------------------------------------------

def get_signal_points(df):
    if df.empty or "signal" not in df.columns:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    signal_series = df["signal"].fillna("").astype(str).str.upper().str.strip()

    ce_buy_df = df[signal_series == "CE BUY"]
    ce_sell_df = df[signal_series == "CE SELL"]
    pe_buy_df = df[signal_series == "PE BUY"]
    pe_sell_df = df[signal_series == "PE SELL"]
    fut_buy_df = df[signal_series == "FUT BUY"]
    fut_sell_df = df[signal_series == "FUT SELL"]

    return ce_buy_df, ce_sell_df, pe_buy_df, pe_sell_df, fut_buy_df, fut_sell_df

def summary_stats(trades):
    if trades.empty or "PnL" not in trades.columns:
        return {"total": 0, "wins": 0, "losses": 0, "net": 0.0, "win_rate": 0.0}

    pnl = pd.to_numeric(trades["PnL"], errors="coerce").fillna(0)
    total = len(pnl)
    wins = int((pnl > 0).sum())
    losses = int((pnl < 0).sum())
    net = float(pnl.sum())
    win_rate = (wins / total * 100) if total else 0.0

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "net": net,
        "win_rate": win_rate,
    }


def get_live_current_candle_row():
    try:
        with STATE.lock:
            c = STATE.current_candle

            if not c:
                return None

            return {
                "time": pd.to_datetime(c["time"], errors="coerce"),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c.get("volume", 1)),
                "ema20": getattr(STATE, "ema20", None),
                "ema50": getattr(STATE, "ema50", None),
                "vwap": getattr(STATE, "vwap", None),            }
    except Exception:
        return None

def get_last_trade_levels(trade_df):
    if trade_df.empty:
        return None

    try:
        df = trade_df.copy()

        for col in ["Entry", "SL", "Target"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        if "EntryTime" in df.columns:
            df["EntryTime"] = pd.to_datetime(df["EntryTime"], errors="coerce")
            df = df.sort_values("EntryTime")

        df = df.dropna(subset=["Entry"], how="any")
        if df.empty:
            return None

        last_row = df.iloc[-1]

        return {
            "entry_price": last_row.get("Entry"),
            "sl": last_row.get("SL"),
            "target": last_row.get("Target"),
            "status": last_row.get("Status"),
            "entry_type": last_row.get("EntryType"),
        }
    except Exception:
        return None

def build_plot_df(df, state):
    plot_df = df.copy()
    live = state.get("live_candle")

    if not live:
        return plot_df

    try:
        live_df = pd.DataFrame([{
            "time": pd.to_datetime(live["time"], errors="coerce"),
            "open": live["open"],
            "high": live["high"],
            "low": live["low"],
            "close": live["close"],
            "volume": live.get("volume", 0),
        }])

        if not live_df.empty and pd.notna(live_df.iloc[0]["time"]):
            if not plot_df.empty and plot_df.iloc[-1]["time"] == live_df.iloc[0]["time"]:
                plot_df.iloc[-1, plot_df.columns.get_loc("open")] = live_df.iloc[0]["open"]
                plot_df.iloc[-1, plot_df.columns.get_loc("high")] = live_df.iloc[0]["high"]
                plot_df.iloc[-1, plot_df.columns.get_loc("low")] = live_df.iloc[0]["low"]
                plot_df.iloc[-1, plot_df.columns.get_loc("close")] = live_df.iloc[0]["close"]

                if "volume" in plot_df.columns:
                    plot_df.iloc[-1, plot_df.columns.get_loc("volume")] = live_df.iloc[0]["volume"]
            else:
                plot_df = pd.concat([plot_df, live_df], ignore_index=True)

    except Exception:
        return plot_df

    live_row = get_live_current_candle_row()
    if live_row is not None and pd.notna(live_row["time"]):
        today = pd.Timestamp.now().date()

        if live_row["time"].date() == today:
            if plot_df.empty:
                plot_df = pd.DataFrame([live_row])
            else:
                same_time = plot_df["time"].astype(str) == str(live_row["time"])

                if same_time.any():
                    idx = plot_df.index[same_time][-1]
                    for key, value in live_row.items():
                        if key in plot_df.columns:
                            if key == "signal":
                                continue
                            plot_df.at[idx, key] = value
                else:
                    plot_df = pd.concat([plot_df, pd.DataFrame([live_row])], ignore_index=True)

    if not plot_df.empty:
        plot_df = plot_df.sort_values("time").reset_index(drop=True)

    return plot_df

def get_xaxis_range(plot_df):
    if plot_df.empty:
        return None

    last_time = plot_df["time"].iloc[-1]
    candle_interval = pd.Timedelta(minutes=1)

    if len(plot_df) >= 2:
        diffs = plot_df["time"].diff().dropna()
        valid_diffs = diffs[diffs > pd.Timedelta(0)]
        if not valid_diffs.empty:
            candle_interval = valid_diffs.mode().iloc[0]

    future_space = candle_interval * FUTURE_EMPTY_CANDLES

    if len(plot_df) > VISIBLE_CANDLES:
        start_time = plot_df["time"].iloc[-VISIBLE_CANDLES]
    else:
        start_time = plot_df["time"].iloc[0]

    end_time = last_time + future_space
    return [start_time, end_time]

def get_yaxis_settings(plot_df, state):
    if plot_df.empty:
        return None

    y_min = float(plot_df["low"].min())
    y_max = float(plot_df["high"].max())

    if y_max - y_min < 50:   # 🔥 prevent flattening
        padding = 50
    else:
        padding = (y_max - y_min) * 0.2

    return {
        "range": [y_min - padding, y_max + padding]
    }

def build_chart(df, state,trade_df):
    plot_df = df.copy()
 #🔥LIMIT DATA HERE (THIS IS THE FIX)
    BUFFER_CANDLES = 20 
    plot_df = plot_df.tail(VISIBLE_CANDLES + FUTURE_EMPTY_CANDLES + BUFFER_CANDLES)
    plot_df = build_plot_df(plot_df, state)
    
    fig = go.Figure()
    if plot_df.empty:
        fig.update_layout(
            title="No market data yet",
            template="plotly_white",
            height=CHART_HEIGHT,
            bargap=0.5
        )
        return fig

    fig.add_trace(go.Candlestick(
        x=plot_df["time"],
        open=plot_df["open"],
        high=plot_df["high"],
        low=plot_df["low"],
        close=plot_df["close"],
        name="Candles",
        #increasing_line_width=4,
        #decreasing_line_width=4,
        whiskerwidth=0.0
    ))
    min_price = plot_df["low"].min()
    max_price = plot_df["high"].max()
    padding = (max_price - min_price) * 0.2
    

    if "ema20" in plot_df.columns and plot_df["ema20"].notna().any():
        fig.add_trace(go.Scatter(
            x=plot_df["time"],
            y=plot_df["ema20"],
            mode="lines",
            name="EMA20"
        ))

    if "ema50" in plot_df.columns:
        ema50 = plot_df["ema50"]

        # 🚀 Remove bad jumps
        ema50_clean = ema50.where(
            (ema50 > plot_df["low"] - 500) & (ema50 < plot_df["high"] + 500)
        )

        fig.add_trace(go.Scatter(
            x=plot_df["time"],
            y=ema50_clean,
            mode="lines",
            name="EMA50"
    ))

    if "vwap" in plot_df.columns:
        vwap = plot_df["vwap"]

        # 🚀 Remove bad jumps
        vwap_clean = vwap.where(
            (vwap > plot_df["low"] - 500) & (vwap < plot_df["high"] + 500)
        )

        fig.add_trace(go.Scatter(
            x=plot_df["time"],
            y=vwap_clean,
            mode="lines",
            name="VWAP"
    ))
    signal_ce_buy_df, signal_ce_sell_df, signal_pe_buy_df, signal_pe_sell_df, fut_buy_df, fut_sell_df = get_signal_points(plot_df)
    ce_buy_df, ce_sell_df, pe_buy_df, pe_sell_df = get_trade_markers_from_log(plot_df, trade_df)
    
    
    if not ce_buy_df.empty:
        fig.add_trace(go.Scatter(
            x=ce_buy_df["marker_time"],
            y=ce_buy_df["marker_y"],
            mode="markers+text",
            text=["CE BUY"] * len(ce_buy_df),
            textposition="top center",
            name="CE Buy",
            marker=dict(size=12, symbol="triangle-up")
        ))

    if not ce_sell_df.empty:
        fig.add_trace(go.Scatter(
            x=ce_sell_df["marker_time"],
            y=ce_sell_df["marker_y"],
            mode="markers+text",
            text=["CE SELL"] * len(ce_sell_df),
            textposition="bottom center",
            name="CE Sell",
            marker=dict(size=12, symbol="triangle-down")
        ))

    if not pe_buy_df.empty:
        fig.add_trace(go.Scatter(
            x=pe_buy_df["marker_time"],
            y=pe_buy_df["marker_y"],
            mode="markers+text",
            text=["PE BUY"] * len(pe_buy_df),
            textposition="top center",
            name="PE Buy",
            marker=dict(size=12, symbol="triangle-up")
        ))

    if not pe_sell_df.empty:
        fig.add_trace(go.Scatter(
            x=pe_sell_df["marker_time"],
            y=pe_sell_df["marker_y"],
            mode="markers+text",
            text=["PE SELL"] * len(pe_sell_df),
            textposition="bottom center",
            name="PE Sell",
            marker=dict(size=12, symbol="triangle-down")
        ))
        
    if not fut_buy_df.empty:
        fig.add_trace(go.Scatter(
            x=fut_buy_df["time"],
            y=fut_buy_df["low"] - 10,
            mode="markers+text",
            text=["FUT BUY"] * len(fut_buy_df),
            textposition="top center",
            name="FUT Buy",
            marker=dict(size=14, symbol="triangle-up", color="green")
    ))

    if not fut_sell_df.empty:
        fig.add_trace(go.Scatter(
            x=fut_sell_df["time"],
            y=fut_sell_df["high"] + 10,
            mode="markers+text",
            text=["FUT SELL"] * len(fut_sell_df),
            textposition="bottom center",
            name="FUT Sell",
            marker=dict(size=14, symbol="triangle-down", color="red")
        ))

    ltp = state.get("ltp")
    if ltp is not None:
        try:
            ltp = float(ltp)

            fig.add_hline(
                y=ltp,
                line_dash="dot",
                annotation_text=f"LTP: {ltp}",
                annotation_position="top right"
            )

            fig.add_trace(go.Scatter(
                x=[plot_df["time"].iloc[-1] + pd.Timedelta(minutes=1)],
                y=[ltp],
                mode="markers",
                marker=dict(size=10),
                name="Live Price"
            ))
        except Exception:
            pass

    pos = state.get("current_position")
    last_trade = get_last_trade_levels(trade_df)

    # Prefer live position. If no live position, show last closed/open trade from Excel.
    levels = pos if pos else last_trade

    if levels:
        try:
            line_suffix = ""
            if not pos:
                entry_type = levels.get("entry_type")
                line_suffix = f" | LAST TRADE" + (f" | {entry_type}" if entry_type else "")

            # 🟠 ENTRY LINE
            if levels.get("entry_price") is not None:
                fig.add_hline(
                    y=float(levels["entry_price"]),
                    line=dict(color="orange", width=2, dash="dash"),
                    annotation_text=f"ENTRY: {levels['entry_price']}{line_suffix}",
                    annotation_position="right"
                )

            # 🔴 SL LINE
            if levels.get("sl") is not None:
                fig.add_hline(
                    y=float(levels["sl"]),
                    line=dict(color="red", width=2, dash="dash"),
                    annotation_text=f"SL: {levels['sl']}{line_suffix}",
                    annotation_position="right"
                )

            # 🟢 TARGET LINE
            if levels.get("target") is not None:
                fig.add_hline(
                    y=float(levels["target"]),
                    line=dict(color="green", width=2, dash="dash"),
                    annotation_text=f"TARGET: {levels['target']}{line_suffix}",
                    annotation_position="right"
                )

        except Exception:
            pass
    yaxis_cfg = get_yaxis_settings(plot_df, state)
    fig.update_layout(
        title="BANKNIFTY Live Monitoring",
        xaxis_title="Time",
        yaxis_title="Price",
        template="plotly_white",
        xaxis_rangeslider_visible=False,
        height=CHART_HEIGHT,
        uirevision="fixed_dashboard",
        margin=dict(l=20, r=20, t=60, b=20),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1
    ),
    xaxis=dict(
        fixedrange=False,
        showgrid=True,
        tickmode="auto",
        nticks=20,
         rangeslider=dict(visible=True),
    ),
     yaxis=dict(
    fixedrange=False,
    range=yaxis_cfg["range"] if yaxis_cfg else None
    )
)

    if len(plot_df) > VISIBLE_CANDLES and "chart_initialized" not in st.session_state:
        fig.update_xaxes(
            range=[
                plot_df["time"].iloc[-VISIBLE_CANDLES],
                plot_df["time"].iloc[-1] + pd.Timedelta(minutes=FUTURE_EMPTY_CANDLES)
            ]
        )
        st.session_state["chart_initialized"] = True
    return fig


st.title("📊 Trading Bot Dashboard")

state = read_state()
state = read_state()
market_df = read_market_data()
trade_df = read_trade_log()
stats = summary_stats(trade_df)
live = state.get("live_candle")

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("WebSocket", "Connected" if state.get("ws_connected") else "Disconnected")
m2.metric("LTP", f"{float(state.get("ltp")):2f}" if state.get("ltp") is not None else "NA")
trade_count = state.get("trade_count", 0)
m3.metric("Trades", f"{trade_count} / {state.get('max_trades', 15)}")

m4.metric("Net PnL",f"{float(stats['net']):.2f}")
m5.metric("Win Rate", f"{stats['win_rate']:.1f}%")

if st.button("🔄 Refresh Now"):
    st.rerun()

market_df["time"] = pd.to_datetime(market_df["time"], errors="coerce")

today = pd.Timestamp.now().date()
market_df = market_df[market_df["time"].dt.date == today]

fig = build_chart(market_df, state, trade_df)

chart_placeholder = st.empty()
with chart_placeholder.container():
    st.plotly_chart(
        fig,
        width="stretch",
        config={"scrollZoom": True}
    )
    
st.subheader("Bot Controls")

BASE_DIR = Path(__file__).resolve().parent.parent
BOT_MANAGER = BASE_DIR / "bot_manager.py"

col1, col2, col3, col4 = st.columns(4)

with col1:
    if st.button("▶ Start / Resume", use_container_width=True):
        write_control(True)
        st.success("Bot resumed")

with col2:
    if st.button("⏸ Stop", use_container_width=True):
        write_control(False)
        st.warning("Bot stopped softly")

with col3:
    if st.button("🔄 Full Restart", use_container_width=True):
        result = subprocess.run(
            [sys.executable, str(BOT_MANAGER), "restart"],
            capture_output=True,
            text=True,
            cwd=str(BASE_DIR),
        )
        if result.returncode == 0:
            st.success("Bot fully restarted")
            if result.stdout.strip():
                st.caption(result.stdout.strip())
        else:
            st.error("Full restart failed")
            if result.stderr.strip():
                st.caption(result.stderr.strip())

with col4:
    if st.button("🧹 Reset Risk Block", use_container_width=True):
        # 1) Stop bot first so it cannot rewrite old state
        stop_result = subprocess.run(
            [sys.executable, str(BOT_MANAGER), "stop"],
            capture_output=True,
            text=True,
            cwd=str(BASE_DIR),
        )

        # 2) Read and reset saved state
        state_data = read_state()
        state_data["realized_pnl"] = 0.0
        state_data["consecutive_sl"] = 0
        state_data["bot_block_reason"] = None
        state_data["last_exit_time"] = None
        state_data["status"] = "RISK_RESET"
        BOT_STATE_FILE.write_text(json.dumps(state_data, indent=2))

        # 3) Start bot fresh
        start_result = subprocess.run(
            [sys.executable, str(BOT_MANAGER), "start"],
            capture_output=True,
            text=True,
            cwd=str(BASE_DIR),
        )

        if start_result.returncode == 0:
            st.success("Risk block reset and bot restarted cleanly")
            if start_result.stdout.strip():
                st.caption(start_result.stdout.strip())
        else:
            st.error("Reset restart failed")
            if start_result.stderr.strip():
                st.caption(start_result.stderr.strip())

st.caption("Start/Resume and Stop are soft controls. Full Restart reloads Python code changes.")

info1, info2, info3 = st.columns(3)

with info1:
    st.write("**Bot Switch:**", "RUNNING" if read_control().get("run_bot") else "STOPPED")
    st.write("**Status:**", state.get("status", "UNKNOWN"))
    st.write("**Last Signal:**", state.get("last_signal", "None"))
    st.write("**Last Action:**", state.get("last_action", "None"))

with info2:
    st.write("**Last Update:**", state.get("last_update", "NA"))
    st.write("**Block Reason:**", state.get("bot_block_reason", "None"))
    st.write("**Realized PnL:**", f"{float(state.get("realized_pnl", 0)):.2f}")
    st.write("**Consecutive SL:**", state.get("consecutive_sl", 0))

with info3:
    st.write("**Total Trades:**", stats["total"])
    st.write("**Wins:**", stats["wins"])
    st.write("**Losses:**", stats["losses"])
    st.write("**Win Rate:**", f"{stats['win_rate']:.1f}%")

#st.write("DEBUG current_position:", state.get("current_position"))
st.subheader("Current Position")
pos = state.get("current_position")
if pos:
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Symbol", pos.get("symbol", "NA"))
    p2.metric("Entry Price",f"{float(pos.get("entry_price")):.2f}" if pos.get("entry_price") is not None else "NA")
    p3.metric("SL", f"{float(pos.get("sl")):.2f}" if pos.get("sl") is not None else "NA")
    p4.metric("Status", pos.get("status", "NA"))
    
    p5, p6, p7, p8 = st.columns(4)        
        
    p5.metric("Qty", pos.get("remaining_qty") or pos.get("qty") or "NA")
    
    p6.metric(
        "Current LTP",
        f"{float(pos.get('current_ltp')):.2f}"
        if pos.get("current_ltp") is not None else "NA"
    )
    p7.metric("MTM Points", f"{float (pos.get("mtm_points")):.2f}" if pos.get("mtm_points") is not None else "NA")

    p8.metric("MTM PnL", f"{float (pos.get("mtm_pnl", "NA")):.2f}" if pos.get("mtm_pnl")is not None else "NA")
    with st.expander("Show full position details"):
        st.json(pos)
else:
    st.info("No open position")
st.subheader("Recent Trades")

if trade_df.empty:
    st.info("No trades logged yet")
else:
    st.dataframe(trade_df.tail(50), width="stretch")

st.subheader("Recent Market Data")
if market_df.empty:
    st.info("No market data yet")
else:
    st.dataframe(market_df.tail(50), width="stretch")



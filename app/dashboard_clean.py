import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from app.config import BOT_CONTROL_FILE, BOT_STATE_FILE, DATA_DIR
from app.state import STATE
from app.trade_db import get_connection


st.set_page_config(page_title="Trading Bot Dashboard", layout="wide")

# Reduce metric font size and remove top padding
st.markdown("""
<style>
[data-testid="stMetricValue"] { font-size: 1.1rem; }
[data-testid="stMetricLabel"] { font-size: 0.8rem; }
.block-container { padding-top: 1rem; }
</style>
""", unsafe_allow_html=True)

VISIBLE_CANDLES = 50
FUTURE_EMPTY_CANDLES = 10
CHART_HEIGHT = 950

# ------------------------------------------------------------------
# Instrument selector — maps display name to file tag
# ------------------------------------------------------------------
INSTRUMENT_OPTIONS = {
    "BANKNIFTY (Options)": "banknifty",
    "Crude Oil (Futures)": "crude",
}

# Sidebar dropdown
selected_label = st.sidebar.selectbox(
    "📊 Select Instrument",
    list(INSTRUMENT_OPTIONS.keys()),
    index=0
)
_TAG = INSTRUMENT_OPTIONS[selected_label]

MARKET_DATA_FILE = DATA_DIR / f"market_data_{_TAG}.csv"

# ------------------------------------------------------------------
def ensure_control_file():
    if not BOT_CONTROL_FILE.exists():
        BOT_CONTROL_FILE.write_text(json.dumps({"run_bot": True}, indent=2))

def _load_market_data():
    """Read market data from the currently selected instrument file."""
    if not MARKET_DATA_FILE.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(MARKET_DATA_FILE)
        if df.empty:
            return df
        df["time"] = pd.to_datetime(df["time"], errors="coerce", format="%Y-%m-%d %H:%M:%S")
        for col in ["open", "high", "low", "close", "ema20", "ema50", "vwap", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["time", "open", "high", "low", "close"])
        return df.sort_values("time").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()
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
#Database → converted into DataFrame (same column names as old Excel format)
def read_trade_log():
    """Read trades from SQLite database, returning a DataFrame with
    column names matching the legacy Excel format for dashboard compatibility."""
    try:
        conn = get_connection()
        query = """
            SELECT trade_id AS TradeID,
                   entry_time AS EntryTime,
                   exit_time AS ExitTime,
                   symbol AS Symbol,
                   instrument AS Instrument,
                   side AS Side,
                   qty AS Qty,
                   entry_price AS Entry,
                   exit_price AS Exit,
                   pnl AS PnL,
                   result AS Result,
                   reason AS Reason,
                   trade_count AS TradeCount,
                   status AS Status,
                   equity AS Equity,
                   atr AS ATR,
                   sl AS SL,
                   target AS Target,
                   entry_type AS EntryType,
                   duration_seconds AS DurationSec,
                   trade_mode AS TradeMode
              FROM trades
             ORDER BY entry_time
        """
        df = pd.read_sql_query(query, conn)
        return df
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

    # Use only the visible candles for initial Y-axis range
    visible = plot_df.tail(VISIBLE_CANDLES)
    y_min = float(visible["low"].min())
    y_max = float(visible["high"].max())

    # Include EMA lines in range calculation so they don't push candles to edges
    for col in ("ema20", "ema50"):
        if col in visible.columns and visible[col].notna().any():
            col_min = float(visible[col].dropna().min())
            col_max = float(visible[col].dropna().max())
            y_min = min(y_min, col_min)
            y_max = max(y_max, col_max)

    price_range = y_max - y_min
    if price_range < 30:
        padding = 20
    else:
        padding = price_range * 0.10  # 10% padding top and bottom

    return {
        "range": [y_min - padding, y_max + padding]
    }

def build_chart(df, state, trade_df):
    # Read from dashboard's selected instrument file
    csv_df = _load_market_data()
    if not csv_df.empty:
        plot_df = csv_df.copy()
    else:
        plot_df = df.copy() if df is not None and not df.empty else pd.DataFrame()

    # Keep enough candles for scrolling back through multiple days
    SCROLL_BACK_CANDLES = 2000
    plot_df = plot_df.tail(SCROLL_BACK_CANDLES)
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
        increasing_line_width=2,
        decreasing_line_width=2,
        whiskerwidth=0.0
    ))
    

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

    # if "vwap" in plot_df.columns:
    #     vwap = plot_df["vwap"]

    #     # 🚀 Remove bad jumps
    #     vwap_clean = vwap.where(
    #         (vwap > plot_df["low"] - 500) & (vwap < plot_df["high"] + 500)
    #     )

    #     fig.add_trace(go.Scatter(
    #         x=plot_df["time"],
    #         y=vwap_clean,
    #         mode="lines",
    #         name="VWAP"
    # ))
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
    if ltp is not None and not plot_df.empty:
        try:
            ltp = float(ltp)
            # Only show LTP if it's in the same price range as chart data
            chart_min = float(plot_df["low"].min())
            chart_max = float(plot_df["high"].max())
            chart_range = chart_max - chart_min
            if chart_min - chart_range < ltp < chart_max + chart_range:
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

    # Only show position lines if position matches selected instrument
    if pos:
        pos_sym = str(pos.get("symbol", "")).upper()
        if _TAG == "banknifty" and "BANKNIFTY" not in pos_sym:
            pos = None
        elif _TAG == "crude" and "CRUDE" not in pos_sym:
            pos = None

    # Skip last_trade levels for options (premium prices don't match index chart)
    levels = pos if pos else None

    if levels:
        try:
            line_suffix = ""
            if not pos:
                entry_type = levels.get("entry_type")
                line_suffix = f" | LAST TRADE" + (f" | {entry_type}" if entry_type else "")

            # Use index-level price for entry line
            entry_index = (
                levels.get("signal_price")
                or levels.get("desired_entry")
                or levels.get("entry_index_ltp")
            )
            entry_option = levels.get("entry_price")
            sl_val = levels.get("sl")
            target_val = levels.get("target")

            # Detect if SL/target are in option premium (not index scale)
            is_options = False
            if entry_option is not None and entry_index is not None:
                is_options = float(entry_option) < float(entry_index) * 0.5

            # 🟠 ENTRY LINE — always at index price
            if entry_index is not None:
                label = f"ENTRY: {float(entry_index):.2f}"
                if entry_option is not None:
                    label += f" (Premium: {float(entry_option):.2f})"
                fig.add_hline(
                    y=float(entry_index),
                    line=dict(color="orange", width=2, dash="dash"),
                    annotation_text=f"{label}{line_suffix}",
                    annotation_position="right"
                )

            # 🔴 SL LINE / 🟢 TARGET LINE — only for futures (same price scale)
            if not is_options:
                if sl_val is not None:
                    fig.add_hline(
                        y=float(sl_val),
                        line=dict(color="red", width=2, dash="dash"),
                        annotation_text=f"SL: {float(sl_val):.2f}{line_suffix}",
                        annotation_position="right"
                    )
                if target_val is not None:
                    fig.add_hline(
                        y=float(target_val),
                        line=dict(color="green", width=2, dash="dash"),
                        annotation_text=f"TARGET: {float(target_val):.2f}{line_suffix}",
                        annotation_position="right"
                    )

        except Exception:
            pass
    yaxis_cfg = get_yaxis_settings(plot_df, state)

    # uirevision controls scroll persistence:
    # - Same value across refreshes = user scroll/zoom preserved
    # - Changed value (via Live button) = reset to latest candles
    ui_rev = st.session_state.get("_chart_ui_rev", 0)

    # Always compute the "live" range for latest candles
    if not plot_df.empty:
        x_range = [
            plot_df["time"].iloc[-VISIBLE_CANDLES] if len(plot_df) > VISIBLE_CANDLES else plot_df["time"].iloc[0],
            plot_df["time"].iloc[-1] + pd.Timedelta(minutes=FUTURE_EMPTY_CANDLES)
        ]
        y_range = yaxis_cfg["range"] if yaxis_cfg else None
    else:
        x_range = None
        y_range = None

    # Holiday gap detection disabled — Plotly rangebreaks cause axis label issues
    _holiday_breaks = []

    fig.update_layout(
        title=f"{selected_label} Live Monitoring",
        xaxis_title="Time",
        yaxis_title="Price",
        template="plotly_white",
        xaxis_rangeslider_visible=False,
        height=CHART_HEIGHT,
        uirevision=str(ui_rev),
        margin=dict(l=20, r=20, t=60, b=20),
        paper_bgcolor="white",
        plot_bgcolor="white",
        shapes=[
            dict(
                type="rect",
                xref="paper", yref="paper",
                x0=0, y0=0, x1=0, y1=0,
                line=dict(color="black", width=0),
            )
        ],
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
        gridcolor="rgba(100,100,100,0.4)",
        tickmode="auto",
        nticks=20,
        rangebreaks=[
            dict(bounds=["sat", "mon"], pattern="day of week"),
        ] + ([dict(bounds=[15.5, 9.25], pattern="hour")] if _TAG == "banknifty" else [dict(bounds=[23.5, 9.0], pattern="hour")])
        + _holiday_breaks,
        range=x_range,
    ),
    yaxis=dict(
        fixedrange=False,
        range=y_range,
        showgrid=True,
        gridcolor="rgba(100,100,100,0.4)",
    ),
    dragmode="pan",
)

    return fig


# ======================================================================
# MAIN DASHBOARD LAYOUT
# ======================================================================

def _fragment(run_every="2s"):
    """Use Streamlit fragment refresh when available; otherwise run once."""
    if hasattr(st, "fragment"):
        return st.fragment(run_every=run_every)
    def decorator(func):
        return func
    return decorator

@_fragment(run_every="3s")
def live_top_section():
    """Refresh only metrics + chart, not the whole page."""
    state = read_state()
    market_df = _load_market_data()
    trade_df = read_trade_log()
    stats = summary_stats(trade_df)
    pos = state.get("current_position")
    # Filter position to match selected instrument
    if pos:
        pos_sym = str(pos.get("symbol", "")).upper()
        if _TAG == "banknifty" and "BANKNIFTY" not in pos_sym:
            pos = None
        elif _TAG == "crude" and "CRUDE" not in pos_sym:
            pos = None

    # ─── ROW 1: Key metrics ──────────────────────────────────────────────────
    m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
    m1.metric("WebSocket", "🟢" if state.get("ws_connected") else "🔴")
    m2.metric("LTP", f"{float(state.get('ltp')):.2f}" if state.get("ltp") is not None else "NA")
    m3.metric("Trades", f"{state.get('trade_count', 0)} / {state.get('max_trades', 15)}")
    m4.metric("Net PnL", f"{float(stats['net']):.2f}")
    m5.metric("Win Rate", f"{stats['win_rate']:.1f}%")
    m6.metric("Status", state.get("status", "—"))
    m7.metric("MTM PnL", f"{float(pos.get('mtm_pnl')):.2f}" if pos and pos.get("mtm_pnl") is not None else "—")

    # ─── ROW 2: Position details ─────────────────────────────────────────────
    if pos:
        p1, p2, p3, p4, p5, p6 = st.columns(6)
        sym = pos.get("symbol", "—")
        p1.metric("Symbol", sym)
        p2.metric("Qty", pos.get("remaining_qty") or pos.get("qty") or "—")
        p3.metric("Entry", f"{float(pos.get('entry_price')):.2f}" if pos.get("entry_price") is not None else "—")
        p4.metric("SL", f"{float(pos.get('sl')):.2f}" if pos.get("sl") is not None else "—")
        p5.metric("Cur LTP", f"{float(pos.get('current_ltp')):.2f}" if pos.get("current_ltp") is not None else "—")
        p6.metric("MTM Pts", f"{float(pos.get('mtm_points')):.2f}" if pos.get("mtm_points") is not None else "—")

    st.divider()
    with st.container():
        chart_hdr, live_btn_col = st.columns([10, 1])
        with chart_hdr:
            st.markdown(f"<p style='font-size:0.9rem; margin:0;'>📈 {selected_label} Live Chart</p>", unsafe_allow_html=True)
        with live_btn_col:
            if st.button("📍Live", key="go_live_btn", help="Snap to latest candles"):
                st.session_state["_chart_ui_rev"] = st.session_state.get("_chart_ui_rev", 0) + 1
        fig = build_chart(market_df, state, trade_df)
        st.plotly_chart(
            fig,
            width="stretch",
            config={"scrollZoom": True, "displayModeBar": True},
            key="live_banknifty_chart"
        )
    st.divider()

@_fragment(run_every="3s")
def live_details_section():
    """Refresh state-dependent details without a full dashboard reload."""
    state = read_state()
    market_df = _load_market_data()
    trade_df = read_trade_log()
    stats = summary_stats(trade_df)

    if not market_df.empty and "time" in market_df.columns:
        market_df = market_df.copy()
        market_df["time"] = pd.to_datetime(market_df["time"], errors="coerce")
        today = pd.Timestamp.now().date()
        market_df = market_df[market_df["time"].dt.date == today].copy()

    info1, info2, info3 = st.columns(3)
    with info1:
        st.write("**Bot Switch:**", "RUNNING" if read_control().get("run_bot") else "STOPPED")
        st.write("**Last Signal:**", state.get("last_signal", "None"))
        st.write("**Last Action:**", state.get("last_action", "None"))
    with info2:
        st.write("**Block Reason:**", state.get("bot_block_reason", "None"))
        st.write("**Realized PnL:**", f"{float(state.get('realized_pnl', 0)):.2f}")
        st.write("**Consecutive SL:**", state.get("consecutive_sl", 0))
    with info3:
        st.write("**Total Trades:**", stats["total"])
        st.write("**Wins:**", stats["wins"])
        st.write("**Losses:**", stats["losses"])

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

st.markdown("#### 📊 Trading Bot Dashboard")
live_top_section()

st.subheader("Bot Controls")
BASE_DIR = Path(__file__).resolve().parent.parent
BOT_MANAGER = BASE_DIR / "bot_manager.py"
col1, col2, col3, col4, col5 = st.columns(5)

with col1:
    if st.button("▶ Start / Resume", width="stretch"):
        write_control(True)
        st.success("Bot resumed")
with col2:
    if st.button("⏸ Stop", width="stretch"):
        write_control(False)
        st.warning("Bot stopped softly")
with col3:
    if st.button("🔄 Full Restart", width="stretch"):
        result = subprocess.run([sys.executable, str(BOT_MANAGER), "restart"], capture_output=True, text=True, cwd=str(BASE_DIR))
        if result.returncode == 0:
            st.success("Bot fully restarted")
        else:
            st.error("Full restart failed")
with col4:
    if st.button("🧹 Reset Risk Block", width="stretch"):
        subprocess.run([sys.executable, str(BOT_MANAGER), "stop"], capture_output=True, text=True, cwd=str(BASE_DIR))
        state_data = read_state()
        state_data["realized_pnl"] = 0.0
        state_data["consecutive_sl"] = 0
        state_data["bot_block_reason"] = None
        state_data["last_exit_time"] = None
        state_data["status"] = "RISK_RESET"
        BOT_STATE_FILE.write_text(json.dumps(state_data, indent=2))
        start_result = subprocess.run([sys.executable, str(BOT_MANAGER), "start"], capture_output=True, text=True, cwd=str(BASE_DIR))
        if start_result.returncode == 0:
            st.success("Risk reset done")
        else:
            st.error("Reset failed")
with col5:
    if st.button("🔄 Refresh", width="stretch"):
        st.rerun()

st.caption("Start/Resume and Stop are soft controls. Full Restart reloads Python code changes.")
live_details_section()

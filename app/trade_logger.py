"""
SQLite-backed trade logging — drop-in replacement for logger_excel.py.

Exposes the same public API so that ``orders.py`` only needs an import
change::

    # old
    from app.logger_excel import log_trade_entry, log_trade_exit
    # new
    from app.trade_logger import log_trade_entry, log_trade_exit

Uses ``app.trade_db.get_connection()`` for all database access and
``app.condition_snapshot`` for market-condition snapshots at entry/exit.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app import trade_db
from app import condition_snapshot as cs

logger = logging.getLogger(__name__)

# Symbols accepted by the Trade Intelligence system (Requirement 8.4)
_VALID_SYMBOLS = {"BANKNIFTY", "NIFTY"}


# ------------------------------------------------------------------
# INSERT helpers
# ------------------------------------------------------------------

_INSERT_TRADE = """
INSERT OR IGNORE INTO trades (
    trade_id, entry_time, symbol, instrument, side, qty,
    entry_price, trade_count, reason, status, atr, sl, target, entry_type, trade_mode
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
"""

_INSERT_SNAPSHOT = """
INSERT INTO condition_snapshots (
    trade_id, snapshot_type,
    ema9, ema21, ema_gap, ema_gap_atr,
    ema_cycle_phase, phase_duration,
    price_distance_ema9, price_distance_ema21,
    price_dist_ema9_atr, price_dist_ema21_atr,
    open_dist_ema9, open_dist_ema9_atr,
    price_ema9_momentum, price_zone,
    candle_open, candle_high, candle_low, candle_close,
    candle_body_ratio, candle_range_atr,
    atr_value, entry_type, time_of_day, index_ltp,
    trade_result, exit_reason,
    leading_phase, leading_phase_duration, phase_alignment
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_TRADE_EXIT = """
UPDATE trades
   SET exit_time        = ?,
       exit_price       = ?,
       pnl              = ?,
       result           = ?,
       reason           = ?,
       status           = ?,
       duration_seconds = ?,
       peak_profit      = ?,
       left_on_table    = ?,
       capture_pct      = ?,
       highest_price    = ?,
       lowest_price     = ?
 WHERE trade_id = ?
"""


# ------------------------------------------------------------------
# Public API — matches logger_excel.py signatures exactly
# ------------------------------------------------------------------


def log_trade_entry(
    trade_id,
    symbol,
    instrument,
    side,
    qty,
    entry_price,
    trade_count,
    reason="Trade opened",
    atr=None,
    sl=None,
    target=None,
    entry_type=None,
    trade_mode="LIVE",
):
    """Insert a trade record and an ENTRY condition snapshot into the DB.

    Rejects symbols not in {BANKNIFTY, NIFTY} (Requirement 8.4).
    Uses ``INSERT OR IGNORE`` for duplicate prevention (Requirement 2.3).

    trade_mode: LIVE (real trade), PAPER (paper-first validation),
                SHADOW (signal tracked but not traded).
    """
    try:
        # --- Symbol validation ---
        sym_upper = str(symbol).upper() if symbol else ""
        if not any(valid in sym_upper for valid in _VALID_SYMBOLS):
            logger.warning(
                "Rejected trade %s — invalid symbol '%s' (expected BANKNIFTY or NIFTY)",
                trade_id, symbol,
            )
            return

        conn = trade_db.get_connection()
        entry_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            _INSERT_TRADE,
            (
                str(trade_id),
                entry_time,
                sym_upper,
                instrument,
                side,
                qty,
                entry_price,
                trade_count,
                reason,
                atr,
                sl,
                target,
                entry_type,
                trade_mode,
            ),
        )
        conn.commit()

        # --- Capture ENTRY condition snapshot ---
        try:
            # Build a lightweight candle-like object from the STATE so
            # condition_snapshot.capture() can read OHLC / EMA / ATR.
            from app.state import STATE

            candle = _build_candle_proxy(STATE, atr)
            snap = cs.capture(
                trade_id=str(trade_id),
                snapshot_type="ENTRY",
                candle=candle,
                entry_type=entry_type or "",
            )
            conn.execute(_INSERT_SNAPSHOT, cs.to_db_row(snap))
            conn.commit()
        except Exception:
            logger.exception("Failed to capture ENTRY snapshot for trade %s", trade_id)

    except Exception:
        logger.exception("log_trade_entry failed for trade %s", trade_id)


def log_trade_exit(trade_id, exit_price, result, reason, status="CLOSED"):
    """Update the trade record with exit data and insert an EXIT snapshot.

    Captures the EXIT snapshot *before* updating the trade row so both
    records reference consistent state (Requirement 4.3).
    """
    try:
        conn = trade_db.get_connection()

        # --- Look up the original trade for PnL / duration computation ---
        row = conn.execute(
            "SELECT entry_time, entry_price, qty, side, entry_type, atr FROM trades WHERE trade_id = ?",
            (str(trade_id),),
        ).fetchone()

        if row is None:
            logger.warning("log_trade_exit: trade_id %s not found in DB", trade_id)
            return

        entry_time_str, entry_price, qty, side, entry_type, atr = row

        # --- Capture EXIT condition snapshot BEFORE updating trade ---
        try:
            from app.state import STATE

            candle = _build_candle_proxy(STATE, atr)
            snap = cs.capture(
                trade_id=str(trade_id),
                snapshot_type="EXIT",
                candle=candle,
                entry_type=entry_type or "",
                trade_result=result,
                exit_reason=reason,
            )
            conn.execute(_INSERT_SNAPSHOT, cs.to_db_row(snap))
            conn.commit()
        except Exception:
            logger.exception("Failed to capture EXIT snapshot for trade %s", trade_id)

        # --- Compute PnL ---
        entry_price = float(entry_price) if entry_price is not None else 0.0
        qty = float(qty) if qty is not None else 0.0
        side = str(side).upper() if side else "BUY"

        if side == "BUY":
            pnl = (float(exit_price) - entry_price) * qty
        else:
            pnl = (entry_price - float(exit_price)) * qty

        pnl = round(pnl, 2)

        # --- Compute peak profit and left-on-table ---
        from app.state import STATE as _state
        pos = _state.current_position or {}
        highest = float(pos.get("highest_price", exit_price))
        lowest = float(pos.get("lowest_price", exit_price))

        if side == "BUY":
            peak_profit = round((highest - entry_price) * qty, 2)
        else:
            peak_profit = round((entry_price - lowest) * qty, 2)

        left_on_table = round(peak_profit - pnl, 2) if peak_profit > pnl else 0.0
        capture_pct = round((pnl / peak_profit * 100), 2) if peak_profit > 0 else 0.0

        # --- Compute duration ---
        exit_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            entry_dt = datetime.strptime(str(entry_time_str), "%Y-%m-%d %H:%M:%S")
            exit_dt = datetime.strptime(exit_time_str, "%Y-%m-%d %H:%M:%S")
            duration_seconds = int((exit_dt - entry_dt).total_seconds())
        except Exception:
            duration_seconds = None

        # --- Update trade record ---
        conn.execute(
            _UPDATE_TRADE_EXIT,
            (
                exit_time_str,
                float(exit_price),
                pnl,
                result,
                reason,
                status,
                duration_seconds,
                peak_profit,
                left_on_table,
                capture_pct,
                highest,
                lowest,
                str(trade_id),
            ),
        )
        conn.commit()

        # Recompute metrics after every exit (mirrors logger_excel behaviour)
        update_metrics()

    except Exception:
        logger.exception("log_trade_exit failed for trade %s", trade_id)


# ------------------------------------------------------------------
# Metrics
# ------------------------------------------------------------------


def update_metrics():
    """Compute trading metrics from all closed trades and return as dict.

    Metrics: total_trades, wins, losses, win_rate_percent, net_pnl,
    average_win, average_loss, profit_factor, max_drawdown.
    """
    return _compute_metrics()


def get_metrics(
    date_from: str | None = None,
    date_to: str | None = None,
    instrument: str | None = None,
    entry_type: str | None = None,
) -> dict:
    """Filtered version of metrics computation.

    Parameters are optional — only supplied filters are applied.
    """
    return _compute_metrics(
        date_from=date_from,
        date_to=date_to,
        instrument=instrument,
        entry_type=entry_type,
    )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _compute_metrics(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    instrument: str | None = None,
    entry_type: str | None = None,
) -> dict:
    """Core metrics computation with optional filters."""
    empty = {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate_percent": 0.0,
        "net_pnl": 0.0,
        "average_win": 0.0,
        "average_loss": 0.0,
        "profit_factor": float("inf"),
        "max_drawdown": 0.0,
    }

    try:
        conn = trade_db.get_connection()

        # Build query with optional filters
        clauses = ["status = 'CLOSED'", "pnl IS NOT NULL", "trade_mode = 'LIVE'"]
        params: list = []

        if date_from is not None:
            clauses.append("entry_time >= ?")
            params.append(str(date_from))
        if date_to is not None:
            clauses.append("entry_time <= ?")
            params.append(str(date_to))
        if instrument is not None:
            clauses.append("instrument = ?")
            params.append(str(instrument))
        if entry_type is not None:
            clauses.append("entry_type = ?")
            params.append(str(entry_type))

        where = " AND ".join(clauses)
        query = f"SELECT pnl FROM trades WHERE {where} ORDER BY entry_time"

        rows = conn.execute(query, params).fetchall()

        if not rows:
            return empty

        pnls = [float(r[0]) for r in rows]

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        total_trades = len(pnls)
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / total_trades) * 100 if total_trades else 0.0
        net_pnl = sum(pnls)
        avg_win = sum(wins) / win_count if wins else 0.0
        avg_loss = sum(losses) / loss_count if losses else 0.0
        profit_factor = (
            abs(sum(wins) / sum(losses)) if losses else float("inf")
        )

        # Max drawdown: largest peak-to-trough decline in cumulative PnL
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        # Also update the equity column on each trade row (mirrors Excel behaviour)
        try:
            _update_equity_column(conn, where, params)
        except Exception:
            logger.debug("Equity column update skipped", exc_info=True)

        return {
            "total_trades": total_trades,
            "wins": win_count,
            "losses": loss_count,
            "win_rate_percent": round(win_rate, 2),
            "net_pnl": round(net_pnl, 2),
            "average_win": round(avg_win, 2),
            "average_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else float("inf"),
            "max_drawdown": round(max_dd, 2),
        }

    except Exception:
        logger.exception("_compute_metrics failed")
        return empty


def _update_equity_column(conn, where_clause: str, params: list) -> None:
    """Write running equity into the trades table (informational)."""
    query = f"SELECT trade_id, pnl FROM trades WHERE {where_clause} ORDER BY entry_time"
    rows = conn.execute(query, params).fetchall()
    running = 0.0
    for trade_id, pnl in rows:
        running += float(pnl)
        conn.execute(
            "UPDATE trades SET equity = ? WHERE trade_id = ?",
            (round(running, 2), trade_id),
        )
    conn.commit()


class _CandleProxy:
    """Lightweight stand-in for a candle object so that
    ``condition_snapshot.capture()`` can read the fields it needs.
    """

    def __init__(self, state, atr_override=None):
        # Prefer the latest candle data from STATE
        self.open = getattr(state, "candle_open", None)
        self.high = getattr(state, "candle_high", None)
        self.low = getattr(state, "candle_low", None)
        self.close = getattr(state, "candle_close", None) or getattr(state, "ltp", None)
        self.fast_ema = getattr(state, "ema9", None) or getattr(state, "ema20", None)
        self.slow_ema = getattr(state, "ema21", None) or getattr(state, "ema50", None)
        self.atr = atr_override if atr_override is not None else getattr(state, "atr", None)
        self.time = getattr(state, "candle_time", None) or datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _build_candle_proxy(state, atr_override=None):
    """Create a candle-like object from RuntimeState for snapshot capture."""
    return _CandleProxy(state, atr_override)

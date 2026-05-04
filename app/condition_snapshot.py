"""
Condition snapshot capture and serialization for the Trade Intelligence System.

Reads current market state from RuntimeState and the candle to build a
structured snapshot of all market conditions at trade entry or exit.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Optional

from app.state import STATE

logger = logging.getLogger(__name__)


@dataclass
class ConditionSnapshot:
    trade_id: str
    snapshot_type: str              # "ENTRY" or "EXIT"
    ema9: Optional[float]
    ema21: Optional[float]
    ema_gap: Optional[float]
    ema_gap_atr: Optional[float]    # abs(ema9 - ema21) / ATR
    ema_cycle_phase: Optional[str]  # SIDEWAYS / EXPANDING / PEAK / CONTRACTING
    phase_duration: Optional[int]   # candles in current phase
    price_distance_ema9: Optional[float]
    price_distance_ema21: Optional[float]
    price_dist_ema9_atr: Optional[float]
    price_dist_ema21_atr: Optional[float]
    open_dist_ema9: Optional[float]         # open - EMA9 (signed)
    open_dist_ema9_atr: Optional[float]     # open distance in ATR multiples
    price_ema9_momentum: Optional[str]   # EXPANDING / SHRINKING / FLAT
    price_zone: Optional[str]       # OVEREXTENDED / STRETCHED / NORMAL / NEAR_EMA9
    candle_open: Optional[float]
    candle_high: Optional[float]
    candle_low: Optional[float]
    candle_close: Optional[float]
    candle_body_ratio: Optional[float]   # abs(close-open)/(high-low), 0.0 if range=0
    candle_range_atr: Optional[float]    # (high-low)/ATR
    atr_value: Optional[float]
    entry_type: Optional[str]
    time_of_day: Optional[str]      # HH:MM format
    index_ltp: Optional[float]
    # Exit-only fields
    trade_result: Optional[str]     # WIN / LOSS / FLAT (exit only)
    exit_reason: Optional[str]      # (exit only)
    # Leading phase fields
    leading_phase: Optional[str]    # SIDEWAYS / EXPANDING_UP / PEAKED_UP / COMPRESSING_DOWN / CROSSED_DOWN / EXPANDING_DOWN / PEAKED_DOWN / COMPRESSING_UP / CROSSED_UP
    leading_phase_duration: Optional[int]  # candles in current leading phase
    phase_alignment: Optional[str]  # ALIGNED_UP / ALIGNED_DOWN / LEADING_AHEAD / LAGGING_BEHIND / DIVERGING / NEUTRAL


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _safe_getattr(obj, attr, default=None):
    """Read an attribute from *obj*, returning *default* on any error."""
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


def _compute_ema_gap_atr(ema9: Optional[float], ema21: Optional[float],
                         atr: Optional[float]) -> Optional[float]:
    if ema9 is None or ema21 is None or atr is None:
        return None
    if atr == 0.0:
        return 0.0
    return abs(ema9 - ema21) / atr


def _compute_body_ratio(open_: Optional[float], high: Optional[float],
                        low: Optional[float], close: Optional[float]) -> Optional[float]:
    if any(v is None for v in (open_, high, low, close)):
        return None
    candle_range = high - low
    if candle_range <= 0:
        return 0.0
    return abs(close - open_) / candle_range


def _compute_range_atr(high: Optional[float], low: Optional[float],
                       atr: Optional[float]) -> Optional[float]:
    if any(v is None for v in (high, low, atr)):
        return None
    if atr == 0.0:
        return 0.0
    return (high - low) / atr


def _compute_phase_duration(phase: Optional[str]) -> Optional[int]:
    """Return the number of consecutive candles in the current EMA cycle phase."""
    if phase is None:
        return None
    expanding = _safe_getattr(STATE, "ema_gap_expanding_count", 0)
    contracting = _safe_getattr(STATE, "ema_gap_contracting_count", 0)
    if phase == "EXPANDING":
        return expanding or 0
    if phase in ("CONTRACTING", "PEAK"):
        return contracting or 0
    # SIDEWAYS
    return 0


def _compute_phase_alignment(leading: Optional[str], lagging: Optional[str]) -> Optional[str]:
    """Derive the alignment between leading and lagging phases.

    Returns:
      ALIGNED_UP     – both expanding/trending upward
      ALIGNED_DOWN   – both expanding/trending downward
      LEADING_AHEAD  – leading peaked/compressing but lagging still expanding
      LAGGING_BEHIND – lagging peaked/contracting, leading already crossed/sideways
      DIVERGING      – phases moving in opposite directions
      NEUTRAL        – both sideways or indeterminate
    """
    if leading is None or lagging is None:
        return None

    leading_bullish = leading in ("EXPANDING_UP", "CROSSED_UP")
    leading_bearish = leading in ("EXPANDING_DOWN", "CROSSED_DOWN")
    leading_fading_up = leading in ("PEAKED_UP", "COMPRESSING_DOWN")
    leading_fading_down = leading in ("PEAKED_DOWN", "COMPRESSING_UP")

    lagging_expanding = lagging == "EXPANDING"
    lagging_fading = lagging in ("PEAK", "CONTRACTING")
    lagging_sideways = lagging == "SIDEWAYS"

    if leading_bullish and lagging_expanding:
        return "ALIGNED_UP"
    if leading_bearish and lagging_expanding:
        return "ALIGNED_DOWN"
    if leading_fading_up and lagging_expanding:
        return "LEADING_AHEAD"
    if leading_fading_down and lagging_expanding:
        return "LEADING_AHEAD"
    if leading in ("SIDEWAYS", "CROSSED_UP", "CROSSED_DOWN") and lagging_fading:
        return "LAGGING_BEHIND"
    if (leading_bullish and lagging_fading) or (leading_bearish and lagging_fading):
        return "DIVERGING"
    if leading == "SIDEWAYS" and lagging_sideways:
        return "NEUTRAL"

    return "NEUTRAL"


def _compute_price_ema9_momentum() -> Optional[str]:
    """Determine whether price distance from EMA9 is expanding, shrinking, or flat."""
    current = _safe_getattr(STATE, "price_dist_ema9")
    prev = _safe_getattr(STATE, "prev_price_dist_ema9")
    if current is None or prev is None:
        return None
    diff = abs(current) - abs(prev)
    if diff > 0.01:
        return "EXPANDING"
    if diff < -0.01:
        return "SHRINKING"
    return "FLAT"


def _extract_time_of_day(candle_time: Optional[str]) -> Optional[str]:
    """Extract HH:MM from a candle time string.

    Handles common formats:
      - "2024-01-15 09:30:00"
      - "09:30:00"
      - "09:30"
    """
    if candle_time is None:
        return None
    try:
        t = str(candle_time).strip()
        # If there's a space, take the time part after the last space
        if " " in t:
            t = t.rsplit(" ", 1)[-1]
        # Now t should be HH:MM or HH:MM:SS
        parts = t.split(":")
        if len(parts) >= 2:
            return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"
        return t
    except Exception:
        return None


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def capture(
    trade_id: str,
    snapshot_type: str,
    candle,
    entry_type: str,
    trade_result: str | None = None,
    exit_reason: str | None = None,
) -> ConditionSnapshot:
    """Build a ConditionSnapshot from current RuntimeState and candle data.

    Parameters
    ----------
    trade_id : str
        Unique identifier for the trade.
    snapshot_type : str
        "ENTRY" or "EXIT".
    candle : CandleSnapshot
        The current candle with OHLC, EMA, and ATR values.
    entry_type : str
        How the trade was entered (CROSSOVER, PREBUY, etc.).
    trade_result : str | None
        WIN / LOSS / FLAT — only for EXIT snapshots.
    exit_reason : str | None
        Reason for exit — only for EXIT snapshots.
    """
    # --- Read from candle ---
    ema9 = getattr(candle, "fast_ema", None)
    ema21 = getattr(candle, "slow_ema", None)
    atr = getattr(candle, "atr", None)
    c_open = getattr(candle, "open", None)
    c_high = getattr(candle, "high", None)
    c_low = getattr(candle, "low", None)
    c_close = getattr(candle, "close", None)
    c_time = getattr(candle, "time", None)

    # --- Read from STATE (graceful on missing attrs) ---
    ema_cycle_phase = _safe_getattr(STATE, "ema_cycle_phase")
    price_dist_ema9 = _safe_getattr(STATE, "price_dist_ema9")
    price_dist_ema21 = _safe_getattr(STATE, "price_dist_ema21")
    price_dist_ema9_atr = _safe_getattr(STATE, "price_dist_ema9_atr")
    price_dist_ema21_atr = _safe_getattr(STATE, "price_dist_ema21_atr")
    open_dist_ema9 = _safe_getattr(STATE, "open_dist_ema9")
    open_dist_ema9_atr = _safe_getattr(STATE, "open_dist_ema9_atr")
    price_zone = _safe_getattr(STATE, "price_zone")
    index_ltp = _safe_getattr(STATE, "ltp")
    leading_phase = _safe_getattr(STATE, "leading_phase")
    leading_phase_duration = _safe_getattr(STATE, "leading_phase_duration")

    # --- Computed fields ---
    ema_gap = abs(ema9 - ema21) if ema9 is not None and ema21 is not None else None
    ema_gap_atr = _compute_ema_gap_atr(ema9, ema21, atr)
    phase_duration = _compute_phase_duration(ema_cycle_phase)
    body_ratio = _compute_body_ratio(c_open, c_high, c_low, c_close)
    range_atr = _compute_range_atr(c_high, c_low, atr)
    price_ema9_momentum = _compute_price_ema9_momentum()
    time_of_day = _extract_time_of_day(c_time)
    phase_alignment = _compute_phase_alignment(leading_phase, ema_cycle_phase)

    return ConditionSnapshot(
        trade_id=trade_id,
        snapshot_type=snapshot_type,
        ema9=ema9,
        ema21=ema21,
        ema_gap=ema_gap,
        ema_gap_atr=ema_gap_atr,
        ema_cycle_phase=ema_cycle_phase,
        phase_duration=phase_duration,
        price_distance_ema9=price_dist_ema9,
        price_distance_ema21=price_dist_ema21,
        price_dist_ema9_atr=price_dist_ema9_atr,
        price_dist_ema21_atr=price_dist_ema21_atr,
        open_dist_ema9=open_dist_ema9,
        open_dist_ema9_atr=open_dist_ema9_atr,
        price_ema9_momentum=price_ema9_momentum,
        price_zone=price_zone,
        candle_open=c_open,
        candle_high=c_high,
        candle_low=c_low,
        candle_close=c_close,
        candle_body_ratio=body_ratio,
        candle_range_atr=range_atr,
        atr_value=atr,
        entry_type=entry_type,
        time_of_day=time_of_day,
        index_ltp=index_ltp,
        trade_result=trade_result,
        exit_reason=exit_reason,
        leading_phase=leading_phase,
        leading_phase_duration=leading_phase_duration,
        phase_alignment=phase_alignment,
    )


# ------------------------------------------------------------------
# Serialization
# ------------------------------------------------------------------

# Column order in condition_snapshots table (excluding id and captured_at)
_DB_COLUMNS = (
    "trade_id", "snapshot_type",
    "ema9", "ema21", "ema_gap", "ema_gap_atr",
    "ema_cycle_phase", "phase_duration",
    "price_distance_ema9", "price_distance_ema21",
    "price_dist_ema9_atr", "price_dist_ema21_atr",
    "open_dist_ema9", "open_dist_ema9_atr",
    "price_ema9_momentum", "price_zone",
    "candle_open", "candle_high", "candle_low", "candle_close",
    "candle_body_ratio", "candle_range_atr",
    "atr_value", "entry_type", "time_of_day", "index_ltp",
    "trade_result", "exit_reason",
    "leading_phase", "leading_phase_duration", "phase_alignment",
)


def to_db_row(snapshot: ConditionSnapshot) -> tuple:
    """Convert a ConditionSnapshot to a tuple matching the
    condition_snapshots table column order (excluding ``id`` and
    ``captured_at`` which are auto-generated).
    """
    return (
        snapshot.trade_id,
        snapshot.snapshot_type,
        snapshot.ema9,
        snapshot.ema21,
        snapshot.ema_gap,
        snapshot.ema_gap_atr,
        snapshot.ema_cycle_phase,
        snapshot.phase_duration,
        snapshot.price_distance_ema9,
        snapshot.price_distance_ema21,
        snapshot.price_dist_ema9_atr,
        snapshot.price_dist_ema21_atr,
        snapshot.open_dist_ema9,
        snapshot.open_dist_ema9_atr,
        snapshot.price_ema9_momentum,
        snapshot.price_zone,
        snapshot.candle_open,
        snapshot.candle_high,
        snapshot.candle_low,
        snapshot.candle_close,
        snapshot.candle_body_ratio,
        snapshot.candle_range_atr,
        snapshot.atr_value,
        snapshot.entry_type,
        snapshot.time_of_day,
        snapshot.index_ltp,
        snapshot.trade_result,
        snapshot.exit_reason,
        snapshot.leading_phase,
        snapshot.leading_phase_duration,
        snapshot.phase_alignment,
    )


def from_db_row(row: sqlite3.Row) -> ConditionSnapshot:
    """Reconstruct a ConditionSnapshot from a ``sqlite3.Row``.

    The row must come from a query against the ``condition_snapshots``
    table with ``row_factory = sqlite3.Row`` enabled.
    """
    return ConditionSnapshot(
        trade_id=row["trade_id"],
        snapshot_type=row["snapshot_type"],
        ema9=row["ema9"],
        ema21=row["ema21"],
        ema_gap=row["ema_gap"],
        ema_gap_atr=row["ema_gap_atr"],
        ema_cycle_phase=row["ema_cycle_phase"],
        phase_duration=row["phase_duration"],
        price_distance_ema9=row["price_distance_ema9"],
        price_distance_ema21=row["price_distance_ema21"],
        price_dist_ema9_atr=row["price_dist_ema9_atr"],
        price_dist_ema21_atr=row["price_dist_ema21_atr"],
        open_dist_ema9=row["open_dist_ema9"],
        open_dist_ema9_atr=row["open_dist_ema9_atr"],
        price_ema9_momentum=row["price_ema9_momentum"],
        price_zone=row["price_zone"],
        candle_open=row["candle_open"],
        candle_high=row["candle_high"],
        candle_low=row["candle_low"],
        candle_close=row["candle_close"],
        candle_body_ratio=row["candle_body_ratio"],
        candle_range_atr=row["candle_range_atr"],
        atr_value=row["atr_value"],
        entry_type=row["entry_type"],
        time_of_day=row["time_of_day"],
        index_ltp=row["index_ltp"],
        trade_result=row["trade_result"],
        exit_reason=row["exit_reason"],
        leading_phase=row["leading_phase"],
        leading_phase_duration=row["leading_phase_duration"],
        phase_alignment=row["phase_alignment"],
    )

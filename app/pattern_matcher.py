"""
Pattern matcher for the Trade Intelligence System.

Compares current market conditions against historical trade outcomes
to produce a SKIP / CONFIRM / NEUTRAL recommendation before entry.

All matching is done in-memory after a single SQL query for <100ms
performance with typical trade volumes (<5000 trades).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from app import config
from app import trade_db
from app import condition_snapshot as cs

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Time-of-day bucketing
# ------------------------------------------------------------------

# 30-minute buckets from 09:15 to 15:30
_TIME_BUCKETS: list[str] = []
_h, _m = 9, 15
while (_h, _m) <= (15, 30):
    _TIME_BUCKETS.append(f"{_h:02d}:{_m:02d}")
    _m += 30
    if _m >= 60:
        _h += 1
        _m -= 60


def _time_to_bucket(time_str: Optional[str]) -> Optional[str]:
    """Map an HH:MM time string to the nearest 30-minute bucket.

    Returns the bucket whose start time is ≤ the given time.
    If the time falls before the first bucket or after the last,
    returns the nearest boundary bucket.
    """
    if not time_str:
        return None
    try:
        parts = time_str.strip().split(":")
        h, m = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None

    # Find the largest bucket start ≤ (h, m)
    best = _TIME_BUCKETS[0]
    for bucket in _TIME_BUCKETS:
        bh, bm = int(bucket[:2]), int(bucket[3:])
        if (bh, bm) <= (h, m):
            best = bucket
        else:
            break
    return best


# ------------------------------------------------------------------
# Condition dimensions used for matching
# ------------------------------------------------------------------

_DIMENSIONS = (
    "ema_gap_atr",
    "price_dist_ema9_atr",
    "phase_duration",
    "candle_body_ratio",
    "candle_range_atr",
    "time_of_day_bucket",
)


# ------------------------------------------------------------------
# MatchResult dataclass
# ------------------------------------------------------------------

@dataclass
class MatchResult:
    """Result of a pattern-matching evaluation."""
    recommendation: str          # "SKIP", "CONFIRM", "NEUTRAL"
    win_count: int = 0
    loss_count: int = 0
    total_matches: int = 0
    win_profile: dict = field(default_factory=dict)
    loss_profile: dict = field(default_factory=dict)
    match_details: str = ""


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _recency_weight(entry_time_str: Optional[str], now: datetime) -> float:
    """Compute recency weight for a trade based on its entry time.

    Last 5 trading days  → PATTERN_RECENCY_5D_WEIGHT  (default 2.0)
    Last 10 trading days → PATTERN_RECENCY_10D_WEIGHT (default 1.5)
    Older                → 1.0
    """
    if not entry_time_str:
        return 1.0
    try:
        entry_dt = datetime.strptime(str(entry_time_str)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return 1.0

    delta_days = (now - entry_dt).days
    if delta_days <= 5:
        return config.PATTERN_RECENCY_5D_WEIGHT
    if delta_days <= 10:
        return config.PATTERN_RECENCY_10D_WEIGHT
    return 1.0


def _within_tolerance(current: float, historical: float,
                      tolerance: float) -> bool:
    """Check if *historical* falls within *current* ± tolerance band.

    For numeric dimensions the band is ``current * (1 ± tolerance)``.
    When current is zero, we use an absolute tolerance of *tolerance*
    to avoid a zero-width band.
    """
    if current == 0.0:
        return abs(historical) <= tolerance
    lo = current * (1.0 - tolerance)
    hi = current * (1.0 + tolerance)
    if lo > hi:
        lo, hi = hi, lo
    return lo <= historical <= hi


def _extract_snapshot_values(row) -> dict:
    """Extract the condition dimension values from a DB row (tuple).

    Expected column order from the query (see _QUERY):
      0: entry_time, 1: result, 2: ema_gap_atr, 3: price_dist_ema9_atr,
      4: phase_duration, 5: candle_body_ratio, 6: candle_range_atr,
      7: time_of_day
    """
    return {
        "ema_gap_atr": float(row[2]) if row[2] is not None else 0.0,
        "price_dist_ema9_atr": float(row[3]) if row[3] is not None else 0.0,
        "phase_duration": float(row[4]) if row[4] is not None else 0.0,
        "candle_body_ratio": float(row[5]) if row[5] is not None else 0.0,
        "candle_range_atr": float(row[6]) if row[6] is not None else 0.0,
        "time_of_day_bucket": _time_to_bucket(row[7]),
    }


def _is_tolerance_match(current_vals: dict, hist_vals: dict,
                        tolerance: float) -> bool:
    """Return True if every numeric dimension of *hist_vals* falls
    within the tolerance band of *current_vals*.

    time_of_day_bucket uses exact equality.
    """
    for dim in _DIMENSIONS:
        cv = current_vals.get(dim)
        hv = hist_vals.get(dim)

        if dim == "time_of_day_bucket":
            if cv != hv:
                return False
            continue

        # Skip dimension if either value is None
        if cv is None or hv is None:
            continue

        if not _within_tolerance(float(cv), float(hv), tolerance):
            return False
    return True


def _weighted_euclidean_distance(current_vals: dict, hist_vals: dict) -> float:
    """Compute weighted Euclidean distance between current and historical
    condition values.  time_of_day_bucket contributes 0 if equal, 1 otherwise.
    """
    dist_sq = 0.0
    for dim in _DIMENSIONS:
        cv = current_vals.get(dim)
        hv = hist_vals.get(dim)

        if dim == "time_of_day_bucket":
            dist_sq += 0.0 if cv == hv else 1.0
            continue

        if cv is None or hv is None:
            continue

        diff = float(cv) - float(hv)
        # Normalise by current value to make dimensions comparable
        norm = abs(float(cv)) if float(cv) != 0 else 1.0
        dist_sq += (diff / norm) ** 2

    return math.sqrt(dist_sq)


def _compute_profile(trades: list[tuple], value_extractor) -> dict:
    """Compute average condition values across a list of trades.

    Returns a dict keyed by dimension name with the mean value.
    """
    if not trades:
        return {}

    sums: dict[str, float] = {}
    counts: dict[str, int] = {}

    for t in trades:
        vals = value_extractor(t)
        for dim in _DIMENSIONS:
            if dim == "time_of_day_bucket":
                continue  # skip non-numeric
            v = vals.get(dim)
            if v is not None:
                sums[dim] = sums.get(dim, 0.0) + float(v)
                counts[dim] = counts.get(dim, 0) + 1

    profile = {}
    for dim in sums:
        if counts.get(dim, 0) > 0:
            profile[dim] = round(sums[dim] / counts[dim], 6)
    return profile


# ------------------------------------------------------------------
# SQL query — single query for all matching trades
# ------------------------------------------------------------------

_QUERY = """
SELECT
    t.entry_time,
    t.result,
    cs.ema_gap_atr,
    cs.price_dist_ema9_atr,
    cs.phase_duration,
    cs.candle_body_ratio,
    cs.candle_range_atr,
    cs.time_of_day
FROM trades t
JOIN condition_snapshots cs ON cs.trade_id = t.trade_id
WHERE t.status = 'CLOSED'
  AND t.entry_type = ?
  AND t.instrument = ?
  AND t.symbol = ?
  AND cs.snapshot_type = 'ENTRY'
ORDER BY t.entry_time
"""


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def evaluate(entry_type: str, candle, instrument: str) -> MatchResult:
    """Query historical trades, compute match, return recommendation.

    Parameters
    ----------
    entry_type : str
        The entry classification (CROSSOVER, PREBUY, etc.).
    candle : object
        Current candle with OHLC, EMA, ATR attributes.
    instrument : str
        CE / PE / FUT.

    Returns
    -------
    MatchResult
        Contains recommendation, counts, profiles, and details.
    """
    try:
        return _evaluate_impl(entry_type, candle, instrument)
    except Exception:
        logger.exception("Pattern matcher evaluate() failed — returning NEUTRAL")
        return MatchResult(
            recommendation="NEUTRAL",
            match_details="Error during pattern matching — defaulting to NEUTRAL",
        )


def _evaluate_impl(entry_type: str, candle, instrument: str) -> MatchResult:
    """Core evaluation logic (unwrapped from error handler)."""
    conn = trade_db.get_connection()
    symbol = config.LIVE_SYMBOL

    rows = conn.execute(_QUERY, (entry_type, instrument, symbol)).fetchall()

    # --- Insufficient history → NEUTRAL ---
    if len(rows) < config.PATTERN_MIN_TRADES:
        result = MatchResult(
            recommendation="NEUTRAL",
            total_matches=len(rows),
            match_details=(
                f"Insufficient history: {len(rows)} trades "
                f"(need {config.PATTERN_MIN_TRADES})"
            ),
        )
        _log_recommendation(result, entry_type, instrument)
        return result

    # --- Build current snapshot values from the candle ---
    snap = cs.capture(
        trade_id="__pattern_eval__",
        snapshot_type="ENTRY",
        candle=candle,
        entry_type=entry_type,
    )
    current_vals = {
        "ema_gap_atr": snap.ema_gap_atr if snap.ema_gap_atr is not None else 0.0,
        "price_dist_ema9_atr": snap.price_dist_ema9_atr if snap.price_dist_ema9_atr is not None else 0.0,
        "phase_duration": float(snap.phase_duration) if snap.phase_duration is not None else 0.0,
        "candle_body_ratio": snap.candle_body_ratio if snap.candle_body_ratio is not None else 0.0,
        "candle_range_atr": snap.candle_range_atr if snap.candle_range_atr is not None else 0.0,
        "time_of_day_bucket": _time_to_bucket(snap.time_of_day),
    }

    now = datetime.now()
    tolerance = config.PATTERN_TOLERANCE_PCT

    # --- Tolerance-band matching with recency weighting ---
    matched_wins: list[tuple] = []
    matched_losses: list[tuple] = []
    matched_win_weights: list[float] = []
    matched_loss_weights: list[float] = []

    for row in rows:
        hist_vals = _extract_snapshot_values(row)
        if _is_tolerance_match(current_vals, hist_vals, tolerance):
            weight = _recency_weight(row[0], now)
            result_str = row[1]
            if result_str == "WIN":
                matched_wins.append(row)
                matched_win_weights.append(weight)
            elif result_str == "LOSS":
                matched_losses.append(row)
                matched_loss_weights.append(weight)
            # FLAT trades are counted but don't affect win/loss

    total_tolerance_matches = len(matched_wins) + len(matched_losses)

    # --- Fallback to nearest N if no tolerance matches ---
    used_fallback = False
    if total_tolerance_matches == 0:
        used_fallback = True
        distances = []
        for row in rows:
            hist_vals = _extract_snapshot_values(row)
            dist = _weighted_euclidean_distance(current_vals, hist_vals)
            weight = _recency_weight(row[0], now)
            distances.append((dist, weight, row))

        distances.sort(key=lambda x: x[0])
        nearest = distances[: config.PATTERN_FALLBACK_N]

        matched_wins = []
        matched_losses = []
        matched_win_weights = []
        matched_loss_weights = []

        for _dist, weight, row in nearest:
            result_str = row[1]
            if result_str == "WIN":
                matched_wins.append(row)
                matched_win_weights.append(weight)
            elif result_str == "LOSS":
                matched_losses.append(row)
                matched_loss_weights.append(weight)

    # --- Compute weighted counts ---
    weighted_wins = sum(matched_win_weights)
    weighted_losses = sum(matched_loss_weights)
    weighted_total = weighted_wins + weighted_losses

    # --- Determine recommendation ---
    if weighted_total == 0:
        recommendation = "NEUTRAL"
    elif (weighted_losses / weighted_total) > config.PATTERN_SKIP_THRESHOLD:
        recommendation = "SKIP"
    elif (weighted_wins / weighted_total) > config.PATTERN_CONFIRM_THRESHOLD:
        recommendation = "CONFIRM"
    else:
        recommendation = "NEUTRAL"

    # --- Compute profiles ---
    win_profile = _compute_profile(matched_wins, _extract_snapshot_values)
    loss_profile = _compute_profile(matched_losses, _extract_snapshot_values)

    # --- Build details string ---
    fallback_note = " (fallback: nearest N)" if used_fallback else ""
    details = (
        f"{recommendation} | wins={len(matched_wins)} losses={len(matched_losses)} "
        f"total={len(matched_wins) + len(matched_losses)}{fallback_note} | "
        f"weighted_win_ratio={weighted_wins / weighted_total:.2f} "
        if weighted_total > 0 else
        f"{recommendation} | no matching trades"
    )

    result = MatchResult(
        recommendation=recommendation,
        win_count=len(matched_wins),
        loss_count=len(matched_losses),
        total_matches=len(matched_wins) + len(matched_losses),
        win_profile=win_profile,
        loss_profile=loss_profile,
        match_details=details,
    )

    _log_recommendation(result, entry_type, instrument)
    return result


def _log_recommendation(result: MatchResult, entry_type: str,
                        instrument: str) -> None:
    """Log the pattern matcher recommendation with full details."""
    log_only_tag = " [LOG_ONLY]" if config.PATTERN_LOG_ONLY else ""
    logger.info(
        "PatternMatcher%s | %s | entry_type=%s instrument=%s | "
        "wins=%d losses=%d total=%d | win_profile=%s loss_profile=%s | %s",
        log_only_tag,
        result.recommendation,
        entry_type,
        instrument,
        result.win_count,
        result.loss_count,
        result.total_matches,
        result.win_profile,
        result.loss_profile,
        result.match_details,
    )

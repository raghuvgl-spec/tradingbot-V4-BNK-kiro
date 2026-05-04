"""
Win/Loss statistical analysis for the Trade Intelligence System.

Computes separate statistical profiles (mean, median, std) for winning
and losing trades across key condition dimensions, grouped by
(entry_type, instrument).  Identifies the top discriminating features
that best separate wins from losses.

Uses ``app.trade_db.get_connection()`` for all database access.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Optional

from app import config, trade_db

logger = logging.getLogger(__name__)

# Condition dimensions analysed
_DIMENSIONS = (
    "ema_gap_atr",
    "price_dist_ema9_atr",
    "phase_duration",
    "candle_body_ratio",
    "time_of_day",
)


# ------------------------------------------------------------------
# Data model
# ------------------------------------------------------------------

@dataclass
class EntryTypeProfile:
    """Statistical profile for a (entry_type, instrument) group."""

    entry_type: str
    instrument: str
    sample_size: int
    sufficient_data: bool
    win_profile: dict = field(default_factory=dict)
    loss_profile: dict = field(default_factory=dict)
    top_discriminators: list = field(default_factory=list)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _time_of_day_numeric(time_str: Optional[str]) -> Optional[float]:
    """Convert an ``HH:MM`` time string to a numeric hour (e.g. 10:30 → 10.5).

    Returns ``None`` when the input is ``None`` or unparseable.
    """
    if time_str is None:
        return None
    try:
        parts = str(time_str).strip().split(":")
        if len(parts) >= 2:
            return int(parts[0]) + int(parts[1]) / 60.0
    except (ValueError, IndexError):
        pass
    return None


def _stats_for(values: list[float]) -> dict:
    """Compute mean, median, and std for a list of floats.

    Returns a dict with keys ``mean``, ``median``, ``std``.
    If the list is empty, all values are 0.0.
    """
    if not values:
        return {"mean": 0.0, "median": 0.0, "std": 0.0}
    mean = statistics.mean(values)
    median = statistics.median(values)
    std = statistics.pstdev(values)  # population std — matches design intent
    return {"mean": mean, "median": median, "std": std}


def _compute_top_discriminators(
    win_profile: dict, loss_profile: dict, n: int = 3
) -> list[dict]:
    """Return the top *n* dimensions with the largest separation metric.

    Separation metric for each dimension:
        ``abs(win_mean - loss_mean) / max(win_std, loss_std, 0.001)``

    Each entry in the returned list is a dict with keys:
        ``feature``, ``separation``, ``win_mean``, ``loss_mean``.
    """
    scored: list[tuple[float, dict]] = []
    for dim in _DIMENSIONS:
        w = win_profile.get(dim, {})
        l = loss_profile.get(dim, {})
        w_mean = w.get("mean", 0.0)
        l_mean = l.get("mean", 0.0)
        w_std = w.get("std", 0.0)
        l_std = l.get("std", 0.0)
        denom = max(w_std, l_std, 0.001)
        separation = abs(w_mean - l_mean) / denom
        scored.append(
            (
                separation,
                {
                    "feature": dim,
                    "separation": round(separation, 4),
                    "win_mean": round(w_mean, 4),
                    "loss_mean": round(l_mean, 4),
                },
            )
        )
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:n]]


# ------------------------------------------------------------------
# SQL query
# ------------------------------------------------------------------

_QUERY = """
SELECT
    cs.entry_type,
    t.instrument,
    t.result,
    cs.ema_gap_atr,
    cs.price_dist_ema9_atr,
    cs.phase_duration,
    cs.candle_body_ratio,
    cs.time_of_day
FROM condition_snapshots cs
JOIN trades t ON cs.trade_id = t.trade_id
WHERE t.status = 'CLOSED'
  AND t.result IN ('WIN', 'LOSS')
  AND cs.snapshot_type = 'ENTRY'
  AND t.symbol = ?
"""


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def analyze(
    entry_type: Optional[str] = None,
    instrument: Optional[str] = None,
) -> dict[str, EntryTypeProfile]:
    """Compute win/loss profiles grouped by (entry_type, instrument).

    Parameters
    ----------
    entry_type : str, optional
        If provided, only analyse this entry type.
    instrument : str, optional
        If provided, only analyse this instrument.

    Returns
    -------
    dict[str, EntryTypeProfile]
        Keyed by ``"{entry_type}_{instrument}"``.
    """
    try:
        conn = trade_db.get_connection()

        # Build query with optional filters
        query = _QUERY
        params: list = [config.LIVE_SYMBOL]

        if entry_type is not None:
            query += " AND cs.entry_type = ?"
            params.append(entry_type)
        if instrument is not None:
            query += " AND t.instrument = ?"
            params.append(instrument)

        rows = conn.execute(query, params).fetchall()

        # Group rows by (entry_type, instrument)
        groups: dict[tuple[str, str], list] = {}
        for row in rows:
            r_entry_type = row[0] or "UNKNOWN"
            r_instrument = row[1] or "UNKNOWN"
            key = (r_entry_type, r_instrument)
            groups.setdefault(key, []).append(row)

        results: dict[str, EntryTypeProfile] = {}

        for (et, inst), group_rows in groups.items():
            sample_size = len(group_rows)
            sufficient = sample_size >= config.ANALYZER_MIN_TRADES

            # Separate wins and losses, collecting dimension values
            win_values: dict[str, list[float]] = {d: [] for d in _DIMENSIONS}
            loss_values: dict[str, list[float]] = {d: [] for d in _DIMENSIONS}

            for row in group_rows:
                result = row[2]  # WIN or LOSS
                raw = {
                    "ema_gap_atr": row[3],
                    "price_dist_ema9_atr": row[4],
                    "phase_duration": row[5],
                    "candle_body_ratio": row[6],
                    "time_of_day": row[7],
                }

                target = win_values if result == "WIN" else loss_values

                for dim in _DIMENSIONS:
                    val = raw[dim]
                    if dim == "time_of_day":
                        val = _time_of_day_numeric(val)
                    if val is not None:
                        target[dim].append(float(val))

            # Compute stats per dimension
            win_profile = {dim: _stats_for(win_values[dim]) for dim in _DIMENSIONS}
            loss_profile = {dim: _stats_for(loss_values[dim]) for dim in _DIMENSIONS}

            # Top discriminators
            top_disc = _compute_top_discriminators(win_profile, loss_profile)

            key_str = f"{et}_{inst}"
            results[key_str] = EntryTypeProfile(
                entry_type=et,
                instrument=inst,
                sample_size=sample_size,
                sufficient_data=sufficient,
                win_profile=win_profile,
                loss_profile=loss_profile,
                top_discriminators=top_disc,
            )

        return results

    except Exception:
        logger.exception("win_loss_analyzer.analyze() failed")
        return {}

"""
Unit tests for app/condition_snapshot.py — ConditionSnapshot dataclass,
capture(), to_db_row(), and from_db_row().
"""

import sqlite3
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from app.condition_snapshot import (
    ConditionSnapshot,
    capture,
    from_db_row,
    to_db_row,
    _compute_body_ratio,
    _compute_ema_gap_atr,
    _compute_range_atr,
    _extract_time_of_day,
)
from app.trade_db import ensure_tables


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@dataclass
class FakeCandle:
    time: str = "2024-06-15 10:30:00"
    open: float = 100.0
    high: float = 110.0
    low: float = 95.0
    close: float = 105.0
    volume: float = 1000.0
    fast_ema: float = 102.0
    slow_ema: float = 98.0
    vwap: float = 101.0
    atr: float = 5.0


class FakeState:
    ema_cycle_phase = "EXPANDING"
    ema_gap_expanding_count = 3
    ema_gap_contracting_count = 0
    price_dist_ema9 = 2.5
    price_dist_ema21 = 5.0
    price_dist_ema9_atr = 0.5
    price_dist_ema21_atr = 1.0
    open_dist_ema9 = 1.8
    open_dist_ema9_atr = 0.36
    price_zone = "NORMAL"
    prev_price_dist_ema9 = 1.5
    ltp = 48500.0
    leading_phase = "EXPANDING_UP"
    leading_phase_duration = 4


@pytest.fixture
def in_memory_db():
    """Create an in-memory SQLite DB with the schema initialised."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_tables(conn)
    # Insert a dummy trade so the FK constraint is satisfied
    conn.execute(
        "INSERT INTO trades (trade_id, entry_time, symbol, instrument, side, qty, entry_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("T001", "2024-06-15 10:30:00", "BANKNIFTY", "CE", "BUY", 15, 100.0),
    )
    conn.commit()
    return conn


# ------------------------------------------------------------------
# Computed field helpers
# ------------------------------------------------------------------

class TestComputedFields:
    def test_ema_gap_atr_normal(self):
        assert _compute_ema_gap_atr(102.0, 98.0, 5.0) == pytest.approx(0.8)

    def test_ema_gap_atr_zero_atr(self):
        assert _compute_ema_gap_atr(102.0, 98.0, 0.0) == 0.0

    def test_ema_gap_atr_none_inputs(self):
        assert _compute_ema_gap_atr(None, 98.0, 5.0) is None

    def test_body_ratio_normal(self):
        # abs(105 - 100) / (110 - 95) = 5 / 15 ≈ 0.333
        assert _compute_body_ratio(100.0, 110.0, 95.0, 105.0) == pytest.approx(1 / 3)

    def test_body_ratio_zero_range(self):
        assert _compute_body_ratio(100.0, 100.0, 100.0, 100.0) == 0.0

    def test_range_atr_normal(self):
        # (110 - 95) / 5 = 3.0
        assert _compute_range_atr(110.0, 95.0, 5.0) == pytest.approx(3.0)

    def test_range_atr_zero_atr(self):
        assert _compute_range_atr(110.0, 95.0, 0.0) == 0.0

    def test_time_of_day_full_datetime(self):
        assert _extract_time_of_day("2024-06-15 09:30:00") == "09:30"

    def test_time_of_day_time_only(self):
        assert _extract_time_of_day("09:30:00") == "09:30"

    def test_time_of_day_hhmm(self):
        assert _extract_time_of_day("09:30") == "09:30"

    def test_time_of_day_none(self):
        assert _extract_time_of_day(None) is None


# ------------------------------------------------------------------
# capture()
# ------------------------------------------------------------------

class TestCapture:
    @patch("app.condition_snapshot.STATE", new_callable=lambda: FakeState)
    def test_entry_snapshot_fields(self, mock_state):
        candle = FakeCandle()
        snap = capture("T001", "ENTRY", candle, "CROSSOVER")

        assert snap.trade_id == "T001"
        assert snap.snapshot_type == "ENTRY"
        assert snap.ema9 == 102.0
        assert snap.ema21 == 98.0
        assert snap.ema_gap == pytest.approx(4.0)
        assert snap.ema_gap_atr == pytest.approx(0.8)
        assert snap.ema_cycle_phase == "EXPANDING"
        assert snap.phase_duration == 3
        assert snap.price_distance_ema9 == 2.5
        assert snap.price_distance_ema21 == 5.0
        assert snap.price_dist_ema9_atr == 0.5
        assert snap.price_dist_ema21_atr == 1.0
        assert snap.price_zone == "NORMAL"
        assert snap.candle_open == 100.0
        assert snap.candle_high == 110.0
        assert snap.candle_low == 95.0
        assert snap.candle_close == 105.0
        assert snap.candle_body_ratio == pytest.approx(1 / 3)
        assert snap.candle_range_atr == pytest.approx(3.0)
        assert snap.atr_value == 5.0
        assert snap.entry_type == "CROSSOVER"
        assert snap.time_of_day == "10:30"
        assert snap.index_ltp == 48500.0
        assert snap.trade_result is None
        assert snap.exit_reason is None

    @patch("app.condition_snapshot.STATE", new_callable=lambda: FakeState)
    def test_exit_snapshot_has_result(self, mock_state):
        candle = FakeCandle()
        snap = capture("T001", "EXIT", candle, "CROSSOVER",
                        trade_result="WIN", exit_reason="TARGET_HIT")

        assert snap.snapshot_type == "EXIT"
        assert snap.trade_result == "WIN"
        assert snap.exit_reason == "TARGET_HIT"

    @patch("app.condition_snapshot.STATE", new_callable=lambda: FakeState)
    def test_price_ema9_momentum_expanding(self, mock_state):
        """current dist (2.5) > prev dist (1.5) → EXPANDING."""
        candle = FakeCandle()
        snap = capture("T001", "ENTRY", candle, "CROSSOVER")
        assert snap.price_ema9_momentum == "EXPANDING"

    @patch("app.condition_snapshot.STATE", new_callable=lambda: FakeState)
    def test_phase_duration_contracting(self, mock_state):
        mock_state.ema_cycle_phase = "CONTRACTING"
        mock_state.ema_gap_contracting_count = 7
        candle = FakeCandle()
        snap = capture("T001", "ENTRY", candle, "CROSSOVER")
        assert snap.phase_duration == 7

    @patch("app.condition_snapshot.STATE", new_callable=lambda: FakeState)
    def test_phase_duration_sideways(self, mock_state):
        mock_state.ema_cycle_phase = "SIDEWAYS"
        candle = FakeCandle()
        snap = capture("T001", "ENTRY", candle, "CROSSOVER")
        assert snap.phase_duration == 0


# ------------------------------------------------------------------
# to_db_row / from_db_row round-trip
# ------------------------------------------------------------------

class TestSerialization:
    @patch("app.condition_snapshot.STATE", new_callable=lambda: FakeState)
    def test_round_trip(self, mock_state, in_memory_db):
        """Write a snapshot to DB and read it back — all fields must match."""
        candle = FakeCandle()
        snap = capture("T001", "ENTRY", candle, "CROSSOVER")

        row_tuple = to_db_row(snap)
        placeholders = ", ".join(["?"] * len(row_tuple))
        in_memory_db.execute(
            f"INSERT INTO condition_snapshots "
            f"(trade_id, snapshot_type, ema9, ema21, ema_gap, ema_gap_atr, "
            f"ema_cycle_phase, phase_duration, price_distance_ema9, price_distance_ema21, "
            f"price_dist_ema9_atr, price_dist_ema21_atr, "
            f"open_dist_ema9, open_dist_ema9_atr, "
            f"price_ema9_momentum, price_zone, "
            f"candle_open, candle_high, candle_low, candle_close, candle_body_ratio, "
            f"candle_range_atr, atr_value, entry_type, time_of_day, index_ltp, "
            f"trade_result, exit_reason, "
            f"leading_phase, leading_phase_duration, phase_alignment) VALUES ({placeholders})",
            row_tuple,
        )
        in_memory_db.commit()

        cursor = in_memory_db.execute(
            "SELECT * FROM condition_snapshots WHERE trade_id = ?", ("T001",)
        )
        db_row = cursor.fetchone()
        restored = from_db_row(db_row)

        assert restored.trade_id == snap.trade_id
        assert restored.snapshot_type == snap.snapshot_type
        assert restored.ema9 == pytest.approx(snap.ema9)
        assert restored.ema21 == pytest.approx(snap.ema21)
        assert restored.ema_gap == pytest.approx(snap.ema_gap)
        assert restored.ema_gap_atr == pytest.approx(snap.ema_gap_atr)
        assert restored.ema_cycle_phase == snap.ema_cycle_phase
        assert restored.phase_duration == snap.phase_duration
        assert restored.price_distance_ema9 == pytest.approx(snap.price_distance_ema9)
        assert restored.price_distance_ema21 == pytest.approx(snap.price_distance_ema21)
        assert restored.price_dist_ema9_atr == pytest.approx(snap.price_dist_ema9_atr)
        assert restored.price_dist_ema21_atr == pytest.approx(snap.price_dist_ema21_atr)
        assert restored.open_dist_ema9 == pytest.approx(snap.open_dist_ema9)
        assert restored.open_dist_ema9_atr == pytest.approx(snap.open_dist_ema9_atr)
        assert restored.price_ema9_momentum == snap.price_ema9_momentum
        assert restored.price_zone == snap.price_zone
        assert restored.candle_open == pytest.approx(snap.candle_open)
        assert restored.candle_high == pytest.approx(snap.candle_high)
        assert restored.candle_low == pytest.approx(snap.candle_low)
        assert restored.candle_close == pytest.approx(snap.candle_close)
        assert restored.candle_body_ratio == pytest.approx(snap.candle_body_ratio)
        assert restored.candle_range_atr == pytest.approx(snap.candle_range_atr)
        assert restored.atr_value == pytest.approx(snap.atr_value)
        assert restored.entry_type == snap.entry_type
        assert restored.time_of_day == snap.time_of_day
        assert restored.index_ltp == pytest.approx(snap.index_ltp)
        assert restored.trade_result == snap.trade_result
        assert restored.exit_reason == snap.exit_reason
        assert restored.leading_phase == snap.leading_phase
        assert restored.leading_phase_duration == snap.leading_phase_duration
        assert restored.phase_alignment == snap.phase_alignment

    @patch("app.condition_snapshot.STATE", new_callable=lambda: FakeState)
    def test_to_db_row_length(self, mock_state):
        """to_db_row should produce a tuple with 31 elements (one per DB column)."""
        candle = FakeCandle()
        snap = capture("T001", "ENTRY", candle, "CROSSOVER")
        row = to_db_row(snap)
        assert len(row) == 31

    @patch("app.condition_snapshot.STATE", new_callable=lambda: FakeState)
    def test_exit_round_trip(self, mock_state, in_memory_db):
        """Exit snapshot round-trip preserves trade_result and exit_reason."""
        candle = FakeCandle()
        snap = capture("T001", "EXIT", candle, "CROSSOVER",
                        trade_result="LOSS", exit_reason="SL_HIT")

        row_tuple = to_db_row(snap)
        placeholders = ", ".join(["?"] * len(row_tuple))
        in_memory_db.execute(
            f"INSERT INTO condition_snapshots "
            f"(trade_id, snapshot_type, ema9, ema21, ema_gap, ema_gap_atr, "
            f"ema_cycle_phase, phase_duration, price_distance_ema9, price_distance_ema21, "
            f"price_dist_ema9_atr, price_dist_ema21_atr, "
            f"open_dist_ema9, open_dist_ema9_atr, "
            f"price_ema9_momentum, price_zone, "
            f"candle_open, candle_high, candle_low, candle_close, candle_body_ratio, "
            f"candle_range_atr, atr_value, entry_type, time_of_day, index_ltp, "
            f"trade_result, exit_reason, "
            f"leading_phase, leading_phase_duration, phase_alignment) VALUES ({placeholders})",
            row_tuple,
        )
        in_memory_db.commit()

        cursor = in_memory_db.execute(
            "SELECT * FROM condition_snapshots WHERE trade_id = ? AND snapshot_type = 'EXIT'",
            ("T001",),
        )
        db_row = cursor.fetchone()
        restored = from_db_row(db_row)

        assert restored.trade_result == "LOSS"
        assert restored.exit_reason == "SL_HIT"

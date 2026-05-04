"""
Unit tests for app.trade_logger — the SQLite-backed trade logging module.
"""

import sqlite3
import pytest
from unittest.mock import patch, MagicMock

from app import trade_db
from app import trade_logger


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _in_memory_db(monkeypatch):
    """Replace the module-level DB connection with an in-memory SQLite DB
    for every test, so tests are isolated and fast."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    trade_db.ensure_tables(conn)

    # Patch get_connection to return our in-memory DB
    monkeypatch.setattr(trade_db, "_connection", conn)
    monkeypatch.setattr(trade_db, "get_connection", lambda: conn)

    yield conn

    conn.close()


@pytest.fixture()
def _mock_snapshot(monkeypatch):
    """Stub out condition_snapshot.capture so it returns a dummy snapshot
    without needing a real RuntimeState."""
    from app.condition_snapshot import ConditionSnapshot

    dummy = ConditionSnapshot(
        trade_id="1", snapshot_type="ENTRY",
        ema9=100.0, ema21=95.0, ema_gap=5.0, ema_gap_atr=0.5,
        ema_cycle_phase="EXPANDING", phase_duration=3,
        price_distance_ema9=2.0, price_distance_ema21=7.0,
        price_dist_ema9_atr=0.2, price_dist_ema21_atr=0.7,
        open_dist_ema9=1.5, open_dist_ema9_atr=0.15,
        price_ema9_momentum="EXPANDING", price_zone="NORMAL",
        candle_open=100.0, candle_high=105.0, candle_low=98.0, candle_close=103.0,
        candle_body_ratio=0.43, candle_range_atr=0.7,
        atr_value=10.0, entry_type="CROSSOVER",
        time_of_day="09:30", index_ltp=50000.0,
        trade_result=None, exit_reason=None,
        leading_phase="EXPANDING_UP", leading_phase_duration=4,
        phase_alignment="ALIGNED_UP",
    )

    def fake_capture(trade_id, snapshot_type, candle, entry_type,
                     trade_result=None, exit_reason=None):
        return ConditionSnapshot(
            trade_id=str(trade_id), snapshot_type=snapshot_type,
            ema9=dummy.ema9, ema21=dummy.ema21, ema_gap=dummy.ema_gap,
            ema_gap_atr=dummy.ema_gap_atr, ema_cycle_phase=dummy.ema_cycle_phase,
            phase_duration=dummy.phase_duration,
            price_distance_ema9=dummy.price_distance_ema9,
            price_distance_ema21=dummy.price_distance_ema21,
            price_dist_ema9_atr=dummy.price_dist_ema9_atr,
            price_dist_ema21_atr=dummy.price_dist_ema21_atr,
            open_dist_ema9=dummy.open_dist_ema9,
            open_dist_ema9_atr=dummy.open_dist_ema9_atr,
            price_ema9_momentum=dummy.price_ema9_momentum,
            price_zone=dummy.price_zone,
            candle_open=dummy.candle_open, candle_high=dummy.candle_high,
            candle_low=dummy.candle_low, candle_close=dummy.candle_close,
            candle_body_ratio=dummy.candle_body_ratio,
            candle_range_atr=dummy.candle_range_atr,
            atr_value=dummy.atr_value, entry_type=entry_type or "",
            time_of_day=dummy.time_of_day, index_ltp=dummy.index_ltp,
            trade_result=trade_result, exit_reason=exit_reason,
            leading_phase=dummy.leading_phase,
            leading_phase_duration=dummy.leading_phase_duration,
            phase_alignment=dummy.phase_alignment,
        )

    monkeypatch.setattr("app.trade_logger.cs.capture", fake_capture)


# ------------------------------------------------------------------
# log_trade_entry tests
# ------------------------------------------------------------------

class TestLogTradeEntry:

    def test_inserts_trade_record(self, _mock_snapshot, _in_memory_db):
        trade_logger.log_trade_entry(
            trade_id="T1", symbol="BANKNIFTY", instrument="CE",
            side="BUY", qty=50, entry_price=100.0, trade_count=1,
            reason="CROSSOVER", atr=10.0, sl=90.0, target=120.0,
            entry_type="CROSSOVER",
        )
        row = _in_memory_db.execute(
            "SELECT trade_id, symbol, instrument, side, qty, entry_price, status FROM trades WHERE trade_id='T1'"
        ).fetchone()
        assert row is not None
        assert row == ("T1", "BANKNIFTY", "CE", "BUY", 50, 100.0, "OPEN")

    def test_inserts_entry_snapshot(self, _mock_snapshot, _in_memory_db):
        trade_logger.log_trade_entry(
            trade_id="T2", symbol="NIFTY", instrument="PE",
            side="BUY", qty=25, entry_price=200.0, trade_count=2,
            entry_type="PREBUY",
        )
        snap = _in_memory_db.execute(
            "SELECT snapshot_type, entry_type FROM condition_snapshots WHERE trade_id='T2'"
        ).fetchone()
        assert snap is not None
        assert snap[0] == "ENTRY"
        assert snap[1] == "PREBUY"

    def test_rejects_invalid_symbol(self, _mock_snapshot, _in_memory_db):
        trade_logger.log_trade_entry(
            trade_id="T3", symbol="CRUDE", instrument="FUT",
            side="BUY", qty=10, entry_price=5000.0, trade_count=1,
        )
        row = _in_memory_db.execute(
            "SELECT * FROM trades WHERE trade_id='T3'"
        ).fetchone()
        assert row is None

    def test_duplicate_trade_id_ignored(self, _mock_snapshot, _in_memory_db):
        for _ in range(2):
            trade_logger.log_trade_entry(
                trade_id="T4", symbol="BANKNIFTY", instrument="CE",
                side="BUY", qty=50, entry_price=100.0, trade_count=1,
            )
        count = _in_memory_db.execute(
            "SELECT COUNT(*) FROM trades WHERE trade_id='T4'"
        ).fetchone()[0]
        assert count == 1


# ------------------------------------------------------------------
# log_trade_exit tests
# ------------------------------------------------------------------

class TestLogTradeExit:

    def _insert_open_trade(self, conn, trade_id="T10", side="BUY",
                           entry_price=100.0, qty=50):
        conn.execute(
            """INSERT INTO trades (trade_id, entry_time, symbol, instrument,
               side, qty, entry_price, trade_count, status, entry_type)
               VALUES (?, datetime('now'), 'BANKNIFTY', 'CE', ?, ?, ?, 1, 'OPEN', 'CROSSOVER')""",
            (trade_id, side, qty, entry_price),
        )
        conn.commit()

    def test_updates_trade_with_exit_data(self, _mock_snapshot, _in_memory_db):
        self._insert_open_trade(_in_memory_db)
        trade_logger.log_trade_exit("T10", exit_price=120.0, result="WIN",
                                    reason="TARGET", status="CLOSED")
        row = _in_memory_db.execute(
            "SELECT exit_price, pnl, result, status FROM trades WHERE trade_id='T10'"
        ).fetchone()
        assert row is not None
        assert row[0] == 120.0
        assert row[1] == 1000.0  # (120 - 100) * 50
        assert row[2] == "WIN"
        assert row[3] == "CLOSED"

    def test_pnl_buy_side(self, _mock_snapshot, _in_memory_db):
        self._insert_open_trade(_in_memory_db, trade_id="T11", side="BUY",
                                entry_price=100.0, qty=50)
        trade_logger.log_trade_exit("T11", exit_price=90.0, result="LOSS",
                                    reason="SL")
        pnl = _in_memory_db.execute(
            "SELECT pnl FROM trades WHERE trade_id='T11'"
        ).fetchone()[0]
        assert pnl == -500.0  # (90 - 100) * 50

    def test_pnl_sell_side(self, _mock_snapshot, _in_memory_db):
        self._insert_open_trade(_in_memory_db, trade_id="T12", side="SELL",
                                entry_price=100.0, qty=50)
        trade_logger.log_trade_exit("T12", exit_price=90.0, result="WIN",
                                    reason="TARGET")
        pnl = _in_memory_db.execute(
            "SELECT pnl FROM trades WHERE trade_id='T12'"
        ).fetchone()[0]
        assert pnl == 500.0  # (100 - 90) * 50

    def test_exit_creates_exit_snapshot(self, _mock_snapshot, _in_memory_db):
        self._insert_open_trade(_in_memory_db, trade_id="T13")
        trade_logger.log_trade_exit("T13", exit_price=110.0, result="WIN",
                                    reason="GIVEBACK")
        snap = _in_memory_db.execute(
            "SELECT snapshot_type FROM condition_snapshots WHERE trade_id='T13'"
        ).fetchone()
        assert snap is not None
        assert snap[0] == "EXIT"

    def test_exit_nonexistent_trade_does_not_crash(self, _mock_snapshot):
        # Should log a warning but not raise
        trade_logger.log_trade_exit("NONEXISTENT", exit_price=100.0,
                                    result="LOSS", reason="SL")


# ------------------------------------------------------------------
# update_metrics tests
# ------------------------------------------------------------------

class TestUpdateMetrics:

    def _insert_closed_trade(self, conn, trade_id, pnl, entry_time="2025-01-01 10:00:00",
                             instrument="CE", entry_type="CROSSOVER"):
        conn.execute(
            """INSERT INTO trades (trade_id, entry_time, symbol, instrument,
               side, qty, entry_price, exit_price, pnl, result, status, entry_type)
               VALUES (?, ?, 'BANKNIFTY', ?, 'BUY', 50, 100.0, 110.0, ?, ?, 'CLOSED', ?)""",
            (trade_id, entry_time, instrument, pnl,
             "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT",
             entry_type),
        )
        conn.commit()

    def test_empty_db_returns_zeros(self, _in_memory_db):
        m = trade_logger.update_metrics()
        assert m["total_trades"] == 0
        assert m["net_pnl"] == 0.0

    def test_basic_metrics(self, _in_memory_db):
        self._insert_closed_trade(_in_memory_db, "M1", 500.0)
        self._insert_closed_trade(_in_memory_db, "M2", -200.0)
        self._insert_closed_trade(_in_memory_db, "M3", 300.0)

        m = trade_logger.update_metrics()
        assert m["total_trades"] == 3
        assert m["wins"] == 2
        assert m["losses"] == 1
        assert m["net_pnl"] == 600.0
        assert m["win_rate_percent"] == pytest.approx(66.67, abs=0.01)
        assert m["average_win"] == 400.0
        assert m["average_loss"] == -200.0
        assert m["profit_factor"] == 4.0

    def test_max_drawdown(self, _in_memory_db):
        # Equity curve: +100, +200, -100, +50 → cumulative: 100, 300, 200, 250
        # Peak=300, trough=200 → drawdown=100
        self._insert_closed_trade(_in_memory_db, "D1", 100.0, "2025-01-01 10:00:00")
        self._insert_closed_trade(_in_memory_db, "D2", 200.0, "2025-01-01 11:00:00")
        self._insert_closed_trade(_in_memory_db, "D3", -100.0, "2025-01-01 12:00:00")
        self._insert_closed_trade(_in_memory_db, "D4", 50.0, "2025-01-01 13:00:00")

        m = trade_logger.update_metrics()
        assert m["max_drawdown"] == 100.0


# ------------------------------------------------------------------
# get_metrics tests
# ------------------------------------------------------------------

class TestGetMetrics:

    def _insert_closed_trade(self, conn, trade_id, pnl, entry_time,
                             instrument="CE", entry_type="CROSSOVER"):
        conn.execute(
            """INSERT INTO trades (trade_id, entry_time, symbol, instrument,
               side, qty, entry_price, exit_price, pnl, result, status, entry_type)
               VALUES (?, ?, 'BANKNIFTY', ?, 'BUY', 50, 100.0, 110.0, ?, ?, 'CLOSED', ?)""",
            (trade_id, entry_time, instrument, pnl,
             "WIN" if pnl > 0 else "LOSS",
             entry_type),
        )
        conn.commit()

    def test_filter_by_instrument(self, _in_memory_db):
        self._insert_closed_trade(_in_memory_db, "F1", 100.0, "2025-01-01 10:00:00", "CE")
        self._insert_closed_trade(_in_memory_db, "F2", 200.0, "2025-01-01 11:00:00", "PE")

        m = trade_logger.get_metrics(instrument="CE")
        assert m["total_trades"] == 1
        assert m["net_pnl"] == 100.0

    def test_filter_by_entry_type(self, _in_memory_db):
        self._insert_closed_trade(_in_memory_db, "F3", 100.0, "2025-01-01 10:00:00",
                                  entry_type="CROSSOVER")
        self._insert_closed_trade(_in_memory_db, "F4", 200.0, "2025-01-01 11:00:00",
                                  entry_type="PREBUY")

        m = trade_logger.get_metrics(entry_type="PREBUY")
        assert m["total_trades"] == 1
        assert m["net_pnl"] == 200.0

    def test_filter_by_date_range(self, _in_memory_db):
        self._insert_closed_trade(_in_memory_db, "F5", 100.0, "2025-01-01 10:00:00")
        self._insert_closed_trade(_in_memory_db, "F6", 200.0, "2025-01-15 10:00:00")
        self._insert_closed_trade(_in_memory_db, "F7", 300.0, "2025-02-01 10:00:00")

        m = trade_logger.get_metrics(date_from="2025-01-10", date_to="2025-01-20")
        assert m["total_trades"] == 1
        assert m["net_pnl"] == 200.0


# ------------------------------------------------------------------
# Error resilience
# ------------------------------------------------------------------

class TestErrorResilience:

    def test_log_entry_does_not_crash_on_db_error(self, monkeypatch):
        """If the DB connection fails, log_trade_entry should not raise."""
        monkeypatch.setattr(trade_db, "get_connection",
                            MagicMock(side_effect=RuntimeError("DB down")))
        # Should not raise
        trade_logger.log_trade_entry(
            trade_id="ERR1", symbol="BANKNIFTY", instrument="CE",
            side="BUY", qty=50, entry_price=100.0, trade_count=1,
        )

    def test_log_exit_does_not_crash_on_db_error(self, monkeypatch):
        monkeypatch.setattr(trade_db, "get_connection",
                            MagicMock(side_effect=RuntimeError("DB down")))
        trade_logger.log_trade_exit("ERR2", exit_price=100.0,
                                    result="LOSS", reason="SL")

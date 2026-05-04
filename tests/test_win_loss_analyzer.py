"""
Unit tests for app/win_loss_analyzer.py — statistical profiles, grouping,
insufficient data marking, top discriminator ranking, and filtering.
"""

import sqlite3
import statistics

import pytest

from app import config, trade_db
from app.win_loss_analyzer import (
    EntryTypeProfile,
    analyze,
    _time_of_day_numeric,
    _stats_for,
    _compute_top_discriminators,
)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _in_memory_db(monkeypatch):
    """Replace the module-level DB connection with an in-memory SQLite DB."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    trade_db.ensure_tables(conn)

    monkeypatch.setattr(trade_db, "_connection", conn)
    monkeypatch.setattr(trade_db, "get_connection", lambda: conn)

    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _default_config(monkeypatch):
    """Ensure config values are at their defaults for each test."""
    monkeypatch.setattr(config, "ANALYZER_MIN_TRADES", 10)
    monkeypatch.setattr(config, "LIVE_SYMBOL", "BANKNIFTY")


# ------------------------------------------------------------------
# DB seeding helpers
# ------------------------------------------------------------------

_TRADE_COUNTER = 0


def _next_id() -> str:
    global _TRADE_COUNTER
    _TRADE_COUNTER += 1
    return f"T_{_TRADE_COUNTER}"


def _insert_trade_with_snapshot(
    conn,
    trade_id: str | None = None,
    entry_type: str = "CROSSOVER",
    instrument: str = "CE",
    symbol: str = "BANKNIFTY",
    result: str = "WIN",
    ema_gap_atr: float = 0.8,
    price_dist_ema9_atr: float = 0.5,
    phase_duration: int = 3,
    candle_body_ratio: float = 0.33,
    time_of_day: str = "10:30",
):
    """Insert a CLOSED trade with an ENTRY snapshot."""
    if trade_id is None:
        trade_id = _next_id()
    conn.execute(
        """INSERT INTO trades
           (trade_id, entry_time, symbol, instrument, side, qty,
            entry_price, exit_price, pnl, result, status, entry_type)
           VALUES (?, '2024-06-15 10:30:00', ?, ?, 'BUY', 50,
                   100.0, 110.0, 500.0, ?, 'CLOSED', ?)""",
        (trade_id, symbol, instrument, result, entry_type),
    )
    conn.execute(
        """INSERT INTO condition_snapshots
           (trade_id, snapshot_type, ema_gap_atr, price_dist_ema9_atr,
            phase_duration, candle_body_ratio, time_of_day, entry_type)
           VALUES (?, 'ENTRY', ?, ?, ?, ?, ?, ?)""",
        (trade_id, ema_gap_atr, price_dist_ema9_atr, phase_duration,
         candle_body_ratio, time_of_day, entry_type),
    )
    conn.commit()


def _seed_group(conn, n: int, result: str = "WIN", **kwargs):
    """Insert *n* trades with the given result and snapshot values."""
    for _ in range(n):
        _insert_trade_with_snapshot(conn, result=result, **kwargs)


# ------------------------------------------------------------------
# Helper function tests
# ------------------------------------------------------------------

class TestTimeOfDayNumeric:

    def test_normal_time(self):
        assert _time_of_day_numeric("10:30") == pytest.approx(10.5)

    def test_morning_time(self):
        assert _time_of_day_numeric("09:15") == pytest.approx(9.25)

    def test_afternoon_time(self):
        assert _time_of_day_numeric("15:00") == pytest.approx(15.0)

    def test_none_returns_none(self):
        assert _time_of_day_numeric(None) is None

    def test_invalid_returns_none(self):
        assert _time_of_day_numeric("invalid") is None


class TestStatsFor:

    def test_empty_list(self):
        result = _stats_for([])
        assert result == {"mean": 0.0, "median": 0.0, "std": 0.0}

    def test_single_value(self):
        result = _stats_for([5.0])
        assert result["mean"] == pytest.approx(5.0)
        assert result["median"] == pytest.approx(5.0)
        assert result["std"] == pytest.approx(0.0)

    def test_known_values(self):
        values = [2.0, 4.0, 6.0, 8.0, 10.0]
        result = _stats_for(values)
        assert result["mean"] == pytest.approx(statistics.mean(values))
        assert result["median"] == pytest.approx(statistics.median(values))
        assert result["std"] == pytest.approx(statistics.pstdev(values))


# ------------------------------------------------------------------
# Statistical profile correctness
# ------------------------------------------------------------------

class TestStatisticalProfileCorrectness:
    """Verify that computed mean, median, std match expected values."""

    def test_win_profile_stats(self, _in_memory_db):
        """Win profile stats should match hand-computed values."""
        # Insert 5 wins with known ema_gap_atr values: 1.0, 2.0, 3.0, 4.0, 5.0
        # and 5 losses so we have enough data
        for i, val in enumerate([1.0, 2.0, 3.0, 4.0, 5.0]):
            _insert_trade_with_snapshot(
                _in_memory_db, result="WIN", ema_gap_atr=val,
            )
        for i in range(5):
            _insert_trade_with_snapshot(
                _in_memory_db, result="LOSS", ema_gap_atr=10.0,
            )

        results = analyze()
        profile = results["CROSSOVER_CE"]

        win_vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert profile.win_profile["ema_gap_atr"]["mean"] == pytest.approx(
            statistics.mean(win_vals)
        )
        assert profile.win_profile["ema_gap_atr"]["median"] == pytest.approx(
            statistics.median(win_vals)
        )
        assert profile.win_profile["ema_gap_atr"]["std"] == pytest.approx(
            statistics.pstdev(win_vals)
        )

    def test_loss_profile_stats(self, _in_memory_db):
        """Loss profile stats should match hand-computed values."""
        for i in range(5):
            _insert_trade_with_snapshot(
                _in_memory_db, result="WIN", ema_gap_atr=1.0,
            )
        for i, val in enumerate([2.0, 4.0, 6.0, 8.0, 10.0]):
            _insert_trade_with_snapshot(
                _in_memory_db, result="LOSS", ema_gap_atr=val,
            )

        results = analyze()
        profile = results["CROSSOVER_CE"]

        loss_vals = [2.0, 4.0, 6.0, 8.0, 10.0]
        assert profile.loss_profile["ema_gap_atr"]["mean"] == pytest.approx(
            statistics.mean(loss_vals)
        )
        assert profile.loss_profile["ema_gap_atr"]["median"] == pytest.approx(
            statistics.median(loss_vals)
        )
        assert profile.loss_profile["ema_gap_atr"]["std"] == pytest.approx(
            statistics.pstdev(loss_vals)
        )

    def test_time_of_day_converted_to_numeric(self, _in_memory_db):
        """time_of_day should be converted to numeric hours for stats."""
        for tod in ["09:15", "10:30", "11:45", "13:00", "14:15"]:
            _insert_trade_with_snapshot(
                _in_memory_db, result="WIN", time_of_day=tod,
            )
        for i in range(5):
            _insert_trade_with_snapshot(
                _in_memory_db, result="LOSS", time_of_day="12:00",
            )

        results = analyze()
        profile = results["CROSSOVER_CE"]

        expected_vals = [9.25, 10.5, 11.75, 13.0, 14.25]
        assert profile.win_profile["time_of_day"]["mean"] == pytest.approx(
            statistics.mean(expected_vals)
        )


# ------------------------------------------------------------------
# Grouping by entry_type and instrument
# ------------------------------------------------------------------

class TestGrouping:

    def test_separate_groups_by_entry_type(self, _in_memory_db):
        """Different entry_types should produce separate profiles."""
        _seed_group(_in_memory_db, 6, result="WIN", entry_type="CROSSOVER")
        _seed_group(_in_memory_db, 6, result="LOSS", entry_type="CROSSOVER")
        _seed_group(_in_memory_db, 6, result="WIN", entry_type="PREBUY")
        _seed_group(_in_memory_db, 6, result="LOSS", entry_type="PREBUY")

        results = analyze()
        assert "CROSSOVER_CE" in results
        assert "PREBUY_CE" in results
        assert results["CROSSOVER_CE"].entry_type == "CROSSOVER"
        assert results["PREBUY_CE"].entry_type == "PREBUY"

    def test_separate_groups_by_instrument(self, _in_memory_db):
        """Different instruments should produce separate profiles."""
        _seed_group(_in_memory_db, 6, result="WIN", instrument="CE")
        _seed_group(_in_memory_db, 6, result="LOSS", instrument="CE")
        _seed_group(_in_memory_db, 6, result="WIN", instrument="PE")
        _seed_group(_in_memory_db, 6, result="LOSS", instrument="PE")

        results = analyze()
        assert "CROSSOVER_CE" in results
        assert "CROSSOVER_PE" in results
        assert results["CROSSOVER_CE"].instrument == "CE"
        assert results["CROSSOVER_PE"].instrument == "PE"

    def test_sample_size_correct(self, _in_memory_db):
        """sample_size should count both wins and losses in the group."""
        _seed_group(_in_memory_db, 7, result="WIN")
        _seed_group(_in_memory_db, 3, result="LOSS")

        results = analyze()
        assert results["CROSSOVER_CE"].sample_size == 10


# ------------------------------------------------------------------
# Insufficient data marking
# ------------------------------------------------------------------

class TestInsufficientData:

    def test_insufficient_when_below_threshold(self, _in_memory_db):
        """Groups with fewer than ANALYZER_MIN_TRADES should be marked."""
        _seed_group(_in_memory_db, 5, result="WIN")
        _seed_group(_in_memory_db, 4, result="LOSS")

        results = analyze()
        profile = results["CROSSOVER_CE"]
        assert profile.sufficient_data is False
        assert profile.sample_size == 9

    def test_sufficient_at_threshold(self, _in_memory_db):
        """Groups with exactly ANALYZER_MIN_TRADES should be sufficient."""
        _seed_group(_in_memory_db, 5, result="WIN")
        _seed_group(_in_memory_db, 5, result="LOSS")

        results = analyze()
        profile = results["CROSSOVER_CE"]
        assert profile.sufficient_data is True
        assert profile.sample_size == 10

    def test_sufficient_above_threshold(self, _in_memory_db):
        """Groups above the threshold should be sufficient."""
        _seed_group(_in_memory_db, 10, result="WIN")
        _seed_group(_in_memory_db, 5, result="LOSS")

        results = analyze()
        assert results["CROSSOVER_CE"].sufficient_data is True

    def test_custom_threshold(self, _in_memory_db, monkeypatch):
        """Changing ANALYZER_MIN_TRADES should affect the threshold."""
        monkeypatch.setattr(config, "ANALYZER_MIN_TRADES", 5)
        _seed_group(_in_memory_db, 3, result="WIN")
        _seed_group(_in_memory_db, 2, result="LOSS")

        results = analyze()
        assert results["CROSSOVER_CE"].sufficient_data is True


# ------------------------------------------------------------------
# Top discriminator ranking
# ------------------------------------------------------------------

class TestTopDiscriminators:

    def test_top_3_returned(self, _in_memory_db):
        """Should return exactly 3 top discriminators."""
        _seed_group(_in_memory_db, 6, result="WIN")
        _seed_group(_in_memory_db, 6, result="LOSS")

        results = analyze()
        profile = results["CROSSOVER_CE"]
        assert len(profile.top_discriminators) == 3

    def test_discriminators_in_descending_order(self, _in_memory_db):
        """Top discriminators should be sorted by separation (descending)."""
        # Create wins with very different ema_gap_atr from losses
        for i in range(6):
            _insert_trade_with_snapshot(
                _in_memory_db, result="WIN",
                ema_gap_atr=1.0,
                price_dist_ema9_atr=0.5,
                candle_body_ratio=0.3,
            )
        for i in range(6):
            _insert_trade_with_snapshot(
                _in_memory_db, result="LOSS",
                ema_gap_atr=10.0,  # big difference from wins
                price_dist_ema9_atr=0.6,  # small difference
                candle_body_ratio=0.35,  # small difference
            )

        results = analyze()
        profile = results["CROSSOVER_CE"]
        seps = [d["separation"] for d in profile.top_discriminators]
        assert seps == sorted(seps, reverse=True)

    def test_discriminator_has_expected_keys(self, _in_memory_db):
        """Each discriminator dict should have feature, separation, win_mean, loss_mean."""
        _seed_group(_in_memory_db, 6, result="WIN")
        _seed_group(_in_memory_db, 6, result="LOSS")

        results = analyze()
        profile = results["CROSSOVER_CE"]
        for disc in profile.top_discriminators:
            assert "feature" in disc
            assert "separation" in disc
            assert "win_mean" in disc
            assert "loss_mean" in disc

    def test_largest_separation_is_first(self, _in_memory_db):
        """The feature with the largest win/loss separation should be first."""
        # Make ema_gap_atr have the biggest separation
        for i in range(6):
            _insert_trade_with_snapshot(
                _in_memory_db, result="WIN",
                ema_gap_atr=1.0,
                price_dist_ema9_atr=5.0,
                phase_duration=3,
                candle_body_ratio=0.5,
                time_of_day="10:30",
            )
        for i in range(6):
            _insert_trade_with_snapshot(
                _in_memory_db, result="LOSS",
                ema_gap_atr=100.0,  # huge separation
                price_dist_ema9_atr=5.1,  # tiny separation
                phase_duration=3,
                candle_body_ratio=0.5,
                time_of_day="10:30",
            )

        results = analyze()
        profile = results["CROSSOVER_CE"]
        assert profile.top_discriminators[0]["feature"] == "ema_gap_atr"

    def test_compute_top_discriminators_directly(self):
        """Test the helper function directly with known values."""
        win_profile = {
            "ema_gap_atr": {"mean": 1.0, "median": 1.0, "std": 0.1},
            "price_dist_ema9_atr": {"mean": 5.0, "median": 5.0, "std": 0.5},
            "phase_duration": {"mean": 3.0, "median": 3.0, "std": 1.0},
            "candle_body_ratio": {"mean": 0.5, "median": 0.5, "std": 0.05},
            "time_of_day": {"mean": 10.5, "median": 10.5, "std": 1.0},
        }
        loss_profile = {
            "ema_gap_atr": {"mean": 10.0, "median": 10.0, "std": 0.1},
            "price_dist_ema9_atr": {"mean": 5.1, "median": 5.1, "std": 0.5},
            "phase_duration": {"mean": 3.5, "median": 3.5, "std": 1.0},
            "candle_body_ratio": {"mean": 0.6, "median": 0.6, "std": 0.05},
            "time_of_day": {"mean": 11.0, "median": 11.0, "std": 1.0},
        }
        top = _compute_top_discriminators(win_profile, loss_profile)
        assert len(top) == 3
        # ema_gap_atr: abs(1-10)/max(0.1,0.1,0.001) = 90.0 — should be first
        assert top[0]["feature"] == "ema_gap_atr"
        assert top[0]["separation"] == pytest.approx(90.0)


# ------------------------------------------------------------------
# Filtering by entry_type and instrument
# ------------------------------------------------------------------

class TestFiltering:

    def test_filter_by_entry_type(self, _in_memory_db):
        """Passing entry_type should only return that entry_type."""
        _seed_group(_in_memory_db, 6, result="WIN", entry_type="CROSSOVER")
        _seed_group(_in_memory_db, 6, result="LOSS", entry_type="CROSSOVER")
        _seed_group(_in_memory_db, 6, result="WIN", entry_type="PREBUY")
        _seed_group(_in_memory_db, 6, result="LOSS", entry_type="PREBUY")

        results = analyze(entry_type="CROSSOVER")
        assert "CROSSOVER_CE" in results
        assert "PREBUY_CE" not in results

    def test_filter_by_instrument(self, _in_memory_db):
        """Passing instrument should only return that instrument."""
        _seed_group(_in_memory_db, 6, result="WIN", instrument="CE")
        _seed_group(_in_memory_db, 6, result="LOSS", instrument="CE")
        _seed_group(_in_memory_db, 6, result="WIN", instrument="PE")
        _seed_group(_in_memory_db, 6, result="LOSS", instrument="PE")

        results = analyze(instrument="CE")
        assert "CROSSOVER_CE" in results
        assert "CROSSOVER_PE" not in results

    def test_filter_by_both(self, _in_memory_db):
        """Passing both filters should narrow to one group."""
        _seed_group(_in_memory_db, 6, result="WIN", entry_type="CROSSOVER", instrument="CE")
        _seed_group(_in_memory_db, 6, result="LOSS", entry_type="CROSSOVER", instrument="CE")
        _seed_group(_in_memory_db, 6, result="WIN", entry_type="PREBUY", instrument="PE")
        _seed_group(_in_memory_db, 6, result="LOSS", entry_type="PREBUY", instrument="PE")

        results = analyze(entry_type="CROSSOVER", instrument="CE")
        assert len(results) == 1
        assert "CROSSOVER_CE" in results

    def test_filter_returns_empty_when_no_match(self, _in_memory_db):
        """Filtering for a non-existent entry_type should return empty."""
        _seed_group(_in_memory_db, 10, result="WIN")

        results = analyze(entry_type="NONEXISTENT")
        assert len(results) == 0

    def test_filters_by_live_symbol(self, _in_memory_db):
        """Only trades matching LIVE_SYMBOL should be included."""
        _seed_group(_in_memory_db, 6, result="WIN", symbol="BANKNIFTY")
        _seed_group(_in_memory_db, 6, result="LOSS", symbol="BANKNIFTY")
        _seed_group(_in_memory_db, 6, result="WIN", symbol="NIFTY")
        _seed_group(_in_memory_db, 6, result="LOSS", symbol="NIFTY")

        results = analyze()
        # LIVE_SYMBOL is BANKNIFTY, so only BANKNIFTY trades
        assert "CROSSOVER_CE" in results
        assert results["CROSSOVER_CE"].sample_size == 12


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------

class TestEdgeCases:

    def test_empty_database(self, _in_memory_db):
        """No trades should return empty dict."""
        results = analyze()
        assert results == {}

    def test_only_wins_no_losses(self, _in_memory_db):
        """Group with only wins should still produce a profile."""
        _seed_group(_in_memory_db, 10, result="WIN")

        results = analyze()
        profile = results["CROSSOVER_CE"]
        assert profile.sample_size == 10
        # Loss profile should have zero stats
        assert profile.loss_profile["ema_gap_atr"]["mean"] == 0.0

    def test_only_losses_no_wins(self, _in_memory_db):
        """Group with only losses should still produce a profile."""
        _seed_group(_in_memory_db, 10, result="LOSS")

        results = analyze()
        profile = results["CROSSOVER_CE"]
        assert profile.sample_size == 10
        # Win profile should have zero stats
        assert profile.win_profile["ema_gap_atr"]["mean"] == 0.0

    def test_db_error_returns_empty(self, monkeypatch):
        """If the DB connection fails, analyze should return empty dict."""
        def _raise(*args, **kwargs):
            raise RuntimeError("DB down")

        monkeypatch.setattr(trade_db, "get_connection", _raise)
        results = analyze()
        assert results == {}

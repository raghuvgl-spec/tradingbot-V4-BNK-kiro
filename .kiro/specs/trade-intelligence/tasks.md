# Implementation Plan: Trade Intelligence System

## Overview

Replace the Excel-based trade logger with a SQLite-backed intelligence system that captures rich market condition snapshots, performs historical pattern matching before entries, and computes win/loss statistical profiles. Implementation proceeds bottom-up: database layer → logging → snapshots → analysis → integration → tests.

All new modules live in `app/` alongside existing code. The test suite lives in `tests/` using pytest + Hypothesis.

## Tasks

- [x] 1. Set up database layer (`app/trade_db.py`) and configuration
  - [x] 1.1 Add Trade Intelligence configuration to `app/config.py`
    - Add `TRADE_DB_PATH`, `PATTERN_TOLERANCE_PCT`, `PATTERN_SKIP_THRESHOLD`, `PATTERN_CONFIRM_THRESHOLD`, `PATTERN_MIN_TRADES`, `PATTERN_FALLBACK_N`, `PATTERN_LOG_ONLY`, `PATTERN_RECENCY_5D_WEIGHT`, `PATTERN_RECENCY_10D_WEIGHT`, `ANALYZER_MIN_TRADES` settings reading from `.env` with defaults as specified in the design
    - _Requirements: 1.1, 5.11_

  - [x] 1.2 Create `app/trade_db.py` with schema and connection management
    - Implement `init_db()` to create the SQLite database file at `TRADE_DB_PATH`, enable WAL journal mode (`PRAGMA journal_mode=WAL`), enable foreign keys (`PRAGMA foreign_keys=ON`)
    - Implement `ensure_tables(conn)` using `CREATE TABLE IF NOT EXISTS` for `trades` and `condition_snapshots` tables with the exact schema from the design (explicit column types: REAL, INTEGER, TEXT)
    - Create indexes: `idx_snapshots_trade_id`, `idx_snapshots_entry_type`, `idx_trades_symbol`, `idx_trades_entry_type`, `idx_trades_status`
    - Implement `get_connection()` that calls `init_db()` if needed and returns a connection with `check_same_thread=False`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 9.3_

  - [ ]* 1.3 Write property tests for database initialization
    - **Property 1: Trade record serialization round-trip** — for any valid trade record, writing to the trades table and reading back produces identical field values
    - **Validates: Requirements 2.1, 9.2**
    - **Property 2: Condition snapshot serialization round-trip** — for any valid ConditionSnapshot, writing and reading back produces identical field values
    - **Validates: Requirements 9.1**

- [x] 2. Implement condition snapshot capture (`app/condition_snapshot.py`)
  - [x] 2.1 Create the `ConditionSnapshot` dataclass and capture logic
    - Define the `ConditionSnapshot` dataclass with all fields from the design (ema9, ema21, ema_gap, ema_gap_atr, ema_cycle_phase, phase_duration, price distances, price zone, candle OHLC, body ratio, range ATR, entry_type, time_of_day, index_ltp, trade_result, exit_reason)
    - Implement `capture(trade_id, snapshot_type, candle, entry_type, trade_result=None, exit_reason=None)` that reads from `STATE` (RuntimeState) and the candle to build a snapshot
    - Compute derived fields: `ema_gap_atr = abs(ema9 - ema21) / atr_value` (0.0 if ATR=0), `candle_body_ratio = abs(close - open) / (high - low)` (0.0 if range=0), `candle_range_atr = (high - low) / atr_value` (0.0 if ATR=0), `phase_duration` from `STATE.ema_gap_expanding_count` or `STATE.ema_gap_contracting_count`
    - Handle missing RuntimeState fields gracefully (fill with None)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.2_

  - [x] 2.2 Implement `to_db_row()` and `from_db_row()` serialization
    - `to_db_row(snapshot)` converts a ConditionSnapshot to a tuple matching the `condition_snapshots` table column order
    - `from_db_row(row)` reconstructs a ConditionSnapshot from a `sqlite3.Row`
    - _Requirements: 9.1_

  - [ ]* 2.3 Write property tests for snapshot computed fields
    - **Property 5: Entry snapshot completeness and computed fields** — for any valid market state and candle data, `capture()` with snapshot_type=ENTRY produces correct `ema_gap_atr`, `candle_body_ratio`, `candle_range_atr`, and `phase_duration`
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**
    - **Property 6: Exit snapshot includes result and reason** — for any trade exit, the EXIT snapshot contains all ENTRY fields plus non-null `trade_result` and `exit_reason`
    - **Validates: Requirements 4.1, 4.2**

- [x] 3. Implement trade logger (`app/trade_logger.py`)
  - [x] 3.1 Create `app/trade_logger.py` with drop-in replacement functions
    - Implement `log_trade_entry(trade_id, symbol, instrument, side, qty, entry_price, trade_count, reason, atr, sl, target, entry_type)` — validates symbol is BANKNIFTY or NIFTY, inserts trade record using `INSERT OR IGNORE` for duplicate prevention, calls `condition_snapshot.capture()` for ENTRY snapshot, wraps in try/except to log errors without crashing
    - Implement `log_trade_exit(trade_id, exit_price, result, reason, status)` — captures EXIT snapshot before updating trade record, computes PnL and duration_seconds, updates trade with exit data
    - Implement `update_metrics()` — computes total_trades, wins, losses, win_rate_percent, net_pnl, average_win, average_loss, profit_factor, max_drawdown from the trades table, returns as dict
    - Implement `get_metrics(date_from, date_to, instrument, entry_type)` — filtered version of metrics computation
    - Match exact function signatures of `logger_excel.py` for `log_trade_entry`, `log_trade_exit`, `update_metrics`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 4.3, 7.1, 7.2, 7.3, 7.4, 8.1, 8.4_

  - [ ]* 3.2 Write property tests for trade logging
    - **Property 3: Duplicate trade insertion is idempotent** — calling `log_trade_entry` twice with the same trade_id results in exactly one record
    - **Validates: Requirements 2.3**
    - **Property 4: PnL computation correctness** — for any trade with known side, entry_price, exit_price, and qty, stored PnL equals `(exit_price - entry_price) * qty` for BUY and `(entry_price - exit_price) * qty` for SELL
    - **Validates: Requirements 2.2**
    - **Property 18: Symbol validation rejects invalid symbols** — for any symbol not in {BANKNIFTY, NIFTY}, `log_trade_entry` rejects the insert
    - **Validates: Requirements 8.4**

  - [ ]* 3.3 Write property tests for metrics computation
    - **Property 16: Metrics computation correctness** — for any set of closed trades, computed metrics satisfy: total_trades == len(pnls), wins == count(pnl > 0), win_rate == wins/total * 100, net_pnl == sum(pnls), profit_factor == sum(wins)/abs(sum(losses)), max_drawdown equals largest peak-to-trough decline
    - **Validates: Requirements 7.1, 7.2**
    - **Property 17: Filtered metrics only include matching trades** — for any filter combination, computed metrics only reflect matching trades
    - **Validates: Requirements 7.4**

- [x] 4. Checkpoint — Verify core logging works
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement pattern matcher (`app/pattern_matcher.py`)
  - [x] 5.1 Create `app/pattern_matcher.py` with the `MatchResult` dataclass and `evaluate()` function
    - Define `MatchResult` dataclass with recommendation, win_count, loss_count, total_matches, win_profile, loss_profile, match_details
    - Implement `evaluate(entry_type, candle, instrument)`:
      - Query all CLOSED trades with matching entry_type, instrument, and LIVE_SYMBOL
      - Return NEUTRAL if fewer than `PATTERN_MIN_TRADES` (default 20) trades exist
      - For each condition dimension (ema_gap_atr, price_dist_ema9_atr, phase_duration, candle_body_ratio, candle_range_atr, time_of_day_bucket), check if historical value falls within `current ± PATTERN_TOLERANCE_PCT`
      - Apply recency weighting: last 5 days → `PATTERN_RECENCY_5D_WEIGHT`, last 10 days → `PATTERN_RECENCY_10D_WEIGHT`, older → 1.0
      - Compute weighted win/loss counts
      - If no tolerance matches → fall back to nearest `PATTERN_FALLBACK_N` trades by weighted Euclidean distance
      - If weighted loss ratio > `PATTERN_SKIP_THRESHOLD` → SKIP; if weighted win ratio > `PATTERN_CONFIRM_THRESHOLD` → CONFIRM; else → NEUTRAL
    - Implement time-of-day bucketing: 30-minute buckets from 09:15 to 15:30
    - Log every recommendation with win_count, loss_count, total_matches, and profile values
    - Support `PATTERN_LOG_ONLY` mode where recommendations are logged but don't block trades
    - All matching done in-memory after a single SQL query for <100ms performance
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10, 5.11, 5.12, 8.2_

  - [ ]* 5.2 Write property tests for pattern matching — filtering and thresholds
    - **Property 7: Pattern matcher filters by entry_type, instrument, and symbol** — evaluate() only considers trades matching the query parameters
    - **Validates: Requirements 5.1, 8.2**
    - **Property 8: Tolerance band matching correctness** — a historical snapshot is a "match" iff every dimension's value falls within `current_value * (1 ± tolerance_pct)`
    - **Validates: Requirements 5.2**
    - **Property 9: Recommendation follows win/loss ratio thresholds** — SKIP when loss ratio > skip_threshold, CONFIRM when win ratio > confirm_threshold, NEUTRAL otherwise
    - **Validates: Requirements 5.5, 5.6**

  - [ ]* 5.3 Write property tests for pattern matching — fallback and recency
    - **Property 10: Fallback to nearest N trades when no tolerance matches** — when no trades match within tolerance, uses N nearest trades by weighted Euclidean distance
    - **Validates: Requirements 5.7**
    - **Property 11: NEUTRAL recommendation when insufficient history** — fewer than min_trades returns NEUTRAL regardless of conditions
    - **Validates: Requirements 5.8**
    - **Property 12: Recency weighting correctness** — weight is 2.0 for last 5 days, 1.5 for last 10 days, 1.0 otherwise
    - **Validates: Requirements 5.9**

- [x] 6. Implement win/loss analyzer (`app/win_loss_analyzer.py`)
  - [x] 6.1 Create `app/win_loss_analyzer.py` with `EntryTypeProfile` and `analyze()` function
    - Define `EntryTypeProfile` dataclass with entry_type, instrument, sample_size, sufficient_data, win_profile, loss_profile, top_discriminators
    - Implement `analyze(entry_type=None, instrument=None)`:
      - Query condition_snapshots joined with trades, filtered by LIVE_SYMBOL
      - Group by (entry_type, instrument)
      - For each group, compute mean, median, std for winning and losing trades across: ema_gap_atr, price_dist_ema9_atr, phase_duration, candle_body_ratio, time_of_day
      - Mark groups with fewer than `ANALYZER_MIN_TRADES` (default 10) as `insufficient_data=True`
      - Compute top 3 discriminating features by `abs(win_mean - loss_mean) / max(win_std, loss_std, 0.001)`
    - Return results as dict keyed by `"{entry_type}_{instrument}"`
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 8.3_

  - [ ]* 6.2 Write property tests for win/loss analyzer
    - **Property 13: Win/loss statistical profile correctness** — computed mean, median, std match expected values within floating-point tolerance
    - **Validates: Requirements 6.1**
    - **Property 14: Analyzer grouping and insufficient data marking** — results partitioned by (entry_type, instrument), groups with <10 trades marked insufficient_data=True
    - **Validates: Requirements 6.2, 6.5, 8.3**
    - **Property 15: Top discriminator ranking** — top 3 features are the 3 dimensions with largest separation metric, in descending order
    - **Validates: Requirements 6.4**

- [x] 7. Checkpoint — Verify all modules work independently
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Integration — Wire into existing bot
  - [x] 8.1 Update `app/orders.py` to use Trade Intelligence
    - Change import from `from app.logger_excel import log_trade_entry, log_trade_exit` to `from app.trade_logger import log_trade_entry, log_trade_exit`
    - In `enter_trade()`, add call to `pattern_matcher.evaluate(entry_type, candle, instrument)` before placing the order
    - If `PATTERN_LOG_ONLY` is False and recommendation is SKIP, block the trade and return False
    - If `PATTERN_LOG_ONLY` is True, log the recommendation but proceed regardless
    - _Requirements: 2.4, 5.1, 5.11_

  - [x] 8.2 Initialize database on bot startup
    - Call `trade_db.init_db()` during bot initialization (in `run_bot.py` or `bot.py` startup sequence)
    - _Requirements: 1.1, 1.2, 1.3_

  - [ ]* 8.3 Write integration tests for end-to-end flow
    - Test full flow: entry signal → pattern match → trade entry → snapshot → exit → metrics
    - Test LOG_ONLY mode: recommendation logged but trade not blocked
    - Test DB error resilience: mock DB failure, verify bot continues
    - Test API signature compatibility with `logger_excel.py`
    - _Requirements: 2.4, 2.5, 5.11_

- [ ] 9. Set up test infrastructure and shared fixtures
  - [x] 9.1 Create `tests/conftest.py` with Hypothesis strategies and shared fixtures
    - Create `tests/` directory and `conftest.py`
    - Define Hypothesis strategies: `valid_trade` (valid symbols BANKNIFTY/NIFTY, instruments CE/PE/FUT, sides BUY/SELL, positive prices, valid entry_types), `valid_snapshot` (realistic float ranges, valid enums), `valid_candle` (consistent OHLC: low ≤ open,close ≤ high, positive ATR)
    - Create pytest fixture for in-memory SQLite database (`:memory:`) with schema initialized
    - Create fixture for mock RuntimeState with configurable EMA/ATR/phase values
    - _Requirements: 9.1, 9.2_

- [x] 10. Final checkpoint — Full test suite passes
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design (18 properties mapped across tasks)
- The import change in `orders.py` (task 8.1) is the only modification to existing files besides `config.py` (task 1.1) and bot startup (task 8.2)
- All new modules use in-memory SQLite for testing — no test artifacts on disk

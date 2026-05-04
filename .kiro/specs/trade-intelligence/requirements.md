# Requirements Document

## Introduction

The Trade Intelligence System adds a data-driven intelligence layer to the existing EMA-based BankNifty/Nifty options trading bot. It replaces the current Excel-based trade logging with a SQLite database, captures rich market condition snapshots at every entry and exit, stores LTP tick data, and uses historical pattern analysis to filter entries — skipping trades that match losing patterns and confirming trades that match winning patterns.

## Glossary

- **Trade_Intelligence_DB**: The SQLite database that stores all trade records, condition snapshots, tick data, and pattern analysis results
- **Condition_Snapshot**: A structured record of all market parameters captured at the moment of trade entry or exit (EMA gap, cycle phase, price distance, candle metrics, etc.)
- **Pattern_Matcher**: The module that compares current market conditions against historical trade outcomes to produce a match score
- **Trade_Logger**: The module that replaces the existing Excel logger (`logger_excel.py`) with SQLite-backed trade logging
- **Win_Loss_Analyzer**: The module that computes statistical profiles of winning vs losing trades across multiple condition dimensions
- **EMA_Gap**: The absolute difference between EMA9 and EMA21 values
- **EMA_Cycle_Phase**: One of SIDEWAYS, EXPANDING, PEAK, or CONTRACTING — describes the lifecycle stage of the EMA gap
- **Phase_Duration**: The number of consecutive candles the EMA cycle has remained in its current phase
- **Price_Zone**: Classification of price position relative to EMA9 — one of OVEREXTENDED, STRETCHED, NORMAL, or NEAR_EMA9
- **Price_EMA9_Momentum**: Direction of price-to-EMA9 distance change — one of EXPANDING, SHRINKING, or FLAT
- **ATR_Multiple**: A distance measurement normalized by the current ATR value (e.g., price distance from EMA9 expressed as a ratio of ATR)
- **Entry_Type**: The classification of how a trade was entered — one of CROSSOVER, PREBUY, PRESELL, TREND, HIGHER_LOW, REENTRY, or REEXPANSION
- **Match_Score**: A numerical score (0.0 to 1.0) representing how closely current conditions match a historical pattern profile

## Requirements

### Requirement 1: SQLite Database Initialization

**User Story:** As a trader, I want trade data stored in a SQLite database instead of Excel files, so that I can query and analyze trade history efficiently.

#### Acceptance Criteria

1. WHEN the trading bot starts, THE Trade_Intelligence_DB SHALL create the database file at a configurable path under the `data/` directory if the file does not exist
2. WHEN the database is created, THE Trade_Intelligence_DB SHALL create tables for trades, condition_snapshots, and pattern_analysis with appropriate schemas and indexes
3. THE Trade_Intelligence_DB SHALL use WAL journal mode to allow concurrent reads during bot operation
4. IF the database file exists but is missing required tables, THEN THE Trade_Intelligence_DB SHALL create the missing tables without altering existing data
5. THE Trade_Intelligence_DB SHALL define a foreign key relationship from condition_snapshots to trades using trade_id

### Requirement 2: Trade Logging to SQLite

**User Story:** As a trader, I want every trade entry and exit logged to the database with the same fields currently captured in Excel, so that I have a complete and queryable trade history.

#### Acceptance Criteria

1. WHEN a trade entry occurs, THE Trade_Logger SHALL insert a record into the trades table containing trade_id, entry_time, symbol, instrument (CE/PE/FUT), side, quantity, entry_price, trade_count, status (OPEN), ATR, stop_loss, target, and entry_type
2. WHEN a trade exit occurs, THE Trade_Logger SHALL update the corresponding trades record with exit_time, exit_price, PnL, result (WIN/LOSS/FLAT), exit_reason, status (CLOSED), and duration_seconds
3. THE Trade_Logger SHALL prevent duplicate trade entries by checking trade_id uniqueness before insertion
4. THE Trade_Logger SHALL expose the same function signatures as the existing `logger_excel.py` module (`log_trade_entry`, `log_trade_exit`, `update_metrics`) so that callers in `orders.py` require minimal changes
5. IF a database write fails, THEN THE Trade_Logger SHALL log the error and continue bot operation without crashing

### Requirement 3: Condition Snapshot Capture at Entry

**User Story:** As a trader, I want the exact market conditions recorded at every trade entry, so that I can later analyze what conditions produce winning vs losing trades.

#### Acceptance Criteria

1. WHEN a trade entry occurs, THE Trade_Logger SHALL insert a condition_snapshot record linked to the trade_id with snapshot_type set to ENTRY
2. THE Condition_Snapshot SHALL contain: ema9 value, ema21 value, ema_gap, ema_gap_atr (gap as ATR multiple), ema_cycle_phase, phase_duration (candles in current phase), price_distance_ema9, price_distance_ema21, price_dist_ema9_atr, price_dist_ema21_atr, price_ema9_momentum, price_zone, candle_open, candle_high, candle_low, candle_close, candle_body_ratio, candle_range_atr (range as ATR multiple), atr_value, entry_type, time_of_day, and index_ltp
3. THE Condition_Snapshot SHALL compute ema_gap_atr as abs(ema9 - ema21) / ATR
4. THE Condition_Snapshot SHALL compute candle_body_ratio as abs(close - open) / (high - low) when candle range is greater than zero, and 0.0 when candle range is zero
5. THE Condition_Snapshot SHALL compute phase_duration from the ema_gap_expanding_count or ema_gap_contracting_count values in RuntimeState depending on the current ema_cycle_phase

### Requirement 4: Condition Snapshot Capture at Exit

**User Story:** As a trader, I want the market conditions at trade exit also recorded, so that I can analyze how conditions changed during the trade.

#### Acceptance Criteria

1. WHEN a trade exit occurs, THE Trade_Logger SHALL insert a condition_snapshot record linked to the trade_id with snapshot_type set to EXIT
2. THE exit Condition_Snapshot SHALL contain the same fields as the entry snapshot, plus the trade result (WIN/LOSS/FLAT) and exit_reason
3. THE Trade_Logger SHALL capture the exit snapshot before updating the trades table, so that both records reference consistent state

### Requirement 5: Pattern Matching Before Entry

**User Story:** As a trader, I want the bot to compare current market conditions against historical winning and losing trade patterns before entering a trade, so that it avoids repeating losing patterns and enters with higher confidence on winning patterns.

#### Acceptance Criteria

1. WHEN a trade entry signal is generated (before the order is placed), THE Pattern_Matcher SHALL query historical condition_snapshots for trades with the same entry_type and instrument
2. THE Pattern_Matcher SHALL find similar historical trades using range-based matching — each condition dimension (ema_gap_atr, price_dist_ema9_atr, phase_duration, candle_body_ratio, candle_range_atr, time_of_day_bucket) SHALL match if the historical value falls within a configurable tolerance band (default: ±20%) of the current value
3. THE Pattern_Matcher SHALL compute an average condition profile from all matching winning trades and a separate average profile from all matching losing trades
4. THE Pattern_Matcher SHALL compare the current conditions against both average profiles using weighted distance — the closer profile determines the recommendation
5. IF the number of matching losing trades exceeds a configurable threshold percentage (default: 70%) of all matching trades, THEN THE Pattern_Matcher SHALL return a SKIP recommendation with the match details
6. IF the number of matching winning trades exceeds a configurable threshold percentage (default: 60%) of all matching trades, THEN THE Pattern_Matcher SHALL return a CONFIRM recommendation with the match details
7. IF no exact matches are found within the tolerance band, THE Pattern_Matcher SHALL fall back to the nearest N trades (configurable, default N=10) using weighted Euclidean distance
8. WHILE fewer than 20 historical trades exist for the given entry_type, THE Pattern_Matcher SHALL return a NEUTRAL recommendation and allow the trade to proceed without filtering
9. THE Pattern_Matcher SHALL apply recency weighting — trades from the last 5 trading days SHALL receive 2x weight, trades from the last 10 days SHALL receive 1.5x weight, and older trades SHALL receive 1x weight
10. THE Pattern_Matcher SHALL log every recommendation (SKIP/CONFIRM/NEUTRAL) with the win_count, loss_count, total_matches, and the average winning and losing profile values so the trader can audit decisions
11. THE Pattern_Matcher SHALL support a LOG_ONLY mode (configurable via .env, default: True) where recommendations are logged but do not block trades — allowing validation before trusting the system with real filtering
12. THE Pattern_Matcher SHALL complete the matching computation within 100 milliseconds to avoid delaying trade execution

### Requirement 6: Win/Loss Pattern Analysis

**User Story:** As a trader, I want statistical analysis of what market conditions correlate with winning vs losing trades, so that I can understand and improve my strategy.

#### Acceptance Criteria

1. THE Win_Loss_Analyzer SHALL compute separate statistical profiles (mean, median, standard deviation) for winning trades and losing trades across: ema_gap_atr at entry, price_dist_ema9_atr at entry, phase_duration at entry, candle_body_ratio at entry, and time_of_day
2. THE Win_Loss_Analyzer SHALL group analysis by entry_type (CROSSOVER, PREBUY, PRESELL, TREND, HIGHER_LOW, REENTRY, REEXPANSION) and by instrument (CE/PE)
3. WHEN the Win_Loss_Analyzer is invoked, THE Win_Loss_Analyzer SHALL return results as a dictionary keyed by entry_type containing win_profile and loss_profile sub-dictionaries
4. THE Win_Loss_Analyzer SHALL identify the top 3 discriminating features (features with the largest separation between win and loss distributions) and include them in the analysis output
5. WHILE fewer than 10 trades exist for a given entry_type, THE Win_Loss_Analyzer SHALL mark that entry_type's analysis as insufficient_data

### Requirement 7: Metrics Computation

**User Story:** As a trader, I want summary trading metrics computed from the database, so that I can track overall performance without relying on Excel.

#### Acceptance Criteria

1. WHEN metrics are requested, THE Trade_Logger SHALL compute from the trades table: total_trades, wins, losses, win_rate_percent, net_pnl, average_win, average_loss, profit_factor, and max_drawdown
2. THE Trade_Logger SHALL compute max_drawdown as the largest peak-to-trough decline in the cumulative PnL equity curve
3. THE Trade_Logger SHALL return metrics as a dictionary for programmatic consumption
4. THE Trade_Logger SHALL support filtering metrics by date range, instrument, and entry_type

### Requirement 8: Scope Restriction

**User Story:** As a trader, I want the system to operate exclusively on BankNifty and Nifty instruments, so that no crude oil or other instrument data pollutes the analysis.

#### Acceptance Criteria

1. THE Trade_Intelligence_DB SHALL store a symbol field in every trades record identifying the index (BANKNIFTY or NIFTY)
2. THE Pattern_Matcher SHALL filter historical queries by the current LIVE_SYMBOL configuration value
3. THE Win_Loss_Analyzer SHALL filter analysis by the current LIVE_SYMBOL configuration value
4. IF a trade record contains a symbol other than BANKNIFTY or NIFTY, THEN THE Trade_Intelligence_DB SHALL reject the insert and log a warning

### Requirement 9: Database Schema Serialization Round-Trip

**User Story:** As a developer, I want to verify that condition snapshots can be written to and read from the database without data loss, so that analysis operates on accurate data.

#### Acceptance Criteria

1. FOR ALL valid Condition_Snapshot objects, writing to the database and reading back SHALL produce a Condition_Snapshot with identical field values (round-trip property)
2. FOR ALL valid trade records, writing to the database and reading back SHALL produce a trade record with identical field values (round-trip property)
3. THE Trade_Intelligence_DB SHALL use explicit column types (REAL for floats, INTEGER for ints, TEXT for strings) to prevent silent type coercion

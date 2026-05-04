# Requirements Document

## Introduction

This document specifies the requirements for an intraday trading bot system that trades BANKNIFTY options and Crude Oil futures via the Angel Broking (SmartAPI) platform. The Trading_Bot operates on 1-minute candle data, uses EMA crossover-based signal detection with a 5-priority entry hierarchy, and manages positions through a layered exit system. The system supports both paper trading and live execution, persists state across restarts, reconciles with the broker on startup, and provides a real-time Streamlit monitoring dashboard.

## Glossary

- **Trading_Bot**: The main orchestration system that coordinates startup, candle restoration, broker reconciliation, indicator computation, signal detection, order execution, and position management.
- **Broker_Adapter**: The module responsible for authenticating with Angel Broking SmartAPI, managing WebSocket connections for live tick data, fetching historical candles, and resolving option symbols.
- **Signal_Engine**: The strategy module that evaluates 1-minute candle data against a 5-priority entry hierarchy (PREBUY/PRESELL, CROSSOVER, TREND_CONTINUATION, HIGHER_LOW/LOWER_HIGH, REENTRY) to generate trade signals.
- **Order_Manager**: The module that executes market orders, manages open positions, computes SL/target levels, and applies the exit hierarchy (Hard SL, EMA rejection, Reversal, Giveback, Target).
- **Indicator_Engine**: The module that computes EMA9, EMA21, ATR14, and VWAP from candle data and updates runtime state.
- **State_Manager**: The thread-safe runtime state container holding all trading state (positions, indicators, counters, flags) protected by a threading lock.
- **Persistence_Layer**: The module responsible for reading/writing CSV market data, JSON bot state, Excel trade logs, and tick logs to disk.
- **Dashboard**: The Streamlit-based live monitoring interface displaying candlestick charts, EMA overlays, trade markers, position details, and bot controls.
- **EMA9**: Exponential Moving Average with period 9 (fast EMA), used for trend direction and entry/exit signals.
- **EMA21**: Exponential Moving Average with period 21 (slow EMA), used for trend confirmation and rejection exits.
- **ATR14**: Average True Range with period 14, used for dynamic SL/target calculation, sideways filtering, and giveback thresholds.
- **VWAP**: Volume Weighted Average Price, computed daily, currently disabled in signal filtering but persisted in data files.
- **Sideways_Filter**: A gate that blocks entries when EMA gap < ATR * MIN_EMA_GAP_ATR, candle range < ATR * MIN_CANDLE_RANGE_ATR, or body ratio < MIN_BODY_RATIO.
- **Giveback_Exit**: An ATR-based percentage trailing exit that activates after a 30-point move from entry and is suppressed when the trend is strong.
- **Cooldown**: A configurable waiting period (default 5 minutes) after any exit before new entries are allowed, bypassed only by PREBUY/PRESELL signals.
- **Paper_Trading**: A mode where orders are simulated locally without sending real orders to the broker.
- **Candle_Snapshot**: A frozen representation of a 1-minute candle with OHLCV data plus computed EMA9, EMA21, VWAP, and ATR values.

## Requirements

### Requirement 1: Broker Authentication and Session Management

**User Story:** As a trader, I want the Trading_Bot to authenticate with Angel Broking on startup, so that it can place orders and receive live market data.

#### Acceptance Criteria

1. WHEN the Trading_Bot starts, THE Broker_Adapter SHALL authenticate with Angel Broking SmartAPI using API_KEY, CLIENT_ID, PASSWORD, and a TOTP token generated from TOTP_SECRET.
2. WHEN authentication succeeds, THE Broker_Adapter SHALL store the JWT auth token and feed token in the State_Manager.
3. IF authentication fails, THEN THE Broker_Adapter SHALL raise a RuntimeError and halt startup.
4. WHEN the Trading_Bot starts on a Saturday or Sunday in live mode, THE Broker_Adapter SHALL exit the process without attempting authentication.

### Requirement 2: Instrument Master Loading

**User Story:** As a trader, I want the Trading_Bot to load the instrument master list, so that it can resolve option symbols and tokens for order placement.

#### Acceptance Criteria

1. WHEN the Trading_Bot starts, THE Broker_Adapter SHALL attempt to load the instrument master from a local JSON cache file (data/instrument_master.json).
2. IF the cache file exists and is valid, THEN THE Broker_Adapter SHALL use the cached data without downloading.
3. IF the cache file is missing or corrupt, THEN THE Broker_Adapter SHALL download the instrument master from the Angel Broking OpenAPI endpoint with up to 3 retry attempts.
4. WHEN the download succeeds, THE Broker_Adapter SHALL save the data to the cache file for future use.
5. IF all 3 download attempts fail, THEN THE Broker_Adapter SHALL raise a RuntimeError and halt startup.

### Requirement 3: Option Symbol Resolution

**User Story:** As a trader, I want the Trading_Bot to automatically select the correct option contract, so that trades are placed on the appropriate strike and expiry.

#### Acceptance Criteria

1. WHEN an entry signal is generated, THE Broker_Adapter SHALL resolve the option symbol by rounding the current index LTP to the nearest 100-point strike.
2. THE Broker_Adapter SHALL select CE options for BUY trend signals and PE options for SELL trend signals.
3. WHEN the instrument is BANKNIFTY, THE Broker_Adapter SHALL select the next calendar month's monthly expiry contract.
4. WHEN the instrument is NIFTY, THE Broker_Adapter SHALL select the nearest weekly expiry contract.
5. THE Broker_Adapter SHALL exclude expired contracts (expiry date before today) from selection.

### Requirement 4: WebSocket Tick Data Connection

**User Story:** As a trader, I want the Trading_Bot to receive real-time tick data, so that candles are built from live prices and positions are monitored tick-by-tick.

#### Acceptance Criteria

1. WHEN the Trading_Bot is ready to trade, THE Broker_Adapter SHALL establish a WebSocket connection using SmartWebSocketV2 with the auth token, API key, client ID, and feed token.
2. WHEN the WebSocket connects, THE Broker_Adapter SHALL subscribe to the index token (BANKNIFTY or Crude Oil depending on mode).
3. WHILE an open position exists at WebSocket connect time, THE Broker_Adapter SHALL also subscribe to the position's option token for real-time premium updates.
4. WHEN a tick is received for the subscribed index token, THE Broker_Adapter SHALL update the LTP in the State_Manager and log the tick to the tick CSV file.
5. WHEN a tick is received for the position's option token, THE Broker_Adapter SHALL update the position's current_ltp, mtm_points, and mtm_pnl fields.
6. IF the WebSocket connection closes unexpectedly, THEN THE Broker_Adapter SHALL wait 3 seconds, re-login, and reconnect the WebSocket.
7. IF a WebSocket error occurs, THEN THE Broker_Adapter SHALL update the state status to WS_ERROR.

### Requirement 5: 1-Minute Candle Building from Ticks

**User Story:** As a trader, I want the Trading_Bot to build 1-minute candles from live ticks, so that the strategy engine can evaluate signals on completed candles.

#### Acceptance Criteria

1. WHEN a tick arrives, THE Broker_Adapter SHALL aggregate the tick into the current 1-minute candle (updating high, low, close, and cumulative volume).
2. WHEN the tick's minute key differs from the current candle's minute key, THE Broker_Adapter SHALL close the current candle, append it to the closed candles list, and start a new candle.
3. WHEN a candle closes, THE Broker_Adapter SHALL trigger the Signal_Engine strategy loop.
4. THE Broker_Adapter SHALL retain a maximum of 500 closed candles in memory.
5. WHEN no current candle exists but closed candles are available, THE Broker_Adapter SHALL resume from the last closed candle rather than creating a new one from scratch.

### Requirement 6: Indicator Computation (EMA9, EMA21, ATR14, VWAP)

**User Story:** As a trader, I want the Trading_Bot to compute technical indicators on each candle close, so that the strategy engine has accurate EMA, ATR, and VWAP values for signal evaluation.

#### Acceptance Criteria

1. WHEN a candle closes, THE Indicator_Engine SHALL compute EMA9 and EMA21 using exponential moving average with multiplier 2/(period+1).
2. THE Indicator_Engine SHALL compute ATR14 as the simple average of the last 14 true ranges (max of high-low, |high-prev_close|, |low-prev_close|).
3. THE Indicator_Engine SHALL compute VWAP as cumulative (typical_price * volume) / cumulative volume, resetting at each new trading day.
4. THE Indicator_Engine SHALL store current and previous EMA values in the State_Manager for crossover detection.
5. IF fewer candles than the EMA period are available, THEN THE Indicator_Engine SHALL compute a simple average of available closes as a fallback.
6. THE Indicator_Engine SHALL write the computed EMA9 and EMA21 series back to the market data CSV file for dashboard display.

### Requirement 7: Sideways Market Filter

**User Story:** As a trader, I want the Trading_Bot to block entries during sideways/choppy markets, so that trades are only taken when a clear trend exists.

#### Acceptance Criteria

1. THE Signal_Engine SHALL block entry signals when the absolute EMA9-EMA21 gap is less than ATR14 multiplied by MIN_EMA_GAP_ATR (default 0.25).
2. THE Signal_Engine SHALL block entry signals when the candle range (high minus low) is less than ATR14 multiplied by MIN_CANDLE_RANGE_ATR (default 0.50).
3. THE Signal_Engine SHALL block entry signals when the candle body ratio (|close-open| / range) is less than MIN_BODY_RATIO (default 0.45).
4. IF ATR14 is unavailable or zero, THEN THE Signal_Engine SHALL treat the market as sideways and block entries.

### Requirement 8: Priority 1 Entry — PREBUY and PRESELL Signals

**User Story:** As a trader, I want the Trading_Bot to detect pre-crossover convergence signals, so that entries can be taken early before the EMA crossover completes.

#### Acceptance Criteria

1. WHEN EMA9 is below EMA21 on both the previous and current candle (not yet crossed), THE Signal_Engine SHALL evaluate PREBUY conditions.
2. THE Signal_Engine SHALL generate a PREBUY signal when the EMA gap is shrinking (gap_shrink >= PREBUY_MIN_GAP_SHRINK) or price is between the two EMAs or near either EMA (within ATR * 0.30), and EMA9 is rising, and the candle is bullish (close >= open), and the Sideways_Filter passes.
3. WHEN EMA9 is above EMA21 on both the previous and current candle (not yet crossed bearish), THE Signal_Engine SHALL evaluate PRESELL conditions.
4. THE Signal_Engine SHALL generate a PRESELL signal when the EMA gap is shrinking or price is in the convergence zone, and EMA9 is falling, and the candle is bearish (close < open), and the Sideways_Filter passes.
5. WHEN a PREBUY or PRESELL signal fires, THE Signal_Engine SHALL bypass the post-exit cooldown timer.
6. WHEN a PREBUY or PRESELL signal fires, THE Signal_Engine SHALL reset trend state (reentry flags, crossover_missed, first_retrace_done) for the new trend direction.

### Requirement 9: Priority 2 Entry — EMA Crossover Signals

**User Story:** As a trader, I want the Trading_Bot to detect EMA9/EMA21 crossovers, so that trend changes trigger trade entries.

#### Acceptance Criteria

1. WHEN the previous candle had EMA9 <= EMA21 and the current candle has EMA9 > EMA21, THE Signal_Engine SHALL detect a bullish crossover.
2. WHEN the previous candle had EMA9 >= EMA21 and the current candle has EMA9 < EMA21, THE Signal_Engine SHALL detect a bearish crossover.
3. WHEN a crossover is detected, THE Signal_Engine SHALL validate it through the valid_safe_crossover gate requiring ATR >= MIN_ATR_THRESHOLD (12), EMA gap >= ATR * 0.05, and the Sideways_Filter passes.
4. IF the crossover fails the valid_safe_crossover gate, THEN THE Signal_Engine SHALL set the crossover_missed flag to true and skip the entry.
5. WHEN a valid crossover fires, THE Signal_Engine SHALL reset trend state for the new direction.

### Requirement 10: Priority 3 Entry — Trend Continuation Signals

**User Story:** As a trader, I want the Trading_Bot to enter trades during strong established trends, so that momentum moves are captured even without a fresh crossover.

#### Acceptance Criteria

1. WHILE the last trend side is BUY, THE Signal_Engine SHALL generate a TREND continuation signal when EMA9 > EMA21, close > EMA9, EMA gap >= ATR * 0.50, distance from close to EMA9 <= ATR * 0.80, the candle is bullish, candle body >= ATR * 0.30, and the Sideways_Filter passes.
2. WHILE the last trend side is SELL, THE Signal_Engine SHALL generate a TREND continuation signal when EMA9 < EMA21, close < EMA9, EMA gap >= ATR * 0.50, distance from close to EMA9 <= ATR * 0.80, the candle is bearish, candle body >= ATR * 0.30, and the Sideways_Filter passes.

### Requirement 11: Priority 4 Entry — Higher Low / Lower High Retrace Signals

**User Story:** As a trader, I want the Trading_Bot to enter on pullbacks to EMA9 during established trends, so that retracement entries are captured at favorable prices.

#### Acceptance Criteria

1. WHILE the last trend side is BUY, THE Signal_Engine SHALL generate a HIGHER_LOW_BUY signal when EMA9 > EMA21, EMA gap >= ATR * 0.25, the candle low touched the EMA9 zone (within ATR * 0.30 above and below EMA9), close is above EMA9, and the Sideways_Filter passes.
2. WHILE the last trend side is SELL, THE Signal_Engine SHALL generate a LOWER_HIGH_SELL signal when EMA9 < EMA21, EMA gap >= ATR * 0.25, the candle high touched the EMA9 zone (within ATR * 0.30 above and below EMA9), close is below EMA9, and the Sideways_Filter passes.

### Requirement 12: Priority 5 Entry — Re-Entry After Stop Loss

**User Story:** As a trader, I want the Trading_Bot to re-enter after a stop loss hit if the trend is still valid, so that premature exits do not cause missed moves.

#### Acceptance Criteria

1. WHEN a position is closed by stop loss and the trend (EMA alignment) still matches the trade direction, THE Order_Manager SHALL arm re-entry with a reference price (highest_price for BUY, lowest_price for SELL) and a time window of REENTRY_WINDOW_SEC (600 seconds).
2. THE Signal_Engine SHALL allow a maximum of MAX_REENTRIES (2) re-entries per armed window.
3. THE Signal_Engine SHALL validate re-entry quality: EMA gap >= ATR * 0.25, price gap from EMA9 >= ATR * 0.15, candle body ratio >= 0.35, pullback from anchor <= ATR * 1.2, reclaim above/below EMA9 >= ATR * 0.20, and momentum break past previous candle high/low + ATR * 0.10 buffer.
4. THE Signal_Engine SHALL apply a reduced cooldown (COOLDOWN_MINUTES minus 2 minutes) before allowing re-entry.
5. IF the re-entry time window expires, THEN THE Signal_Engine SHALL disarm re-entry and clear the reference price.

### Requirement 13: Risk Management Gates

**User Story:** As a trader, I want the Trading_Bot to enforce daily risk limits, so that catastrophic losses are prevented.

#### Acceptance Criteria

1. THE Signal_Engine SHALL block all new entries when trade_count reaches MAX_TRADES (50).
2. THE Signal_Engine SHALL block all new entries when realized_pnl falls to or below MAX_DAILY_LOSS (-15000).
3. THE Signal_Engine SHALL block all new entries when consecutive_sl reaches MAX_CONSECUTIVE_SL (25).
4. THE Signal_Engine SHALL block all new entries outside the configured trading window (START_TIME to END_TIME).
5. WHEN a new trading day begins, THE Signal_Engine SHALL reset trade_count, consecutive_sl, and last_trade_day.

### Requirement 14: Post-Exit Cooldown

**User Story:** As a trader, I want the Trading_Bot to wait after exiting a trade before entering a new one, so that emotional re-entries are avoided.

#### Acceptance Criteria

1. WHEN a position is closed, THE Signal_Engine SHALL enforce a cooldown of COOLDOWN_MINUTES (default 7) before allowing new entries.
2. WHEN a PREBUY or PRESELL signal is detected, THE Signal_Engine SHALL bypass the cooldown and allow immediate entry.
3. WHEN a GIVEBACK exit occurs, THE Order_Manager SHALL enforce the standard cooldown and disarm any re-entry window.

### Requirement 15: Trade Entry Execution

**User Story:** As a trader, I want the Trading_Bot to execute market orders for entries, so that signals are converted into actual positions.

#### Acceptance Criteria

1. WHEN an entry signal is confirmed, THE Order_Manager SHALL resolve the option symbol and token, fetch the option LTP, and place a market BUY order.
2. THE Order_Manager SHALL compute SL as entry_price minus max(ATR * ATR_SL_MULTIPLIER * premium_multiplier, SL_POINTS) and target as entry_price plus max(ATR * ATR_TARGET_MULTIPLIER * premium_multiplier, TARGET_POINTS).
3. IF the current position is not empty or a pending trade exists, THEN THE Order_Manager SHALL block the entry.
4. IF ATR is unavailable or zero, THEN THE Order_Manager SHALL block the entry.
5. IF the option LTP is unavailable or zero, THEN THE Order_Manager SHALL block the entry.
6. WHEN Paper_Trading is enabled, THE Order_Manager SHALL generate a simulated order ID without calling the broker API.
7. WHEN an entry succeeds, THE Order_Manager SHALL log the entry to the Excel trade journal, write market data with the signal, update bot state, and subscribe to the option token on the WebSocket.

### Requirement 16: Exit Hierarchy — Hard Stop Loss

**User Story:** As a trader, I want the Trading_Bot to exit immediately when price hits the stop loss, so that losses are capped.

#### Acceptance Criteria

1. WHILE a BUY position is open, THE Order_Manager SHALL exit the position when the option LTP falls to or below the SL price.
2. WHILE a SELL position is open, THE Order_Manager SHALL exit the position when the option LTP rises to or above the SL price.
3. THE Order_Manager SHALL continuously update the trailing SL as highest_price minus ATR * ATR_TRAIL_MULTIPLIER (for BUY) and lowest_price plus ATR * ATR_TRAIL_MULTIPLIER (for SELL), never moving the SL against the trade direction.

### Requirement 17: Exit Hierarchy — EMA Rejection Exits

**User Story:** As a trader, I want the Trading_Bot to exit when price rejects off key EMA levels, so that losing positions are closed before the stop loss is hit.

#### Acceptance Criteria

1. WHILE a PREBUY or PRESELL position has been held for at least 60 seconds, THE Order_Manager SHALL exit when the candle close is on the wrong side of EMA9 (below EMA9 for BUY trend, above EMA9 for SELL trend).
2. WHILE any position has been held for at least 120 seconds, THE Order_Manager SHALL exit when the candle close is on the wrong side of EMA21 (below EMA21 for BUY trend, above EMA21 for SELL trend).
3. THE Broker_Adapter SHALL perform tick-level EMA21 rejection checks: when the index tick price touches or crosses EMA21 on the wrong side and the position has been held for at least 120 seconds, THE Broker_Adapter SHALL trigger an immediate exit.

### Requirement 18: Exit Hierarchy — Reversal Exit

**User Story:** As a trader, I want the Trading_Bot to exit when a strong EMA reversal occurs, so that positions are closed when the trend changes.

#### Acceptance Criteria

1. WHILE a BUY position has been held for at least 300 seconds, THE Order_Manager SHALL exit when EMA9 crosses below EMA21 and the EMA gap exceeds ATR * 0.60.
2. WHILE a SELL position has been held for at least 300 seconds, THE Order_Manager SHALL exit when EMA9 crosses above EMA21 and the EMA gap exceeds ATR * 0.60.

### Requirement 19: Exit Hierarchy — Giveback (Swing) Exit

**User Story:** As a trader, I want the Trading_Bot to lock in profits using a percentage-based trailing exit, so that large unrealized gains are not given back entirely.

#### Acceptance Criteria

1. WHEN the move from entry exceeds 30 points and the trend is not strong, THE Order_Manager SHALL compute a swing exit level as recent_extreme minus move * giveback_pct (12% if move < ATR, 15% otherwise), floored at entry_price.
2. WHEN the option LTP retraces to or past the swing exit level, THE Order_Manager SHALL exit the position with reason GIVEBACK.
3. WHILE the trend is strong (EMA9 expanding from EMA21 by at least ATR * 0.15, price above EMA21 for BUY or below EMA21 for SELL), THE Order_Manager SHALL suppress the Giveback_Exit and allow the position to run.
4. THE Broker_Adapter SHALL perform tick-level giveback checks on every option tick, applying the same suppression logic.

### Requirement 20: Trend Direction Auto-Sync

**User Story:** As a trader, I want the Trading_Bot to keep the trend direction synchronized with EMA alignment, so that continuation and retrace signals use the correct direction.

#### Acceptance Criteria

1. WHEN EMA9 is above EMA21 and the last_trend_side is not BUY, THE Signal_Engine SHALL update last_trend_side to BUY.
2. WHEN EMA9 is below EMA21 and the last_trend_side is not SELL, THE Signal_Engine SHALL update last_trend_side to SELL.

### Requirement 21: Candle Restoration and Warm-Up on Startup

**User Story:** As a trader, I want the Trading_Bot to restore candle history on startup, so that indicators are warm and signals can fire immediately.

#### Acceptance Criteria

1. WHEN the Trading_Bot starts, THE Persistence_Layer SHALL restore up to 500 recent candles from the market data CSV into runtime memory.
2. WHEN a new trading day is detected (last saved candle date differs from today), THE Trading_Bot SHALL clear previous day runtime candles and rebuild the full session from 09:15 to now using the historical candle API.
3. WHEN the same trading day is detected with a candle gap, THE Trading_Bot SHALL fetch missing candles from the API and merge them into the existing data.
4. WHEN STARTUP_PREWARM_ENABLED is true and fewer candles than STARTUP_PREWARM_CANDLES (60) are available, THE Trading_Bot SHALL fetch additional historical candles to warm up indicators.
5. THE Trading_Bot SHALL set a startup_cutoff_time and skip signal evaluation on any candle with a timestamp at or before the cutoff, preventing false signals from restored data.

### Requirement 22: Broker Position Reconciliation on Startup

**User Story:** As a trader, I want the Trading_Bot to detect and adopt any open broker position on startup, so that positions are not orphaned after a restart.

#### Acceptance Criteria

1. WHEN RECONCILE_WITH_BROKER_ON_STARTUP is true and Paper_Trading is false, THE Broker_Adapter SHALL query the broker's position API for any open positions with non-zero net quantity.
2. WHEN an open broker position is found, THE Broker_Adapter SHALL rebuild SL and target using ATR (or fixed SL/TARGET as fallback), seed trailing prices from tick history or candle history, and adopt the position into runtime state.
3. WHEN an open broker position is found, THE Broker_Adapter SHALL attempt to determine the entry time from the broker's order book, falling back to the locally saved entry time.
4. WHEN no open broker position is found, THE Broker_Adapter SHALL clear any stale position from runtime state.

### Requirement 23: State Persistence and Restoration

**User Story:** As a trader, I want the Trading_Bot to persist its state to disk, so that it can resume correctly after a restart.

#### Acceptance Criteria

1. THE Persistence_Layer SHALL write bot state (positions, PnL, trade count, block reasons, candle keys) to bot_state.json on every state change.
2. WHEN the Trading_Bot starts in AUTO mode on the same trading day, THE Persistence_Layer SHALL restore the previous state from bot_state.json.
3. WHEN the Trading_Bot starts in AUTO mode on a new trading day, THE Persistence_Layer SHALL reset state (PnL, consecutive SL, block reasons) while preserving any open position.
4. WHEN the Trading_Bot starts in AUTO mode, THE Persistence_Layer SHALL cross-check the trade log Excel for a higher trade count than JSON state and rebuild from the trade log if needed.
5. WHEN the Trading_Bot starts in FRESH mode, THE Persistence_Layer SHALL reset all runtime state.
6. WHEN the Trading_Bot starts in RESUME mode, THE Persistence_Layer SHALL restore the previous state unconditionally.

### Requirement 24: Market Data CSV Persistence

**User Story:** As a trader, I want the Trading_Bot to persist candle data to CSV files, so that the dashboard can display historical charts and indicators can be recomputed.

#### Acceptance Criteria

1. THE Persistence_Layer SHALL write each closed candle's OHLCV data, EMA9, EMA21, VWAP, and signal to the instrument-specific market data CSV (market_data_banknifty.csv or market_data_crude.csv).
2. WHEN a candle row already exists for the same timestamp, THE Persistence_Layer SHALL update OHLCV and indicator values in place, preserving existing labeled signals (CE BUY, PE SELL, etc.) over generic signals (BUY, SELL).
3. THE Persistence_Layer SHALL normalize all timestamps to the format YYYY-MM-DD HH:MM:SS and deduplicate rows by time.

### Requirement 25: Trade Journal (Excel) Logging

**User Story:** As a trader, I want the Trading_Bot to log every trade entry and exit to an Excel file, so that I have a complete trade journal for review.

#### Acceptance Criteria

1. WHEN a trade entry occurs, THE Persistence_Layer SHALL append a row to the Trades sheet with TradeID, EntryTime, Symbol, Instrument, Side, Qty, Entry price, ATR, SL, Target, EntryType, TradeCount, and Status=OPEN.
2. WHEN a trade exit occurs, THE Persistence_Layer SHALL update the matching row with ExitTime, Exit price, PnL, Result (WIN/LOSS/FLAT), Reason, Status=CLOSED, and DurationSec.
3. THE Persistence_Layer SHALL prevent duplicate trade entries by checking TradeID before appending.
4. WHEN a trade exit is logged, THE Persistence_Layer SHALL recompute the Metrics sheet with Total Trades, Wins, Losses, Win Rate, Net PnL, Avg Win, Avg Loss, Profit Factor, and Max Drawdown.

### Requirement 26: Tick Data Logging

**User Story:** As a trader, I want the Trading_Bot to log every tick to a CSV file, so that tick-level analysis and trailing price reconstruction are possible.

#### Acceptance Criteria

1. WHEN a tick is received for the subscribed index token, THE Persistence_Layer SHALL append a row with timestamp (including microseconds), LTP, and last traded quantity to the instrument-specific tick log CSV.

### Requirement 27: Configuration via Environment Variables

**User Story:** As a trader, I want all Trading_Bot parameters to be configurable via a .env file, so that I can tune the strategy without modifying code.

#### Acceptance Criteria

1. THE Trading_Bot SHALL load all configuration parameters from a .env file at the project root, with sensible defaults for every parameter.
2. THE Trading_Bot SHALL validate on startup that LOTS > 0, LOT_SIZE > 0, QTY is a multiple of LOT_SIZE, PARTIAL_LOTS >= 0, and PARTIAL_LOTS <= LOTS, raising ValueError for violations.
3. THE Trading_Bot SHALL support switching between BANKNIFTY options mode and Crude Oil futures mode via the DEBUG_MODE flag.

### Requirement 28: Instrument-Specific Data Files

**User Story:** As a trader, I want the Trading_Bot to maintain separate data files per instrument, so that BANKNIFTY and Crude Oil data do not interfere with each other.

#### Acceptance Criteria

1. THE Persistence_Layer SHALL use the instrument tag (banknifty or crude) in file names for market data CSV, trade log Excel, and tick log CSV.
2. WHEN DEBUG_MODE is true, THE Persistence_Layer SHALL use the "crude" tag for all data files.
3. WHEN DEBUG_MODE is false, THE Persistence_Layer SHALL use the LIVE_SYMBOL (lowercased) tag for all data files.

### Requirement 29: Dashboard — Live Monitoring

**User Story:** As a trader, I want a real-time dashboard showing the live chart, indicators, position details, and trade history, so that I can monitor the bot's activity.

#### Acceptance Criteria

1. THE Dashboard SHALL display a candlestick chart with EMA9 and EMA21 overlay lines, refreshing every 3 seconds.
2. THE Dashboard SHALL display trade entry and exit markers on the chart using data from the trade log Excel.
3. THE Dashboard SHALL display key metrics: WebSocket status, LTP, trade count, net PnL, win rate, bot status, and MTM PnL.
4. WHILE an open position exists, THE Dashboard SHALL display position details: symbol, quantity, entry price, SL, current LTP, and MTM points.
5. THE Dashboard SHALL provide an instrument selector dropdown allowing the user to switch between BANKNIFTY and Crude Oil views.
6. THE Dashboard SHALL provide bot control buttons: Start/Resume, Stop, Full Restart, Reset Risk Block, and Refresh.
7. THE Dashboard SHALL display recent trades and recent market data tables filtered to today's date.

### Requirement 30: Paper Trading Mode

**User Story:** As a trader, I want the Trading_Bot to support a paper trading mode, so that I can test the strategy without risking real money.

#### Acceptance Criteria

1. WHEN PAPER_TRADING is true, THE Order_Manager SHALL generate simulated order IDs (PAPER_BUY_timestamp / PAPER_SELL_timestamp) instead of calling the broker API.
2. WHEN PAPER_TRADING is true, THE Broker_Adapter SHALL skip broker position reconciliation on startup.
3. THE Trading_Bot SHALL persist paper trade state and trade logs identically to live mode, enabling full strategy evaluation.

### Requirement 31: Thread Safety

**User Story:** As a trader, I want the Trading_Bot to handle concurrent tick processing and state updates safely, so that race conditions do not corrupt trading state.

#### Acceptance Criteria

1. THE State_Manager SHALL protect all state mutations with a threading Lock.
2. THE Broker_Adapter SHALL acquire the State_Manager lock before updating candle data, LTP, or position fields from WebSocket tick callbacks.
3. THE Broker_Adapter SHALL use a reconnecting flag to prevent duplicate reconnection attempts and suppress tick processing during reconnection.

### Requirement 32: Market Hours Enforcement

**User Story:** As a trader, I want the Trading_Bot to respect market hours, so that it does not attempt to trade when the market is closed.

#### Acceptance Criteria

1. WHEN the Trading_Bot starts before market open, THE Trading_Bot SHALL wait in a polling loop (checking every 30 seconds) until market_is_open returns true.
2. THE Signal_Engine SHALL block all entry signals outside the configured trading window (START_TIME to END_TIME).

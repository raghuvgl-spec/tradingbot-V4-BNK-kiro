"""
Database initialization, connection management, and schema for the
Trade Intelligence System.

Uses SQLite with WAL journal mode for concurrent read access.
"""

import sqlite3
import logging

from app import config

logger = logging.getLogger(__name__)

# Module-level connection for reuse
_connection: sqlite3.Connection | None = None


# ------------------------------------------------------------------
# Schema DDL
# ------------------------------------------------------------------

_CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    entry_time TEXT NOT NULL,
    exit_time TEXT,
    symbol TEXT NOT NULL,
    instrument TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    pnl REAL,
    result TEXT,
    reason TEXT,
    trade_count INTEGER,
    status TEXT NOT NULL DEFAULT 'OPEN',
    equity REAL,
    atr REAL,
    sl REAL,
    target REAL,
    entry_type TEXT,
    duration_seconds INTEGER,
    peak_profit REAL,
    left_on_table REAL,
    capture_pct REAL,
    highest_price REAL,
    lowest_price REAL,
    trade_mode TEXT NOT NULL DEFAULT 'LIVE',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_MARKET_DATA_TABLE = """
CREATE TABLE IF NOT EXISTS market_data_banknifty (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL DEFAULT 0.0,
    ema20 REAL,
    ema50 REAL,
    vwap REAL,
    signal TEXT,
    UNIQUE(time)
);
"""

_CREATE_SIGNAL_REJECTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS signal_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candle_time TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    side TEXT NOT NULL,
    rejection_reason TEXT NOT NULL,
    ema_cycle_phase TEXT,
    leading_phase TEXT,
    phase_alignment TEXT,
    ema_gap REAL,
    ema_gap_atr REAL,
    price_dist_ema9 REAL,
    price_dist_ema9_atr REAL,
    open_dist_ema9 REAL,
    open_dist_ema9_atr REAL,
    candle_open REAL,
    candle_high REAL,
    candle_low REAL,
    candle_close REAL,
    atr_value REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_CONDITION_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS condition_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL,
    snapshot_type TEXT NOT NULL,
    ema9 REAL,
    ema21 REAL,
    ema_gap REAL,
    ema_gap_atr REAL,
    ema_cycle_phase TEXT,
    phase_duration INTEGER,
    price_distance_ema9 REAL,
    price_distance_ema21 REAL,
    price_dist_ema9_atr REAL,
    price_dist_ema21_atr REAL,
    open_dist_ema9 REAL,
    open_dist_ema9_atr REAL,
    price_ema9_momentum TEXT,
    price_zone TEXT,
    candle_open REAL,
    candle_high REAL,
    candle_low REAL,
    candle_close REAL,
    candle_body_ratio REAL,
    candle_range_atr REAL,
    atr_value REAL,
    entry_type TEXT,
    time_of_day TEXT,
    index_ltp REAL,
    trade_result TEXT,
    exit_reason TEXT,
    leading_phase TEXT,
    leading_phase_duration INTEGER,
    phase_alignment TEXT,
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_snapshots_trade_id ON condition_snapshots(trade_id);",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_entry_type ON condition_snapshots(entry_type, snapshot_type);",
    "CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_trades_entry_type ON trades(entry_type);",
    "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);",
    "CREATE INDEX IF NOT EXISTS idx_trades_trade_mode ON trades(trade_mode);",
    "CREATE INDEX IF NOT EXISTS idx_market_data_bnf_time ON market_data_banknifty(time);",
    "CREATE INDEX IF NOT EXISTS idx_rejections_candle_time ON signal_rejections(candle_time);",
    "CREATE INDEX IF NOT EXISTS idx_rejections_signal_type ON signal_rejections(signal_type);",
]


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create missing tables and indexes without altering existing data."""
    conn.execute(_CREATE_TRADES_TABLE)
    conn.execute(_CREATE_MARKET_DATA_TABLE)
    conn.execute(_CREATE_SIGNAL_REJECTIONS_TABLE)
    conn.execute(_CREATE_CONDITION_SNAPSHOTS_TABLE)

    # Migrate first, then create indexes (indexes may reference new columns)
    _migrate_leading_phase_columns(conn)
    _migrate_trade_mode_column(conn)
    _migrate_open_dist_columns(conn)
    _migrate_trades_exit_columns(conn)

    for idx_sql in _CREATE_INDEXES:
        conn.execute(idx_sql)

    conn.commit()


def _migrate_leading_phase_columns(conn: sqlite3.Connection) -> None:
    """Add leading_phase, leading_phase_duration, phase_alignment columns
    to condition_snapshots if they don't already exist."""
    cursor = conn.execute("PRAGMA table_info(condition_snapshots)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    migrations = [
        ("leading_phase", "TEXT"),
        ("leading_phase_duration", "INTEGER"),
        ("phase_alignment", "TEXT"),
    ]
    for col_name, col_type in migrations:
        if col_name not in existing_cols:
            conn.execute(
                f"ALTER TABLE condition_snapshots ADD COLUMN {col_name} {col_type}"
            )
            logger.info("Migrated condition_snapshots: added column %s", col_name)


def _migrate_trade_mode_column(conn: sqlite3.Connection) -> None:
    """Add trade_mode column to trades if it doesn't already exist.

    Values: LIVE (real trade), PAPER (paper-first validation),
    SHADOW (signal tracked but not traded).
    """
    cursor = conn.execute("PRAGMA table_info(trades)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    if "trade_mode" not in existing_cols:
        conn.execute(
            "ALTER TABLE trades ADD COLUMN trade_mode TEXT NOT NULL DEFAULT 'LIVE'"
        )
        logger.info("Migrated trades: added column trade_mode")


def _migrate_open_dist_columns(conn: sqlite3.Connection) -> None:
    """Add open_dist_ema9 and open_dist_ema9_atr columns to condition_snapshots
    if they don't already exist."""
    cursor = conn.execute("PRAGMA table_info(condition_snapshots)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    migrations = [
        ("open_dist_ema9", "REAL"),
        ("open_dist_ema9_atr", "REAL"),
    ]
    for col_name, col_type in migrations:
        if col_name not in existing_cols:
            conn.execute(
                f"ALTER TABLE condition_snapshots ADD COLUMN {col_name} {col_type}"
            )
            logger.info("Migrated condition_snapshots: added column %s", col_name)


def _migrate_trades_exit_columns(conn: sqlite3.Connection) -> None:
    """Add peak_profit, left_on_table, capture_pct, highest_price, lowest_price
    columns to trades if they don't already exist."""
    cursor = conn.execute("PRAGMA table_info(trades)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    migrations = [
        ("peak_profit", "REAL"),
        ("left_on_table", "REAL"),
        ("capture_pct", "REAL"),
        ("highest_price", "REAL"),
        ("lowest_price", "REAL"),
    ]
    for col_name, col_type in migrations:
        if col_name not in existing_cols:
            conn.execute(
                f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}"
            )
            logger.info("Migrated trades: added column %s", col_name)


def init_db() -> None:
    """Create the database file (if needed), enable WAL mode and foreign
    keys, and ensure all tables exist."""
    global _connection

    db_path = config.TRADE_DB_PATH

    # Ensure the parent directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    ensure_tables(conn)

    _connection = conn
    logger.info("Trade Intelligence DB initialised at %s", db_path)


def get_connection() -> sqlite3.Connection:
    """Return the module-level connection, initialising the DB first if
    it has not been set up yet."""
    global _connection
    if _connection is None:
        init_db()
    return _connection  # type: ignore[return-value]


# ------------------------------------------------------------------
# Market data persistence
# ------------------------------------------------------------------

_UPSERT_MARKET_CANDLE = """
INSERT INTO market_data_banknifty (time, open, high, low, close, volume, ema20, ema50, vwap, signal)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(time) DO UPDATE SET
    open   = excluded.open,
    high   = excluded.high,
    low    = excluded.low,
    close  = excluded.close,
    volume = excluded.volume,
    ema20  = excluded.ema20,
    ema50  = excluded.ema50,
    vwap   = excluded.vwap,
    signal = excluded.signal
"""


def save_market_candle(
    time_str: str,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 0.0,
    ema20: float | None = None,
    ema50: float | None = None,
    vwap: float | None = None,
    signal: str | None = None,
) -> None:
    """Insert or update a single candle in market_data_banknifty."""
    try:
        conn = get_connection()
        conn.execute(
            _UPSERT_MARKET_CANDLE,
            (time_str, open_, high, low, close, volume, ema20, ema50, vwap, signal),
        )
        conn.commit()
    except Exception:
        logger.exception("save_market_candle failed for %s", time_str)


def save_market_candles_bulk(rows: list[tuple]) -> None:
    """Bulk insert/update candles. Each row is a tuple matching
    (time, open, high, low, close, volume, ema20, ema50, vwap, signal)."""
    try:
        conn = get_connection()
        conn.executemany(_UPSERT_MARKET_CANDLE, rows)
        conn.commit()
    except Exception:
        logger.exception("save_market_candles_bulk failed")


# ------------------------------------------------------------------
# Signal rejection logging
# ------------------------------------------------------------------

_INSERT_REJECTION = """
INSERT INTO signal_rejections (
    candle_time, signal_type, side, rejection_reason,
    ema_cycle_phase, leading_phase, phase_alignment,
    ema_gap, ema_gap_atr, price_dist_ema9, price_dist_ema9_atr,
    open_dist_ema9, open_dist_ema9_atr,
    candle_open, candle_high, candle_low, candle_close, atr_value
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def log_signal_rejection(
    candle_time: str,
    signal_type: str,
    side: str,
    rejection_reason: str,
    ema_cycle_phase: str | None = None,
    leading_phase: str | None = None,
    phase_alignment: str | None = None,
    ema_gap: float | None = None,
    ema_gap_atr: float | None = None,
    price_dist_ema9: float | None = None,
    price_dist_ema9_atr: float | None = None,
    open_dist_ema9: float | None = None,
    open_dist_ema9_atr: float | None = None,
    candle_open: float | None = None,
    candle_high: float | None = None,
    candle_low: float | None = None,
    candle_close: float | None = None,
    atr_value: float | None = None,
) -> None:
    """Log a signal rejection for future analysis."""
    try:
        conn = get_connection()
        conn.execute(
            _INSERT_REJECTION,
            (
                candle_time, signal_type, side, rejection_reason,
                ema_cycle_phase, leading_phase, phase_alignment,
                ema_gap, ema_gap_atr, price_dist_ema9, price_dist_ema9_atr,
                open_dist_ema9, open_dist_ema9_atr,
                candle_open, candle_high, candle_low, candle_close, atr_value,
            ),
        )
        conn.commit()
    except Exception:
        logger.exception("log_signal_rejection failed for %s at %s", signal_type, candle_time)

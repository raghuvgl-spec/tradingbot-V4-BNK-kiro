from threading import Lock
from collections import deque
from datetime import date


class RuntimeState:
    def __init__(self):
        self.current_trade_id = None
        self.trade_id_counter = 0

        self.obj = None
        self.auth_token = None
        self.feed_token = None
        self.ws = None

        self.ltp = None
        self.ws_connected = False
        self.reconnecting = False

        self.trade_count = 0
        self.last_trade_day = None
        self.last_candle_key = None

        self.ema20 = None
        self.ema50 = None
        self.prev_ema20 = None
        self.prev_ema50 = None
        self.vwap = None
        self.atr = None

        self.current_position = None
        self.pending_trade = None
        self.reentry_allowed = False
        self.reentry_allowed_until = None
        self.reentry_reason = None
        self.reentry_count = 0
        self.reentry_reference_price = None
        self.last_trend_side = None
        self.crossover_missed = False
        self.first_retrace_done = False
        self.last_exit_reason = None

        # ─── EMA CYCLE TRACKING ──────────────────────────────────────
        self.ema_cycle_phase = "SIDEWAYS"      # SIDEWAYS / EXPANDING / PEAK / CONTRACTING
        self.ema_cycle_peak_gap = 0.0          # max EMA gap in current trend cycle
        self.ema_gap_prev = 0.0                # previous candle's EMA gap (for direction)
        self.ema_gap_expanding_count = 0       # consecutive candles of gap widening
        self.ema_gap_contracting_count = 0     # consecutive candles of gap narrowing

        # ─── PRICE RECLAIM TRACKING ──────────────────────────────────
        # Tracks when price was below both EMAs and reclaims above them (or vice versa)
        # This signals strong momentum reversal — boosts entry confidence
        self.price_was_below_emas = False      # price was below both EMA9 & EMA21
        self.price_was_above_emas = False      # price was above both EMA9 & EMA21
        self.price_reclaim_buy = False         # price reclaimed above both EMAs from below
        self.price_reclaim_sell = False        # price reclaimed below both EMAs from above

        # ─── TREND RE-EXPANSION TRACKING ─────────────────────────────
        # When cycle goes CONTRACTING → EXPANDING without crossover = trend survived
        self.trend_reexpansion = False         # True when CONTRACTING → EXPANDING detected

        # ─── LEADING CYCLE TRACKING (price − EMA9) ───────────────────
        self.leading_phase = "SIDEWAYS"        # SIDEWAYS / EXPANDING_UP / PEAKED_UP / COMPRESSING_DOWN / CROSSED_DOWN / EXPANDING_DOWN / PEAKED_DOWN / COMPRESSING_UP / CROSSED_UP
        self.leading_phase_duration = 0        # candles in current leading phase
        self.leading_peak_dist = 0.0           # max abs(price - EMA9) in current cycle
        self.leading_prev_abs_dist = 0.0       # previous candle's abs(price - EMA9)

        # ─── PRICE DISTANCE FROM EMAs ────────────────────────────────
        self.price_dist_ema9 = 0.0             # close - EMA9 (signed)
        self.price_dist_ema21 = 0.0            # close - EMA21 (signed)
        self.price_dist_ema9_atr = 0.0         # distance in ATR multiples
        self.price_dist_ema21_atr = 0.0        # distance in ATR multiples
        self.open_dist_ema9 = 0.0              # open - EMA9 (signed)
        self.open_dist_ema9_atr = 0.0          # open distance in ATR multiples
        self.price_zone = "NORMAL"             # OVEREXTENDED / STRETCHED / NEAR_EMA9 / NORMAL
        self.prev_price_dist_ema9 = 0.0        # previous candle's close - EMA9

        self.last_entry_eval_candle_key = None
        self.last_tick_time = None
        self.last_action = None
        self.last_signal = None
        self.instrument_data = None
        self.startup_cutoff_time = None

        self.history_buffer = deque(maxlen=500)
        self.session_candles = []
        self.session_date = date.today()
        self.latest_closed_candles = []
        self.current_candle = None

        self.realized_pnl = 0.0
        self.consecutive_sl = 0
        self.last_exit_time = None
        self.bot_block_reason = None
        self.candle_retry_after = None
        self.shutting_down = False
        self.lock = Lock()


STATE = RuntimeState()

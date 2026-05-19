from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import datetime, timedelta

from app.config import (
    COOLDOWN_MINUTES,
    FAST_EMA_PERIOD,
    MAX_CONSECUTIVE_SL,
    MAX_DAILY_LOSS,
    MAX_TRADES,
    MIN_ATR_THRESHOLD,
    SLOW_EMA_PERIOD,
    MAX_REENTRIES,
    REENTRY_EMA_GAP_MULTIPLIER,
    REENTRY_PRICE_GAP_MULTIPLIER,
    REENTRY_MAX_PULLBACK_ATR,
    REENTRY_MIN_RECLAIM_BODY_ATR,
    REENTRY_BREAK_BUFFER_ATR,
    REENTRY_MIN_CANDLE_BODY_RATIO,
    REENTRY_MIN_EMA_GAP_ATR,
    USE_PREBUY_ENTRY,
    PREBUY_MAX_EMA_GAP,
    PREBUY_MIN_GAP_SHRINK,
    MIN_EMA_GAP_ATR,
    MIN_CANDLE_RANGE_ATR,
    MIN_BODY_RATIO,
)
from app.state import STATE
from app.indicators import update_indicators
from app.files import write_market_data, write_state
from app.utils import trading_window_open
from app.trade_db import log_signal_rejection
from app.condition_snapshot import _compute_phase_alignment
import pandas as pd

@dataclass
class CandleSnapshot:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    fast_ema: float | None
    slow_ema: float | None
    vwap: float | None
    atr: float | None
    is_crossover: bool = False


def _make_candle(raw, fast_ema, slow_ema, vwap, atr):
    return CandleSnapshot(
        time=str(raw[0]),
        open=float(raw[1]),
        high=float(raw[2]),
        low=float(raw[3]),
        close=float(raw[4]),
        volume=float(raw[5]) if len(raw) > 5 else 0.0,
        fast_ema=fast_ema,
        slow_ema=slow_ema,
        vwap=vwap,
        atr=atr,
    )


def _log_rejection(candle: CandleSnapshot, signal_type: str, side: str, reason: str):
    """Log a signal rejection to the database for future analysis."""
    try:
        ema_gap = abs(candle.fast_ema - candle.slow_ema) if candle.fast_ema and candle.slow_ema else None
        ema_gap_atr = ema_gap / candle.atr if ema_gap is not None and candle.atr and candle.atr > 0 else None
        alignment = _compute_phase_alignment(
            getattr(STATE, "leading_phase", None),
            getattr(STATE, "ema_cycle_phase", None),
        )
        log_signal_rejection(
            candle_time=candle.time,
            signal_type=signal_type,
            side=side,
            rejection_reason=reason,
            ema_cycle_phase=getattr(STATE, "ema_cycle_phase", None),
            leading_phase=getattr(STATE, "leading_phase", None),
            phase_alignment=alignment,
            ema_gap=ema_gap,
            ema_gap_atr=ema_gap_atr,
            price_dist_ema9=getattr(STATE, "price_dist_ema9", None),
            price_dist_ema9_atr=getattr(STATE, "price_dist_ema9_atr", None),
            open_dist_ema9=getattr(STATE, "open_dist_ema9", None),
            open_dist_ema9_atr=getattr(STATE, "open_dist_ema9_atr", None),
            candle_open=candle.open,
            candle_high=candle.high,
            candle_low=candle.low,
            candle_close=candle.close,
            atr_value=candle.atr,
        )
    except Exception:
        pass  # never block strategy for logging failures


def _is_overextended(candle: CandleSnapshot) -> bool:
    """Block entries when price is overextended from EMA9 (>1.5x ATR)."""
    if candle.atr is None or candle.atr <= 0 or candle.fast_ema is None:
        return False
    dist = abs(candle.close - candle.fast_ema)
    return dist > candle.atr * 1.5


def _reentry_quality_ok(side: str, candle: CandleSnapshot, prev_candle: CandleSnapshot) -> tuple[bool, str]:
    """Loose reentry check — if trend is intact and price bounces back, allow it."""
    if candle.atr is None or candle.atr <= 0:
        return False, "ATR unavailable"

    if candle.fast_ema is None or candle.slow_ema is None:
        return False, "EMA unavailable"

    candle_range = max(0.0, candle.high - candle.low)
    candle_body = abs(candle.close - candle.open)
    body_ratio = (candle_body / candle_range) if candle_range > 0 else 0.0

    # Minimum body ratio — avoid doji/indecision candles
    if body_ratio < 0.30:
        return False, f"Weak candle body ratio {body_ratio:.2f}"

    if side == "BUY":
        # Trend must still be intact
        if candle.fast_ema < candle.slow_ema:
            return False, "Trend not bullish"
        # Price must be above EMA9 (bounced back)
        if candle.close < candle.fast_ema:
            return False, f"Price below EMA9 {candle.close:.2f} < {candle.fast_ema:.2f}"
        # Candle must be bullish
        if candle.close < candle.open:
            return False, "Candle not bullish"
    else:
        # Trend must still be intact
        if candle.fast_ema > candle.slow_ema:
            return False, "Trend not bearish"
        # Price must be below EMA9 (bounced back)
        if candle.close > candle.fast_ema:
            return False, f"Price above EMA9 {candle.close:.2f} > {candle.fast_ema:.2f}"
        # Candle must be bearish
        if candle.close > candle.open:
            return False, "Candle not bearish"

    return True, "OK"

def _risk_blocks() -> str | None:
    today = datetime.now().date()
    if STATE.last_trade_day != today:
        STATE.trade_count = 0
        STATE.last_trade_day = today
        STATE.consecutive_sl = 0
    
    
    if not trading_window_open():
        return "OUTSIDE_TRADING_WINDOW"
    if STATE.trade_count >= MAX_TRADES:
        return "MAX_TRADES_REACHED"
    if STATE.realized_pnl <= MAX_DAILY_LOSS:
        return "MAX_DAILY_LOSS_HIT"
    if STATE.consecutive_sl >= MAX_CONSECUTIVE_SL:
        return "MAX_CONSECUTIVE_SL_HIT"
    return None

def valid_safe_crossover(candle):
    """Crossover entry requires EMA gap expansion — not just the cross itself.
    
    The cross is detected and tracked (crossover_missed=True), but actual entry
    waits for gap to expand, confirming the trend is real. This prevents entries
    in sideways chop where EMAs cross but never separate.
    """
    if candle.fast_ema is None or candle.slow_ema is None:
        return False
    ema_gap = abs(candle.fast_ema - candle.slow_ema)
    return (
        candle.atr >= MIN_ATR_THRESHOLD
        and ema_gap >= candle.atr * 0.03
        and not _sideways_filter(candle)
    )

def strategy_loop():
    candles = getattr(STATE, "latest_closed_candles", [])
    if len(candles) < max(SLOW_EMA_PERIOD + 2, FAST_EMA_PERIOD + 2):
        return

    if not update_indicators():
        return
    import math

    prev_fast = getattr(STATE, "prev_ema20", None)
    prev_slow = getattr(STATE, "prev_ema50", None)
    fast = getattr(STATE, "ema20", None)
    slow = getattr(STATE, "ema50", None)
    vwap = getattr(STATE, "vwap", None)
    atr = getattr(STATE, "atr", None)
    


    #VWAP Making NONE to disable 
    if vwap is None or math.isnan(vwap):
        print("⚠️ VWAP invalid → skipping VWAP filter")
        vwap = None  # important: normalize

    USE_VWAP = False
    if not USE_VWAP:
        vwap = None
    #if None in (prev_fast, prev_slow, fast,vwap, slow,atr):
    if None in (prev_fast, prev_slow, fast, slow,atr):
        return
    candle = _make_candle(candles[-1], fast, slow, vwap, atr)
    cutoff = getattr(STATE, "startup_cutoff_time", None)
    if cutoff:
        try:
            candle_time = pd.to_datetime(candle.time)
            if candle_time <= cutoff:
                print(
                    f"⏳ STARTUP BLOCK: skipping restored candle "
                    f"{candle_time.strftime('%H:%M:%S')} | "
                    f"cutoff={cutoff.strftime('%H:%M:%S')}"
                )
                return
        except Exception as e:
            print(f"⚠️ Time parse error: {e}")
            return
    prev_candle = _make_candle(candles[-2], prev_fast, prev_slow, vwap, atr)
    from app.orders import enter_trade, manage_open_position

    # ─── UPDATE EMA CYCLE PHASE (every candle, even in position) ──────────
    _update_ema_cycle(candle, prev_candle)

    # ─── PRICE RECLAIM TRACKING ──────────────────────────────────────────
    # Track when price crosses back above/below both EMAs — strong momentum signal
    _update_price_reclaim(candle)

    # ─── PRICE DISTANCE FROM EMAs (observation + entry/exit intelligence) ─────
    _log_price_distance(candle)

    pos = STATE.current_position

    if pos:
        token = pos.get("token")
        if not token:
            print("⚠️ Missing token in current_position — skipping position management")
            return
        manage_open_position(candle)
        return

    if STATE.pending_trade:
        return
    block = _risk_blocks()
    if block:
        STATE.bot_block_reason = block
        write_state(block)
        return

    # ─── POST-EXIT COOLDOWN ───────────────────────────────────────────────────
    # Prebuy/presell bypass cooldown — they're high quality signals
    in_cooldown = False
    last_exit_time = getattr(STATE, "last_exit_time", None)
    if last_exit_time is not None:
        # Ensure last_exit_time is a datetime (may be string after state restore)
        if isinstance(last_exit_time, str):
            try:
                last_exit_time = datetime.strptime(last_exit_time, "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    last_exit_time = datetime.fromisoformat(last_exit_time)
                except Exception:
                    last_exit_time = None
        if last_exit_time is not None:
            cooldown_until = last_exit_time + timedelta(minutes=COOLDOWN_MINUTES)
            if datetime.now() < cooldown_until:
                in_cooldown = True

    STATE.bot_block_reason = None
    buy_cross, sell_cross = detect_crossover(candle, prev_candle)
    candle.is_crossover = buy_cross or sell_cross

    # ─── AUTO-SYNC TREND DIRECTION ───────────────────────────────────────────
    # Keep last_trend_side in sync with actual EMA alignment
    if candle.fast_ema is not None and candle.slow_ema is not None:
        if candle.fast_ema > candle.slow_ema and STATE.last_trend_side != "BUY":
            STATE.last_trend_side = "BUY"
            print(f"🔄 TREND SYNC: BUY (EMA9 > EMA21)")
        elif candle.fast_ema < candle.slow_ema and STATE.last_trend_side != "SELL":
            STATE.last_trend_side = "SELL"
            print(f"🔄 TREND SYNC: SELL (EMA9 < EMA21)")

    # ─── PHASE-BASED ENTRY GATING ────────────────────────────────────────────
    # CONTRACTION → PREBUY, CROSSOVER only (max 1-2 entries)
    # EXPANSION → REENTRY, HIGHER_LOW, TREND only
    # PEAK → NO entry
    # SIDEWAYS → NO entry (dead zone — wait for direction)
    current_phase = STATE.ema_cycle_phase

    # ─── 1. PRE-BUY / PRE-SELL (bypass cooldown) ─────────────────────────────
    # Only allowed in CONTRACTING phase (NOT sideways — sideways is dead zone)
    if current_phase == "CONTRACTING":
        pre_buy = detect_prebuy(candle, prev_candle)
        pre_sell = detect_presell(candle, prev_candle)

        if pre_buy:
            reset_for_new_trend("BUY")
            if enter_trade("BUY", candle, entry_type="PREBUY"):
                print("⚡ PRE-BUY ENTRY: taken before bullish crossover")
            return

        if pre_sell:
            reset_for_new_trend("SELL")
            if enter_trade("SELL", candle, entry_type="PRESELL"):
                print("⚡ PRE-SELL ENTRY: taken before bearish crossover")
            return
    elif current_phase == "PEAK":
        # PEAK phase — NO new entries allowed
        print(f"🚫 PEAK PHASE — no entries | gap={abs(candle.fast_ema - candle.slow_ema):.2f}")
        _log_rejection(candle, "ALL", STATE.last_trend_side or "BUY", "PEAK phase - no entries")
        write_market_data()
        return
    elif current_phase == "SIDEWAYS":
        # SIDEWAYS — only crossover allowed (signals direction change)
        # All other entries blocked
        pass  # fall through to crossover detection below

    # ─── CONTRACTING/SIDEWAYS BLOCK for remaining entry types ───────────────
    # After crossover check, block TREND/HIGHER_LOW/REENTRY in these phases
    _contracting_or_sideways_block = current_phase in ("CONTRACTING", "SIDEWAYS")

    # ─── COOLDOWN BLOCK (after prebuy/presell check) ─────────────────────────
    if in_cooldown:
        # Still detect crossovers during cooldown so we don't miss the trend
        if buy_cross or sell_cross:
            STATE.crossover_missed = True
            side = "BUY" if buy_cross else "SELL"
            reset_for_new_trend(side)
            print(f"⚠️ CROSSOVER {side} during cooldown — marked as missed")
        last_reason = getattr(STATE, "last_exit_reason", "")
        print(f"⏳ COOLDOWN until {cooldown_until.strftime('%H:%M:%S')} | reason={last_reason}")
        write_market_data()
        return

    # ─── 2. CROSSOVER ─────────────────────────────────────────────────────────
    if buy_cross:
        print("🔎 BUY CROSS DETECTED")
        reset_for_new_trend("BUY")
        if valid_safe_crossover(candle):
            print("✅ VALID_SAFE_CROSSOVER BUY — entering trade")
            enter_trade("BUY", candle, entry_type="CROSSOVER")
            return
        else:
            print("🚫 BUY CROSS BLOCKED: failed valid_safe_crossover (weak/sideways)")
            _log_rejection(candle, "CROSSOVER", "BUY", "failed valid_safe_crossover")
            STATE.crossover_missed = True

    if sell_cross:
        print("🔎 SELL CROSS DETECTED")
        reset_for_new_trend("SELL")
        if valid_safe_crossover(candle):
            print("✅ VALID_SAFE_CROSSOVER SELL — entering trade")
            enter_trade("SELL", candle, entry_type="CROSSOVER")
            return
        else:
            print("🚫 SELL CROSS BLOCKED: failed valid_safe_crossover (weak/sideways)")
            _log_rejection(candle, "CROSSOVER", "SELL", "failed valid_safe_crossover")
            STATE.crossover_missed = True

    # ─── GAP-FROM-PEAK CHECK (catches momentum loss before phase label changes) ─
    # Price peaks BEFORE or AT the same time as EMA gap peak.
    # Two signals: 1) EMA gap dropping from peak  2) Price making lower high with wide EMAs
    _curr_ema_gap = abs(candle.fast_ema - candle.slow_ema) if candle.fast_ema and candle.slow_ema else 0.0
    _peak_drop_pct = (STATE.ema_cycle_peak_gap - _curr_ema_gap) / STATE.ema_cycle_peak_gap if STATE.ema_cycle_peak_gap > 0 else 0.0

    # Signal 1: EMA gap dropped 5%+ from a meaningful peak
    # Peak must be significant (>= 0.25 * ATR) — a peak of 1.43 on ATR 15 is noise
    _peak_was_real = (
        candle.atr is not None and candle.atr > 0
        and STATE.ema_cycle_peak_gap >= candle.atr * 0.25
    )
    _ema_gap_falling = (
        _peak_was_real
        and _curr_ema_gap < STATE.ema_gap_prev
        and _peak_drop_pct >= 0.05
    )

    # Signal 2: Price peak — candle made lower high than prev candle while EMAs are wide
    # This catches the turn before EMAs even start contracting
    _price_peaked = (
        candle.atr is not None and candle.atr > 0
        and _curr_ema_gap >= candle.atr * 0.30        # EMAs must be well separated
        and candle.high < prev_candle.high             # lower high
        and candle.close < prev_candle.close           # lower close
        and candle.close < candle.open                 # bearish candle (for BUY trend)
        and STATE.last_trend_side == "BUY"
    ) or (
        candle.atr is not None and candle.atr > 0
        and _curr_ema_gap >= candle.atr * 0.30
        and candle.low > prev_candle.low               # higher low
        and candle.close > prev_candle.close           # higher close
        and candle.close > candle.open                 # bullish candle (for SELL trend)
        and STATE.last_trend_side == "SELL"
    )

    _gap_falling = _ema_gap_falling or _price_peaked

    # ─── PRICE RECLAIM / RE-EXPANSION OVERRIDE ───────────────────────────────
    # Override gap_falling block when strong momentum is confirmed:
    # 1) Price reclaimed both EMAs from the other side
    # 2) Trend re-expanded (CONTRACTING → EXPANDING without crossover)
    _reclaim_active = (
        (STATE.last_trend_side == "BUY" and STATE.price_reclaim_buy)
        or (STATE.last_trend_side == "SELL" and STATE.price_reclaim_sell)
    )
    _reexpansion_active = STATE.trend_reexpansion

    if _gap_falling and (_reclaim_active or _reexpansion_active):
        reason = "price reclaimed EMAs" if _reclaim_active else "trend re-expanded"
        print(f"💪 OVERRIDE | gap_falling blocked but {reason} | allowing entries")
        _gap_falling = False

    # ─── 2.5 TREND RE-EXPANSION ENTRY ────────────────────────────────────────
    # CONTRACTING → EXPANDING = trend survived the squeeze. Take the entry.
    # Only allowed if NOT in contracting phase anymore
    if _reexpansion_active and not _gap_falling and not _contracting_or_sideways_block:
        if STATE.last_trend_side == "BUY" and valid_buy_continuation(candle):
            STATE.trend_reexpansion = False  # consumed
            print("🔄 TREND RE-EXPANSION BUY — entering trade")
            enter_trade("BUY", candle, entry_type="REEXPANSION")
            return
        if STATE.last_trend_side == "SELL" and valid_sell_continuation(candle):
            STATE.trend_reexpansion = False  # consumed
            print("🔄 TREND RE-EXPANSION SELL — entering trade")
            enter_trade("SELL", candle, entry_type="REEXPANSION")
            return

    # ─── CONTRACTING/SIDEWAYS BLOCK for remaining entry types ────────────────
    if _contracting_or_sideways_block:
        print(f"🚫 {current_phase} — only prebuy/crossover allowed | gap={_curr_ema_gap:.2f}")
        _log_rejection(candle, "ALL", STATE.last_trend_side or "BUY", f"{current_phase} - only prebuy/crossover")
        write_market_data()
        return

    # ─── 3. TREND CONTINUATION ────────────────────────────────────────────────
    # Only fires after a confirmed trend direction (last_trend_side set by crossover/prebuy)
    # Block during CONTRACTING or when gap/price is falling from peak
    if STATE.ema_cycle_phase in ("CONTRACTING",) or _gap_falling:
        if _price_peaked:
            if STATE.last_trend_side == "BUY":
                _reason = f"price peaked | high={candle.high:.2f} < prev_high={prev_candle.high:.2f}"
                print(f"🚫 TREND CONTINUATION BLOCKED | {_reason} | gap={_curr_ema_gap:.2f}")
                _log_rejection(candle, "TREND", "BUY", _reason)
            else:
                _reason = f"price bounced | low={candle.low:.2f} > prev_low={prev_candle.low:.2f}"
                print(f"🚫 TREND CONTINUATION BLOCKED | {_reason} | gap={_curr_ema_gap:.2f}")
                _log_rejection(candle, "TREND", "SELL", _reason)
        elif _ema_gap_falling:
            _reason = f"gap falling from peak | curr={_curr_ema_gap:.2f} peak={STATE.ema_cycle_peak_gap:.2f} drop={_peak_drop_pct:.0%}"
            print(f"🚫 TREND CONTINUATION BLOCKED | {_reason}")
            _log_rejection(candle, "TREND", STATE.last_trend_side or "BUY", _reason)
        else:
            _reason = f"cycle={STATE.ema_cycle_phase}"
            print(f"🚫 TREND CONTINUATION BLOCKED | {_reason}")
            _log_rejection(candle, "TREND", STATE.last_trend_side or "BUY", _reason)
    elif STATE.last_trend_side == "BUY" and valid_buy_continuation(candle):
        if _is_overextended(candle):
            print(f"🚫 TREND BUY BLOCKED | OVEREXTENDED | dist={abs(candle.close - candle.fast_ema):.2f}")
            _log_rejection(candle, "TREND", "BUY", "OVEREXTENDED")
        else:
            print("🚀 TREND CONTINUATION BUY")
            enter_trade("BUY", candle, entry_type="TREND")
            return
    elif STATE.last_trend_side == "SELL" and valid_sell_continuation(candle):
        if _is_overextended(candle):
            print(f"🚫 TREND SELL BLOCKED | OVEREXTENDED | dist={abs(candle.close - candle.fast_ema):.2f}")
            _log_rejection(candle, "TREND", "SELL", "OVEREXTENDED")
        else:
            print("🚀 TREND CONTINUATION SELL")
            enter_trade("SELL", candle, entry_type="TREND")
            return

    # ─── 4. HIGHER_LOW ENTRY (price respects EMA9) ───────────────────────────
    # Block during CONTRACTING or when gap is falling from peak — pullback in dying trend is a trap
    print("🔎 Checking HIGHER_LOW ENTRY")
    if STATE.ema_cycle_phase in ("CONTRACTING",) or _gap_falling:
        if _price_peaked:
            if STATE.last_trend_side == "BUY":
                _reason = f"price peaked | high={candle.high:.2f} < prev_high={prev_candle.high:.2f}"
                print(f"🚫 HIGHER_LOW BLOCKED | {_reason} | gap={_curr_ema_gap:.2f}")
                _log_rejection(candle, "HIGHER_LOW", "BUY", _reason)
            else:
                _reason = f"price bounced | low={candle.low:.2f} > prev_low={prev_candle.low:.2f}"
                print(f"🚫 HIGHER_LOW BLOCKED | {_reason} | gap={_curr_ema_gap:.2f}")
                _log_rejection(candle, "HIGHER_LOW", "SELL", _reason)
        elif _ema_gap_falling:
            _reason = f"gap falling from peak | curr={_curr_ema_gap:.2f} peak={STATE.ema_cycle_peak_gap:.2f} drop={_peak_drop_pct:.0%}"
            print(f"🚫 HIGHER_LOW BLOCKED | {_reason}")
            _log_rejection(candle, "HIGHER_LOW", STATE.last_trend_side or "BUY", _reason)
        else:
            _reason = f"cycle={STATE.ema_cycle_phase}"
            print(f"🚫 HIGHER_LOW BLOCKED | {_reason}")
            _log_rejection(candle, "HIGHER_LOW", STATE.last_trend_side or "BUY", _reason)
    elif STATE.last_trend_side == "BUY" and valid_retrace_buy(candle):
        print("✅ SIGNAL: HIGHER_LOW_BUY (CE)")
        if enter_trade("BUY", candle, entry_type="HIGHER_LOW_BUY"):
            STATE.crossover_missed = False
        return
    elif STATE.last_trend_side == "SELL" and valid_retrace_sell(candle):
        print("✅ SIGNAL: LOWER_HIGH_SELL (PE)")
        if enter_trade("SELL", candle, entry_type="LOWER_HIGH_SELL"):
            STATE.crossover_missed = False
        return

    # ─── 5. RE-ENTRY (after SL hit, trend still valid) ────────────────────────
    # Block during PEAK/CONTRACTING when:
    # - Gap is weak (below threshold), OR
    # - Gap is shrinking (trend fading even if gap is still large)
    _reentry_gap_weak = (
        candle.atr is not None and candle.atr > 0
        and _curr_ema_gap < candle.atr * REENTRY_MIN_EMA_GAP_ATR
    )
    _reentry_gap_shrinking = _curr_ema_gap < STATE.ema_gap_prev  # gap narrowing this candle

    _reentry_cycle_block = False
    _reentry_block_reason = ""
    if STATE.ema_cycle_phase in ("PEAK", "CONTRACTING"):
        if _reentry_gap_weak:
            _reentry_cycle_block = True
            _reentry_block_reason = (
                f"weak gap | gap={_curr_ema_gap:.2f} < "
                f"ATR*{REENTRY_MIN_EMA_GAP_ATR}={candle.atr * REENTRY_MIN_EMA_GAP_ATR:.2f}"
            )
        elif STATE.ema_cycle_phase == "CONTRACTING" and _reentry_gap_shrinking:
            _reentry_cycle_block = True
            _reentry_block_reason = (
                f"gap shrinking in CONTRACTING | gap={_curr_ema_gap:.2f} < "
                f"prev={STATE.ema_gap_prev:.2f}"
            )

    if _reentry_cycle_block:
        if STATE.reentry_allowed:
            print(
                f"🚫 RE-ENTRY BLOCKED | cycle={STATE.ema_cycle_phase} + {_reentry_block_reason}"
            )
            _log_rejection(candle, "REENTRY", STATE.last_trend_side or "BUY", f"cycle={STATE.ema_cycle_phase} + {_reentry_block_reason}")
    elif STATE.reentry_allowed and STATE.reentry_allowed_until:
        if datetime.now() <= STATE.reentry_allowed_until:
            current_reentries = getattr(STATE, "reentry_count", 0)
            if current_reentries >= MAX_REENTRIES:
                print(
                    f"🚫 RE-ENTRY BLOCKED: max re-entries reached | "
                    f"current={current_reentries} | max={MAX_REENTRIES}"
                )
                _log_rejection(candle, "REENTRY", STATE.last_trend_side or "BUY", f"max re-entries reached | current={current_reentries}")
                STATE.reentry_allowed = False
                STATE.reentry_allowed_until = None
                STATE.reentry_reference_price = None
                return

            last_exit_time = getattr(STATE, "last_exit_time", None)
            if last_exit_time is not None:
                reentry_cooldown_minutes = max(1, COOLDOWN_MINUTES - 2)
                cooldown_until = last_exit_time + timedelta(minutes=reentry_cooldown_minutes)
                if datetime.now() < cooldown_until:
                    print(
                        f"⏳ RE-ENTRY COOLDOWN ACTIVE until {cooldown_until.strftime('%H:%M:%S')} "
                        f"| reentry_cooldown={reentry_cooldown_minutes}m"
                    )
                    return

            if STATE.last_trend_side == "BUY":
                ok, reason = _reentry_quality_ok("BUY", candle, prev_candle)
                if not ok:
                    print(f"🚫 RE-ENTRY BUY BLOCKED: {reason}")
                    _log_rejection(candle, "REENTRY", "BUY", reason)
                    return
                if valid_buy_reentry(candle):
                    if enter_trade("BUY", candle, entry_type="REENTRY"):
                        STATE.reentry_count = current_reentries + 1
                        STATE.reentry_allowed = False
                        STATE.reentry_allowed_until = None
                        STATE.reentry_reference_price = None
                        print(f"🔁 RE-ENTRY BUY SUCCESS | count={STATE.reentry_count}")
                    return

            if STATE.last_trend_side == "SELL":
                ok, reason = _reentry_quality_ok("SELL", candle, prev_candle)
                if not ok:
                    print(f"🚫 RE-ENTRY SELL BLOCKED: {reason}")
                    _log_rejection(candle, "REENTRY", "SELL", reason)
                    return
                if valid_sell_reentry(candle):
                    if enter_trade("SELL", candle, entry_type="REENTRY"):
                        STATE.reentry_count = current_reentries + 1
                        STATE.reentry_allowed = False
                        STATE.reentry_allowed_until = None
                        STATE.reentry_reference_price = None
                        print(f"🔁 RE-ENTRY SELL SUCCESS | count={STATE.reentry_count}")
                    return
        else:
            STATE.reentry_allowed = False
            STATE.reentry_allowed_until = None
            STATE.reentry_reference_price = None

    print("🚫 BLOCKED: no valid signal on this candle")
    print(f"DEBUG crossover | prev_fast={prev_fast} prev_slow={prev_slow} fast={fast} slow={slow}")
    print(f"DEBUG filters | close={candle.close} vwap={candle.vwap} atr={candle.atr}")
    write_market_data()

def detect_prebuy(candle: CandleSnapshot, prev_candle: CandleSnapshot) -> bool:
    if not USE_PREBUY_ENTRY:
        return False

    if None in (
        candle.fast_ema, candle.slow_ema,
        prev_candle.fast_ema, prev_candle.slow_ema, candle.atr,
    ):
        return False

    ema_gap = abs(candle.fast_ema - candle.slow_ema)
    prev_gap = abs(prev_candle.fast_ema - prev_candle.slow_ema)
    gap_shrink = prev_gap - ema_gap

    # ── REFINED COUNTER-TREND BLOCK ──────────────────────────────────────
    # Allow prebuy even in SELL trend IF:
    # 1. EMAs are converging (gap shrinking)
    # 2. Price-EMA9 distance is shrinking (price returning to EMA9)
    # 3. Gap is small enough (< 0.50 * ATR) — genuine convergence zone
    if STATE.last_trend_side == "SELL":
        gap_converging = gap_shrink > 0 and ema_gap < candle.atr * 0.50
        price_returning = (
            getattr(STATE, "price_ema9_momentum", None) == "SHRINKING"
            or abs(candle.close - candle.fast_ema) < candle.atr * 0.30
        )
        if not (gap_converging and price_returning):
            if ema_gap > candle.atr * 0.50:
                print(
                    f"🚫 PREBUY BLOCKED | counter-trend (trend=SELL) | "
                    f"gap={ema_gap:.2f} > ATR*0.50={candle.atr * 0.50:.2f}"
                )
                _log_rejection(candle, "PREBUY", "BUY", f"counter-trend (trend=SELL) | gap={ema_gap:.2f}")
                return False

    # ── EMA POSITION CHECK ───────────────────────────────────────────────
    # EMA9 must be below EMA21 (not yet crossed) — this IS a pre-buy
    if not (candle.fast_ema < candle.slow_ema):
        # Already crossed — not a prebuy anymore
        return False

    # ── CONVERGENCE SIGNALS ──────────────────────────────────────────────
    curr_gap = ema_gap

    # Price between EMA9 and EMA21 = convergence zone
    price_between_emas = (
        min(candle.fast_ema, candle.slow_ema) <= candle.close <= max(candle.fast_ema, candle.slow_ema)
    )

    # Price near EMA9 (within 0.30 ATR)
    price_to_ema9 = abs(candle.close - candle.fast_ema)
    near_ema9 = price_to_ema9 <= candle.atr * 0.30

    # Price above both EMAs (reclaim signal)
    price_above_both = candle.close > max(candle.fast_ema, candle.slow_ema)

    # Classic prebuy: gap small + shrinking
    classic_prebuy = curr_gap <= PREBUY_MAX_EMA_GAP and gap_shrink >= PREBUY_MIN_GAP_SHRINK
    # Zone prebuy: price in convergence zone or above both EMAs
    zone_prebuy = price_between_emas or near_ema9 or price_above_both

    # ── REFINED ENTRY CONDITIONS ─────────────────────────────────────────
    # REMOVED: bullish candle requirement (candle.close >= candle.open)
    # In contraction, swings are small — requiring bullish candle misses entries
    # Instead: EMA9 must be rising (direction confirmed) + price near/above EMAs

    ema9_rising = candle.fast_ema > prev_candle.fast_ema

    print(
        f"🧪 PREBUY CHECK | "
        f"prev_gap={prev_gap:.2f} | curr_gap={curr_gap:.2f} | "
        f"gap_shrink={gap_shrink:.2f} | between_emas={price_between_emas} | "
        f"near_ema9={near_ema9} | above_both={price_above_both} | "
        f"ema9_rising={ema9_rising}"
    )

    return (
        (classic_prebuy or zone_prebuy)
        and ema9_rising                              # EMA9 must be rising (direction)
        and not _is_overextended(candle)             # don't prebuy at stretched levels
    )

def detect_presell(candle: CandleSnapshot, prev_candle: CandleSnapshot) -> bool:
    if not USE_PREBUY_ENTRY:
        return False

    if None in (
        candle.fast_ema, candle.slow_ema,
        prev_candle.fast_ema, prev_candle.slow_ema, candle.atr,
    ):
        return False

    ema_gap = abs(candle.fast_ema - candle.slow_ema)
    prev_gap = abs(prev_candle.fast_ema - prev_candle.slow_ema)
    gap_shrink = prev_gap - ema_gap

    # ── REFINED COUNTER-TREND BLOCK ──────────────────────────────────────
    if STATE.last_trend_side == "BUY":
        gap_converging = gap_shrink > 0 and ema_gap < candle.atr * 0.50
        price_returning = (
            getattr(STATE, "price_ema9_momentum", None) == "SHRINKING"
            or abs(candle.close - candle.fast_ema) < candle.atr * 0.30
        )
        if not (gap_converging and price_returning):
            if ema_gap > candle.atr * 0.50:
                print(
                    f"🚫 PRESELL BLOCKED | counter-trend (trend=BUY) | "
                    f"gap={ema_gap:.2f} > ATR*0.50={candle.atr * 0.50:.2f}"
                )
                _log_rejection(candle, "PRESELL", "SELL", f"counter-trend (trend=BUY) | gap={ema_gap:.2f}")
                return False

    # ── EMA POSITION CHECK ───────────────────────────────────────────────
    # EMA9 must be above EMA21 (not yet crossed bearish) — this IS a pre-sell
    if not (candle.fast_ema > candle.slow_ema):
        return False

    # ── CONVERGENCE SIGNALS ──────────────────────────────────────────────
    curr_gap = ema_gap

    price_between_emas = (
        min(candle.fast_ema, candle.slow_ema) <= candle.close <= max(candle.fast_ema, candle.slow_ema)
    )

    price_to_ema9 = abs(candle.close - candle.fast_ema)
    near_ema9 = price_to_ema9 <= candle.atr * 0.30

    # Price below both EMAs (breakdown signal)
    price_below_both = candle.close < min(candle.fast_ema, candle.slow_ema)

    classic_presell = curr_gap <= PREBUY_MAX_EMA_GAP and gap_shrink >= PREBUY_MIN_GAP_SHRINK
    zone_presell = price_between_emas or near_ema9 or price_below_both

    # EMA9 must be falling (direction confirmed)
    ema9_falling = candle.fast_ema < prev_candle.fast_ema

    print(
        f"🧪 PRESELL CHECK | "
        f"prev_gap={prev_gap:.2f} | curr_gap={curr_gap:.2f} | "
        f"gap_shrink={gap_shrink:.2f} | between_emas={price_between_emas} | "
        f"near_ema9={near_ema9} | below_both={price_below_both} | "
        f"ema9_falling={ema9_falling}"
    )

    return (
        (classic_presell or zone_presell)
        and ema9_falling                             # EMA9 must be falling (direction)
        and not _is_overextended(candle)             # don't presell at stretched levels
    )


def detect_crossover(candle: CandleSnapshot, prev_candle: CandleSnapshot):
    buy_cross = prev_candle.fast_ema <= prev_candle.slow_ema and candle.fast_ema > candle.slow_ema
    sell_cross = prev_candle.fast_ema >= prev_candle.slow_ema and candle.fast_ema < candle.slow_ema
    return buy_cross, sell_cross


def reset_for_new_trend(side: str):
    STATE.last_trend_side = side
    STATE.reentry_allowed = False
    STATE.reentry_allowed_until = None
    STATE.crossover_missed = False
    STATE.first_retrace_done = False
    STATE.reentry_count = 0
    # Reset EMA cycle for new trend
    STATE.ema_cycle_phase = "EXPANDING"
    STATE.ema_cycle_peak_gap = 0.0
    STATE.ema_gap_expanding_count = 0
    STATE.ema_gap_contracting_count = 0
    # Reset price reclaim
    STATE.price_reclaim_buy = False
    STATE.price_reclaim_sell = False
    STATE.price_was_below_emas = False
    STATE.price_was_above_emas = False
    # Reset re-expansion
    STATE.trend_reexpansion = False

def _sideways_filter(candle: CandleSnapshot) -> bool:
    if candle.atr is None or candle.atr <= 0:
        return True

    if candle.fast_ema is None or candle.slow_ema is None:
        return True

    ema_gap = abs(candle.fast_ema - candle.slow_ema)
    candle_range = max(0.0, candle.high - candle.low)
    body = abs(candle.close - candle.open)
    body_ratio = (body / candle_range) if candle_range > 0 else 0.0

    if ema_gap < candle.atr * MIN_EMA_GAP_ATR:
        return True

    if candle_range < candle.atr * MIN_CANDLE_RANGE_ATR:
        return True

    if body_ratio < MIN_BODY_RATIO:
        return True

    return False


# ─── EMA CYCLE PHASE TRACKING ────────────────────────────────────────────────
# Lifecycle: SIDEWAYS → EXPANDING → PEAK → CONTRACTING → SIDEWAYS/EXPANDING
#
# SIDEWAYS:     EMAs tangled, gap < threshold. No trend.
# EXPANDING:    Gap widening candle-over-candle. Momentum building. Best entries.
# PEAK:         Gap was expanding, now flattened or just started narrowing.
#               Trend mature, momentum fading. Activate tighter exits.
# CONTRACTING:  Gap narrowing toward next crossover. Avoid new entries.

EMA_CYCLE_SIDEWAYS_ATR = 0.30       # below this = sideways (matches MIN_EMA_GAP_ATR)
EMA_CYCLE_PEAK_CANDLES = 2          # gap must narrow for N candles to confirm peak→contracting
EMA_CYCLE_EXPAND_CANDLES = 2        # gap must widen for N candles to confirm expanding
EMA_CYCLE_PEAK_CONFIRM = 2          # gap must narrow for N candles to confirm EXPANDING→PEAK


def _update_ema_cycle(candle: CandleSnapshot, prev_candle: CandleSnapshot = None):
    """Update EMA cycle phase based on current gap vs previous gap."""
    if candle.atr is None or candle.atr <= 0:
        return
    if candle.fast_ema is None or candle.slow_ema is None:
        return

    curr_gap = abs(candle.fast_ema - candle.slow_ema)
    prev_gap = STATE.ema_gap_prev

    # ── First candle after restart: use prev_candle to determine direction ──
    if prev_gap == 0.0 and curr_gap > 0:
        if (prev_candle is not None
                and prev_candle.fast_ema is not None
                and prev_candle.slow_ema is not None):
            prev_candle_gap = abs(prev_candle.fast_ema - prev_candle.slow_ema)
            STATE.ema_gap_prev = prev_candle_gap
            STATE.ema_cycle_peak_gap = max(curr_gap, prev_candle_gap)

            if curr_gap < candle.atr * EMA_CYCLE_SIDEWAYS_ATR:
                STATE.ema_cycle_phase = "SIDEWAYS"
            elif curr_gap > prev_candle_gap:
                STATE.ema_cycle_phase = "EXPANDING"
                STATE.ema_gap_expanding_count = 1
            elif curr_gap < prev_candle_gap:
                STATE.ema_cycle_phase = "CONTRACTING"
                STATE.ema_gap_contracting_count = 1
            else:
                STATE.ema_cycle_phase = "EXPANDING"

            print(
                f"📊 EMA CYCLE: INIT → {STATE.ema_cycle_phase} | "
                f"prev_gap={prev_candle_gap:.2f} curr_gap={curr_gap:.2f} | ATR={candle.atr:.2f}"
            )
        else:
            STATE.ema_gap_prev = curr_gap
            STATE.ema_cycle_peak_gap = curr_gap
            print(
                f"📊 EMA CYCLE: INIT (waiting) | gap={curr_gap:.2f} | ATR={candle.atr:.2f}"
            )
        return

    gap_change = curr_gap - prev_gap
    old_phase = STATE.ema_cycle_phase

    # ── SIDEWAYS detection ────────────────────────────────────────────────
    if curr_gap < candle.atr * EMA_CYCLE_SIDEWAYS_ATR:
        STATE.ema_cycle_phase = "SIDEWAYS"
        STATE.ema_gap_expanding_count = 0
        STATE.ema_gap_contracting_count = 0
    else:
        # ── Gap direction tracking ────────────────────────────────────────
        if gap_change > 0:
            STATE.ema_gap_expanding_count += 1
            STATE.ema_gap_contracting_count = 0
        elif gap_change < 0:
            STATE.ema_gap_contracting_count += 1
            STATE.ema_gap_expanding_count = 0

        # ── Phase transitions ─────────────────────────────────────────────
        if STATE.ema_gap_expanding_count >= EMA_CYCLE_EXPAND_CANDLES:
            # Detect CONTRACTING/PEAK → EXPANDING (trend survived the squeeze)
            if old_phase in ("CONTRACTING", "PEAK"):
                STATE.trend_reexpansion = True
                print(
                    f"🔄 TREND RE-EXPANSION | {old_phase} → EXPANDING | "
                    f"trend survived squeeze | side={STATE.last_trend_side}"
                )
            STATE.ema_cycle_phase = "EXPANDING"
        elif old_phase == "EXPANDING" and STATE.ema_gap_contracting_count >= EMA_CYCLE_PEAK_CONFIRM:
            STATE.ema_cycle_phase = "PEAK"
        elif old_phase == "PEAK" and STATE.ema_gap_contracting_count >= EMA_CYCLE_PEAK_CANDLES:
            STATE.ema_cycle_phase = "CONTRACTING"
        elif old_phase not in ("EXPANDING", "PEAK") and STATE.ema_gap_contracting_count >= EMA_CYCLE_PEAK_CANDLES:
            if old_phase == "SIDEWAYS":
                STATE.ema_cycle_phase = "SIDEWAYS"
            else:
                STATE.ema_cycle_phase = "CONTRACTING"

    # ── Track peak gap for this cycle ─────────────────────────────────────
    if curr_gap > STATE.ema_cycle_peak_gap:
        STATE.ema_cycle_peak_gap = curr_gap

    STATE.ema_gap_prev = curr_gap

    if STATE.ema_cycle_phase != old_phase:
        print(
            f"📊 EMA CYCLE: {old_phase} → {STATE.ema_cycle_phase} | "
            f"gap={curr_gap:.2f} | peak={STATE.ema_cycle_peak_gap:.2f} | "
            f"ATR={candle.atr:.2f}"
        )
    else:
        print(
            f"📊 EMA CYCLE: {STATE.ema_cycle_phase} | "
            f"gap={curr_gap:.2f} | peak={STATE.ema_cycle_peak_gap:.2f}"
        )


def _update_price_reclaim(candle: CandleSnapshot):
    """Track when price crosses back above/below both EMAs from the other side.
    
    Pattern: price was below both EMAs → reclaims above both = strong bullish momentum
             price was above both EMAs → reclaims below both = strong bearish momentum
    Only fires when EMAs have meaningful separation — not during sideways/contracting chop.
    """
    if candle.fast_ema is None or candle.slow_ema is None:
        return

    ema_upper = max(candle.fast_ema, candle.slow_ema)
    ema_lower = min(candle.fast_ema, candle.slow_ema)
    ema_gap = ema_upper - ema_lower

    price_above_both = candle.close > ema_upper
    price_below_both = candle.close < ema_lower

    # Track if price was on the other side
    if price_below_both:
        STATE.price_was_below_emas = True
        STATE.price_reclaim_buy = False       # reset — price is below again
    if price_above_both:
        STATE.price_was_above_emas = True
        STATE.price_reclaim_sell = False       # reset — price is above again

    # Minimum gap required — reclaim signals STRONG momentum, not sideways noise
    # Require gap >= 0.30 * ATR — higher than sideways filter because reclaim
    # should only fire when EMAs are clearly separated
    min_gap = candle.atr * 0.30 if candle.atr and candle.atr > 0 else 0
    if ema_gap < min_gap:
        return

    # Detect reclaim: price was below both → now above both
    if price_above_both and STATE.price_was_below_emas:
        if not STATE.price_reclaim_buy:
            STATE.price_reclaim_buy = True
            STATE.price_was_below_emas = False  # consumed
            print(
                f"💪 PRICE RECLAIM BUY | close={candle.close:.2f} > "
                f"EMA9={candle.fast_ema:.2f} EMA21={candle.slow_ema:.2f} | "
                f"gap={ema_gap:.2f} | strong bullish momentum"
            )

    # Detect reclaim: price was above both → now below both
    if price_below_both and STATE.price_was_above_emas:
        if not STATE.price_reclaim_sell:
            STATE.price_reclaim_sell = True
            STATE.price_was_above_emas = False  # consumed
            print(
                f"💪 PRICE RECLAIM SELL | close={candle.close:.2f} < "
                f"EMA9={candle.fast_ema:.2f} EMA21={candle.slow_ema:.2f} | "
                f"gap={ema_gap:.2f} | strong bearish momentum"
            )


# ── Leading Phase Thresholds ─────────────────────────────────────────────
LEADING_SIDEWAYS_ATR = 0.15   # abs(price-EMA9) < this * ATR → SIDEWAYS
LEADING_PEAK_CONFIRM = 2      # candles of compression after expansion to confirm peak


def _update_leading_phase(dist_ema9: float, curr_abs_dist: float, atr: float):
    """Update the leading phase state machine (price − EMA9).

    Phases:
      SIDEWAYS        – price hugging EMA9 (abs distance < threshold)
      EXPANDING_UP    – price pulling away above EMA9 (gap growing)
      PEAKED_UP       – gap stopped growing from above, starting to compress
      COMPRESSING_DOWN– gap shrinking toward zero from above
      CROSSED_DOWN    – price just crossed below EMA9
      EXPANDING_DOWN  – price pulling away below EMA9 (gap growing negative)
      PEAKED_DOWN     – negative gap stopped growing, starting to compress
      COMPRESSING_UP  – gap shrinking toward zero from below
      CROSSED_UP      – price just crossed above EMA9
    """
    old_phase = STATE.leading_phase
    prev_abs = STATE.leading_prev_abs_dist
    prev_signed = STATE.prev_price_dist_ema9

    # Detect zero-cross events
    crossed_up = prev_signed < 0 and dist_ema9 >= 0
    crossed_down = prev_signed >= 0 and dist_ema9 < 0

    # Near-zero → SIDEWAYS
    if curr_abs_dist < atr * LEADING_SIDEWAYS_ATR:
        new_phase = "SIDEWAYS"
    elif crossed_up:
        new_phase = "CROSSED_UP"
    elif crossed_down:
        new_phase = "CROSSED_DOWN"
    elif dist_ema9 > 0:
        # Price is above EMA9
        if curr_abs_dist > prev_abs + 0.01:
            # Gap still growing
            if old_phase in ("PEAKED_UP", "COMPRESSING_DOWN"):
                # Was compressing, now re-expanding → back to EXPANDING_UP
                new_phase = "EXPANDING_UP"
            else:
                new_phase = "EXPANDING_UP"
        elif curr_abs_dist < prev_abs - 0.01:
            # Gap shrinking
            if old_phase == "EXPANDING_UP":
                new_phase = "PEAKED_UP"
            elif old_phase == "PEAKED_UP":
                new_phase = "COMPRESSING_DOWN"
            elif old_phase == "COMPRESSING_DOWN":
                new_phase = "COMPRESSING_DOWN"
            else:
                new_phase = "COMPRESSING_DOWN"
        else:
            # Flat — hold current phase
            new_phase = old_phase if old_phase in (
                "EXPANDING_UP", "PEAKED_UP", "COMPRESSING_DOWN", "CROSSED_UP"
            ) else "EXPANDING_UP"
    else:
        # Price is below EMA9
        if curr_abs_dist > prev_abs + 0.01:
            # Gap still growing (more negative)
            if old_phase in ("PEAKED_DOWN", "COMPRESSING_UP"):
                new_phase = "EXPANDING_DOWN"
            else:
                new_phase = "EXPANDING_DOWN"
        elif curr_abs_dist < prev_abs - 0.01:
            # Gap shrinking (less negative)
            if old_phase == "EXPANDING_DOWN":
                new_phase = "PEAKED_DOWN"
            elif old_phase == "PEAKED_DOWN":
                new_phase = "COMPRESSING_UP"
            elif old_phase == "COMPRESSING_UP":
                new_phase = "COMPRESSING_UP"
            else:
                new_phase = "COMPRESSING_UP"
        else:
            # Flat — hold current phase
            new_phase = old_phase if old_phase in (
                "EXPANDING_DOWN", "PEAKED_DOWN", "COMPRESSING_UP", "CROSSED_DOWN"
            ) else "EXPANDING_DOWN"

    # Update state
    if new_phase != old_phase:
        STATE.leading_phase = new_phase
        STATE.leading_phase_duration = 1
        print(
            f"📈 LEADING PHASE: {old_phase} → {new_phase} | "
            f"dist={dist_ema9:+.2f} | peak_dist={STATE.leading_peak_dist:.2f}"
        )
    else:
        STATE.leading_phase_duration += 1

    # Track peak distance in current directional cycle
    if new_phase in ("EXPANDING_UP", "EXPANDING_DOWN"):
        if curr_abs_dist > STATE.leading_peak_dist:
            STATE.leading_peak_dist = curr_abs_dist
    elif new_phase in ("CROSSED_UP", "CROSSED_DOWN", "SIDEWAYS"):
        STATE.leading_peak_dist = 0.0  # reset on cross/sideways

    STATE.leading_prev_abs_dist = curr_abs_dist


def _log_price_distance(candle: CandleSnapshot):
    """Log how far price is from EMA9 and EMA21 in ATR multiples.
    
    Tracks candle-over-candle change in price-to-EMA9 distance:
    - Distance expanding = momentum building (price pulling away from EMA9)
    - Distance shrinking = momentum fading (price returning to EMA9)
    """
    if candle.fast_ema is None or candle.slow_ema is None or candle.atr is None or candle.atr <= 0:
        return

    dist_ema9 = candle.close - candle.fast_ema
    dist_ema21 = candle.close - candle.slow_ema
    dist_ema9_atr = dist_ema9 / candle.atr
    dist_ema21_atr = dist_ema21 / candle.atr

    # Open-to-EMA9 distance (where candle started relative to EMA9)
    open_dist_ema9 = candle.open - candle.fast_ema
    open_dist_ema9_atr = open_dist_ema9 / candle.atr

    # Price-EMA9 momentum direction (absolute distance comparison)
    prev_abs = abs(STATE.prev_price_dist_ema9)
    curr_abs = abs(dist_ema9)
    if curr_abs > prev_abs + 0.01:
        price_momentum = "EXPANDING"    # price pulling away from EMA9
    elif curr_abs < prev_abs - 0.01:
        price_momentum = "SHRINKING"    # price returning to EMA9
    else:
        price_momentum = "FLAT"

    # Classify price position
    if abs(dist_ema9_atr) > 1.5:
        zone = "OVEREXTENDED"
    elif abs(dist_ema9_atr) > 0.8:
        zone = "STRETCHED"
    elif abs(dist_ema9_atr) <= 0.3:
        zone = "NEAR_EMA9"
    else:
        zone = "NORMAL"

    # Store for use by entry/exit logic
    STATE.prev_price_dist_ema9 = STATE.price_dist_ema9
    STATE.price_dist_ema9 = dist_ema9
    STATE.price_dist_ema21 = dist_ema21
    STATE.price_dist_ema9_atr = dist_ema9_atr
    STATE.price_dist_ema21_atr = dist_ema21_atr
    STATE.open_dist_ema9 = open_dist_ema9
    STATE.open_dist_ema9_atr = open_dist_ema9_atr
    STATE.price_zone = zone

    # ── LEADING PHASE DETECTION (price − EMA9) ───────────────────────────
    _update_leading_phase(dist_ema9, curr_abs, candle.atr)

    print(
        f"📏 PRICE DIST | EMA9={dist_ema9:+.2f} ({dist_ema9_atr:+.2f}x ATR) | "
        f"EMA21={dist_ema21:+.2f} ({dist_ema21_atr:+.2f}x ATR) | "
        f"{zone} | price-EMA9 {price_momentum} | leading={STATE.leading_phase}({STATE.leading_phase_duration})"
    )


def valid_buy_continuation(candle: CandleSnapshot):
    ema_gap = abs(candle.fast_ema - candle.slow_ema)
    candle_body = abs(candle.close - candle.open)
    # If crossover was missed, relax thresholds — the trend is confirming after a weak cross
    missed = getattr(STATE, "crossover_missed", False)
    max_dist_mult = 2.00 if missed else 0.80
    min_gap_mult = 0.25 if missed else 0.50
    return (
        candle.fast_ema > candle.slow_ema
        and candle.close > candle.fast_ema
        and ema_gap >= candle.atr * min_gap_mult       # relaxed after missed crossover
        and abs(candle.close - candle.fast_ema) <= candle.atr * max_dist_mult
        and candle.close > candle.open                 # must be bullish candle
        and candle_body >= candle.atr * 0.30           # strong body, not doji
        and not _sideways_filter(candle)
    )


def valid_sell_continuation(candle: CandleSnapshot):
    ema_gap = abs(candle.fast_ema - candle.slow_ema)
    candle_body = abs(candle.close - candle.open)
    # If crossover was missed, relax thresholds — the trend is confirming after a weak cross
    missed = getattr(STATE, "crossover_missed", False)
    max_dist_mult = 2.00 if missed else 0.80
    min_gap_mult = 0.25 if missed else 0.50
    return (
        candle.fast_ema < candle.slow_ema
        and candle.close < candle.fast_ema
        and ema_gap >= candle.atr * min_gap_mult       # relaxed after missed crossover
        and abs(candle.close - candle.fast_ema) <= candle.atr * max_dist_mult
        and candle.close < candle.open                 # must be bearish candle
        and candle_body >= candle.atr * 0.30           # strong body, not doji
        and not _sideways_filter(candle)
    )

def valid_buy_reentry(candle: CandleSnapshot):
    if candle.fast_ema is None or candle.slow_ema is None or candle.atr is None:
        return False
    if candle.fast_ema <= candle.slow_ema:
        return False
    ema_gap = candle.fast_ema - candle.slow_ema
    # Stricter EMA gap for re-entries — need clear trend separation, not sideways chop
    if ema_gap < candle.atr * REENTRY_MIN_EMA_GAP_ATR:
        print(
            f"🚫 RE-ENTRY BUY BLOCKED: weak EMA gap | "
            f"gap={ema_gap:.2f} < ATR*{REENTRY_MIN_EMA_GAP_ATR}={candle.atr * REENTRY_MIN_EMA_GAP_ATR:.2f}"
        )
        return False
    return not _sideways_filter(candle)

def valid_sell_reentry(candle: CandleSnapshot):
    if candle.fast_ema is None or candle.slow_ema is None or candle.atr is None:
        return False
    if candle.fast_ema >= candle.slow_ema:
        return False
    ema_gap = candle.slow_ema - candle.fast_ema
    # Stricter EMA gap for re-entries — need clear trend separation, not sideways chop
    if ema_gap < candle.atr * REENTRY_MIN_EMA_GAP_ATR:
        print(
            f"🚫 RE-ENTRY SELL BLOCKED: weak EMA gap | "
            f"gap={ema_gap:.2f} < ATR*{REENTRY_MIN_EMA_GAP_ATR}={candle.atr * REENTRY_MIN_EMA_GAP_ATR:.2f}"
        )
        return False
    return not _sideways_filter(candle)

def valid_retrace_buy(candle: CandleSnapshot):
    ema_gap = abs(candle.fast_ema - candle.slow_ema)
    return (
        not _sideways_filter(candle)
        and candle.fast_ema > candle.slow_ema
        and ema_gap >= candle.atr * 0.25              # trend must be established
        and candle.low <= candle.fast_ema + (candle.atr * 0.30)  # touched near EMA9
        and candle.low >= candle.fast_ema - (candle.atr * 0.30)  # didn't blow past EMA9
        and candle.close > candle.fast_ema
    )


def valid_retrace_sell(candle: CandleSnapshot):
    ema_gap = abs(candle.fast_ema - candle.slow_ema)
    return (
        not _sideways_filter(candle)
        and candle.fast_ema < candle.slow_ema
        and ema_gap >= candle.atr * 0.25              # trend must be established
        and candle.high >= candle.fast_ema - (candle.atr * 0.30)  # touched near EMA9
        and candle.high <= candle.fast_ema + (candle.atr * 0.30)  # didn't blow past EMA9
        and candle.close < candle.fast_ema
    )


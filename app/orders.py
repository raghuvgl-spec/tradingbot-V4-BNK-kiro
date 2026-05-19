from __future__ import annotations

from datetime import datetime, timedelta

from app.config import (
    ATR_PROFIT_GIVEBACK,
    ATR_PROFIT_LOCK_MULTIPLIER,
    ATR_SL_MULTIPLIER,
    ATR_TARGET_MULTIPLIER,
    ATR_TRAIL_MULTIPLIER,
    COOLDOWN_MINUTES,
    DEBUG_MODE,
    DEBUG_ORDER_EXCHANGE,
    DEBUG_SYMBOL,
    DEBUG_TOKEN,
    LOT_SIZE,
    ORDER_TAG_PREFIX,
    PAPER_TRADING,
    QTY,
    REENTRY_WINDOW_SEC,
    SL_POINTS,
    TARGET_POINTS,
    PROFIT_GIVEBACK_PCT,
)
from app.state import STATE
from app.broker import get_option_ltp, get_option_symbol
# from app.logger_excel import log_trade_entry, log_trade_exit  # replaced by Trade Intelligence
from app.trade_logger import log_trade_entry, log_trade_exit
from app.files import write_market_data, write_state
from app.utils import safe_api_call
from app import pattern_matcher
from app.config import PATTERN_LOG_ONLY


def _next_trade_id():
    STATE.trade_id_counter += 1
    return int(datetime.now().timestamp() * 1000) + STATE.trade_id_counter


def _parse_entry_time(pos):
    """Parse entry_time from position dict."""
    try:
        et = pos.get("entry_time")
        if et is None:
            return None
        if isinstance(et, datetime):
            return et
        return datetime.strptime(str(et), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _resolve_symbol_token(trend_side: str):
    if DEBUG_MODE:
        symbol = DEBUG_SYMBOL
        token = DEBUG_TOKEN
        exchange = DEBUG_ORDER_EXCHANGE
        return symbol, token, exchange, "FUT"

    index_ltp = getattr(STATE, "ltp", None)
    option_type = "CE" if trend_side == "BUY" else "PE"
    symbol, token = get_option_symbol(index_ltp, option_type)
    print(f"ORDER DEBUG | symbol={symbol} | token={token} | exchange=NFO | side={trend_side}")
    return symbol, token, "NFO", option_type

def _place_market_order(symbol: str, token: str, side: str, qty: int, exchange: str):
    if PAPER_TRADING:
        order_id = f"PAPER_{side}_{int(datetime.now().timestamp())}"
        return True, order_id

    params = {
        "variety": "NORMAL",
        "tradingsymbol": symbol,
        "symboltoken": str(token),
        "transactiontype": side,
        "exchange": exchange,
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "quantity": int(qty),
        "tag": ORDER_TAG_PREFIX[:12],
    }
    order_id = safe_api_call(lambda: STATE.obj.placeOrder(params))
    return bool(order_id), order_id


def enter_trade(trend_side, candle, entry_type="CROSSOVER"):
    if STATE.current_position or STATE.pending_trade:
        print("🚫 ENTRY BLOCKED: existing position/pending trade")
        return False

    atr = candle.atr
    if atr is None or atr <= 0:
        print("🚫 ENTRY BLOCKED: ATR unavailable")
        return False

    symbol, token, exchange, instrument = _resolve_symbol_token(trend_side)
    if not symbol or not token:
        print("🚫 ENTRY BLOCKED: symbol/token unavailable")
        return False

    option_ltp = get_option_ltp(symbol, token, exchange=exchange)
    if option_ltp is None or option_ltp <= 0:
        print(f"🚫 ENTRY BLOCKED: option LTP invalid | symbol={symbol}")
        return False
    
    entry_price = float(option_ltp)
    premium_multiplier = entry_price / candle.close if candle.close > 0 else 0

    atr_sl_distance = atr * ATR_SL_MULTIPLIER * premium_multiplier
    atr_target_distance = atr * ATR_TARGET_MULTIPLIER * premium_multiplier

    min_sl_distance = SL_POINTS
    min_target_distance = TARGET_POINTS

    # ── PHASE-ADAPTIVE SL ────────────────────────────────────────────────
    # In strong trend (EXPANDING), give more breathing room
    # Use percentage of premium as minimum SL (2.5% of entry price)
    ema_phase = getattr(STATE, "ema_cycle_phase", "SIDEWAYS")
    if ema_phase == "EXPANDING":
        # Wider SL in expanding — trend needs room to breathe
        pct_sl = entry_price * 0.025  # 2.5% of premium
        min_sl_distance = max(SL_POINTS, pct_sl)
    else:
        min_sl_distance = SL_POINTS

    sl_distance = max(atr_sl_distance, min_sl_distance)
    target_distance = max(atr_target_distance, min_target_distance)

    sl = entry_price - sl_distance
    target = entry_price + target_distance

    print(
        f"🧾 ENTRY DEBUG | entry={entry_price:.2f} | index_close={candle.close:.2f} | "
        f"atr={atr:.2f} | premium_mult={premium_multiplier:.6f} | "
        f"atr_sl_dist={atr_sl_distance:.2f} | final_sl_dist={sl_distance:.2f} | "
        f"target_dist={target_distance:.2f} | phase={ema_phase}"
    )

    sl = entry_price - sl_distance
    target = entry_price + target_distance

    # --- Trade Intelligence: Pattern Matcher evaluation ---
    try:
        match_result = pattern_matcher.evaluate(entry_type, candle, instrument)
        print(
            f"🧠 PATTERN MATCHER | {match_result.recommendation} | "
            f"entry_type={entry_type} instrument={instrument} | "
            f"wins={match_result.win_count} losses={match_result.loss_count} "
            f"total={match_result.total_matches} | {match_result.match_details}"
        )
        if not PATTERN_LOG_ONLY and match_result.recommendation == "SKIP":
            print(f"🚫 ENTRY BLOCKED by Pattern Matcher: SKIP recommendation for {entry_type}")
            return False
    except Exception as e:
        print(f"⚠️ Pattern matcher error (proceeding with trade): {e}")

    ok, order_id = _place_market_order(symbol, token, "BUY", QTY, exchange)
    if not ok:
        print("❌ ENTRY ORDER FAILED")
        return False

    trade_id = _next_trade_id()
    position = {
        "trade_id": trade_id,
        "symbol": symbol,
        "token": str(token),
        "instrument": instrument,
        "exchange": exchange,
        "side": "BUY",
        "trend_side": trend_side,
        "qty": QTY,
        "entry_price": entry_price,
        "entry_index_ltp": getattr(STATE, "ltp", None),
        "signal_price": candle.close,
        "desired_entry": candle.close,
        "sl": sl,
        "target": target,
        "initial_sl": sl,
        "initial_target": target,
        "highest_price": entry_price,
        "lowest_price": entry_price,
        "profit_lock_price": None,
        "entry_order_id": order_id,
        "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "paper_trade": PAPER_TRADING,
        "entry_logged": True,
        "entry_reason": entry_type,
        "status": "OPEN",
    }

    STATE.current_position = position
    STATE.current_trade_id = trade_id
    STATE.trade_count += 1
    STATE.last_action = f"ENTRY_{trend_side}"
    STATE.last_signal = f"{instrument} BUY"

    log_trade_entry(
        trade_id=trade_id,
        symbol=symbol,
        instrument=instrument,
        side="BUY",
        qty=QTY,
        entry_price=entry_price,
        trade_count=STATE.trade_count,
        reason=entry_type,
        atr=atr,
        sl=sl,
        target=target,
        entry_type=entry_type,
    )
    write_market_data("CE BUY" if instrument == "CE" else "PE BUY" if instrument == "PE" else "FUT BUY")
    write_state("TRADE_OPEN")
    try:
        from app.broker import subscribe_open_position_token
        subscribe_open_position_token()
    except Exception as e:
        print(f"⚠️ Could not subscribe option token after entry: {e}")

    # Adjust entry_type label based on instrument for clarity in logs
    if entry_type in ("HIGHER_LOW_BUY", "LOWER_HIGH_SELL"):
        if instrument == "PE":
            display_type = "LOWER_HIGH_BUY"
        elif instrument == "CE":
            display_type = "HIGHER_LOW_BUY"
        elif instrument == "FUT" and trend_side == "SELL":
            display_type = "LOWER_HIGH_SELL"
        else:
            display_type = entry_type
    else:
        display_type = entry_type

    print(f"✅ ENTRY BUY | {symbol} @ {entry_price:.2f} | SL={sl:.2f} TARGET={target:.2f} | {display_type}")
    return True
    


def _exit_position(reason: str, exit_price: float, status="CLOSED"):
    pos = STATE.current_position
    if not pos:
        return

    exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
    _place_market_order(pos["symbol"], pos["token"], exit_side, pos.get("qty", QTY), pos.get("exchange", "MCX"))

    entry_price = float(pos["entry_price"])
    qty = float(pos.get("qty", QTY))
    pnl = (exit_price - entry_price) * qty if pos["side"] == "BUY" else (entry_price - exit_price) * qty
    STATE.realized_pnl += pnl
    STATE.last_exit_time = datetime.now()
    STATE.last_exit_reason = reason
    STATE.last_action = f"EXIT_{reason}"

    is_sl = reason == "SL"
    is_giveback = reason == "GIVEBACK"

    if is_sl:
        STATE.consecutive_sl += 1

        trend_side = pos.get("trend_side")
        bullish_trend = STATE.ema20 and STATE.ema50 and STATE.ema20 > STATE.ema50
        bearish_trend = STATE.ema20 and STATE.ema50 and STATE.ema20 < STATE.ema50

        if ((trend_side == "BUY" and bullish_trend) or
            (trend_side == "SELL" and bearish_trend)):
            STATE.reentry_allowed = True
            STATE.reentry_allowed_until = datetime.now() + timedelta(seconds=REENTRY_WINDOW_SEC)
            STATE.reentry_count = 0

            if trend_side == "BUY":
                STATE.reentry_reference_price = float(pos.get("highest_price", exit_price))
            else:
                STATE.reentry_reference_price = float(pos.get("lowest_price", exit_price))

            print(
                f"🎯 RE-ENTRY ARMED | side={trend_side} | "
                f"anchor={STATE.reentry_reference_price:.2f} | "
                f"window={REENTRY_WINDOW_SEC}s"
            )
        else:
            STATE.reentry_allowed = False
            STATE.reentry_allowed_until = None
            STATE.reentry_reference_price = None
            STATE.reentry_count = 0
    elif is_giveback:
        # Giveback = profit taken. Cooldown before next entry, no reentry arming.
        STATE.consecutive_sl = 0
        STATE.reentry_allowed = False
        STATE.reentry_allowed_until = None
        STATE.reentry_reference_price = None
        STATE.reentry_count = 0
        STATE.last_exit_time = datetime.now()  # cooldown uses this
        print(f"🎯 GIVEBACK COOLDOWN | no re-entry for {COOLDOWN_MINUTES} min")
    else:
        STATE.consecutive_sl = 0
        STATE.reentry_allowed = False
        STATE.reentry_allowed_until = None
        STATE.reentry_reference_price = None
        STATE.reentry_count = 0

    result = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"
    log_trade_exit(pos["trade_id"], exit_price=exit_price, result=result, reason=reason, status=status)

    exit_signal = ""
    if pos.get("instrument") == "CE":
        exit_signal = "CE SELL"
    elif pos.get("instrument") == "PE":
        exit_signal = "PE SELL"
    elif pos.get("instrument") == "FUT":
        exit_signal = "FUT SELL"

    if exit_signal:
        write_market_data(exit_signal)

    print(f"✅ EXIT {pos['side']} | {reason} | exit={exit_price:.2f} pnl={pnl:.2f}")

    # ─── LOG PEAK vs ACTUAL PROFIT (left-on-table analysis) ──────────────
    entry_price_val = float(pos.get("entry_price", 0))
    highest = float(pos.get("highest_price", exit_price))
    lowest = float(pos.get("lowest_price", exit_price))
    qty_val = float(pos.get("qty", QTY))
    trend_side_val = pos.get("trend_side", pos.get("side", "BUY"))

    if pos["side"] == "BUY":
        peak_profit = (highest - entry_price_val) * qty_val
    else:
        peak_profit = (entry_price_val - lowest) * qty_val

    left_on_table = peak_profit - pnl if peak_profit > pnl else 0.0
    capture_pct = (pnl / peak_profit * 100) if peak_profit > 0 else 0.0

    print(
        f"📊 PROFIT ANALYSIS | peak_profit={peak_profit:.2f} | actual_pnl={pnl:.2f} | "
        f"left_on_table={left_on_table:.2f} | captured={capture_pct:.0f}% | "
        f"exit_reason={reason} | highest={highest:.2f} lowest={lowest:.2f}"
    )
    STATE.current_position = None
    STATE.current_trade_id = None
    write_state("TRADE_CLOSED")


def manage_open_position(candle):
    pos = STATE.current_position
    if not pos:
        return

    atr = candle.atr
    if atr is None or atr <= 0:
        return
    token = pos.get("token")

    if not token:
        for item in getattr(STATE, "instrument_data", []):
            if str(item.get("symbol", "")).upper() == str(pos.get("symbol", "")).upper():
                token = str(item.get("token"))
                pos["token"] = token
                pos["exchange"] = item.get("exch_seg", "NFO")
                break

    option_ltp = get_option_ltp(pos["symbol"], token, exchange=pos.get("exchange", "NFO")) if token else None
    price = float(option_ltp) if option_ltp is not None else float(candle.close)

    pos["current_ltp"] = price
    qty = float(pos.get("remaining_qty") or pos.get("qty") or QTY)
    
    if pos["side"] == "BUY":
        pos["mtm_points"] = price - float(pos["entry_price"])
    else:
        pos["mtm_points"] = float(pos["entry_price"]) - price

    pos["mtm_pnl"] = pos["mtm_points"] * qty

    # Use trend_side for directional logic (pos["side"] is always BUY for options)
    trend_side = pos.get("trend_side", pos.get("side", "BUY"))
    is_bullish = trend_side == "BUY"   # CE or FUT BUY
    is_bearish = trend_side == "SELL"  # PE or FUT SELL

    if pos["side"] == "BUY":
        pos["highest_price"] = max(float(pos.get("highest_price", price)), price)
        pos["lowest_price"] = min(float(pos.get("lowest_price", price)), price)

        # Adaptive trailing — slower when trend is strong and move is big
        entry_price_val = float(pos.get("entry_price", 0))

        # For CE/bullish: track move up from entry. For PE/bearish: premium rises when index drops
        # but option premium tracking is always highest_price - entry for BUY side
        move_from_entry = pos["highest_price"] - entry_price_val

        # Trend strength check uses trend_side, not pos["side"]
        if is_bullish:
            trend_strong = (
                candle.fast_ema is not None and candle.slow_ema is not None
                and candle.fast_ema > candle.slow_ema
                and candle.close > candle.slow_ema
                and candle.fast_ema > float(candle.slow_ema) + (atr * 0.15)
            )
        else:
            # PE trade: bearish trend is strong when EMA9 < EMA21 and expanding
            trend_strong = (
                candle.fast_ema is not None and candle.slow_ema is not None
                and candle.fast_ema < candle.slow_ema
                and candle.close < candle.slow_ema
                and candle.fast_ema < float(candle.slow_ema) - (atr * 0.15)
            )

        if trend_strong and move_from_entry >= atr * 1.5:
            trail_multiplier = ATR_TRAIL_MULTIPLIER * 2.0  # slower trail in strong trend
        else:
            trail_multiplier = ATR_TRAIL_MULTIPLIER

        # Move SL to breakeven once move >= 1.5*ATR — let EMA rejection be primary exit
        if move_from_entry >= atr * 1.5:
            breakeven_sl = entry_price_val + 1.0  # 1pt above entry to cover slippage
            pos["sl"] = max(float(pos["sl"]), breakeven_sl)

        # ── LEADING PHASE PROFIT LOCK ────────────────────────────────────
        # Layer 1: When in profit and leading is EXPANDING → lock breakeven
        # Layer 2: When leading PEAKED → tighten SL aggressively
        leading_phase = getattr(STATE, "leading_phase", "SIDEWAYS")
        if move_from_entry > 0:
            if leading_phase in ("PEAKED_UP", "PEAKED_DOWN", "COMPRESSING_DOWN", "COMPRESSING_UP"):
                # Leading peaked — tighten SL to lock most profit
                tight_sl = pos["highest_price"] - (atr * 0.3)
                if tight_sl > float(pos["sl"]):
                    pos["sl"] = tight_sl
                    print(
                        f"🔒 LEADING PHASE LOCK | phase={leading_phase} | "
                        f"SL tightened to {tight_sl:.2f} | peak={pos['highest_price']:.2f}"
                    )
            elif leading_phase in ("EXPANDING_UP", "EXPANDING_DOWN"):
                # Still expanding — just ensure breakeven
                if move_from_entry >= atr * 0.5:
                    be_sl = entry_price_val + 1.0
                    if be_sl > float(pos["sl"]):
                        pos["sl"] = be_sl

        trail_sl = pos["highest_price"] - (atr * trail_multiplier)
        pos["sl"] = max(float(pos["sl"]), trail_sl)

        # activate profit-lock only after meaningful move
        if pos["highest_price"] >= entry_price_val + (atr * ATR_PROFIT_LOCK_MULTIPLIER):
            pos["profit_lock_price"] = pos["highest_price"]

        # 1) HARD STOP / TRAILING STOP — always first
        if price <= float(pos["sl"]):
            _exit_position("SL", price)
            return

        # 2) ENTRY-TYPE BASED EXIT
        ema_gap = abs(float(candle.fast_ema) - float(candle.slow_ema))
        entry_time = _parse_entry_time(pos)
        hold_seconds = (datetime.now() - entry_time).total_seconds() if entry_time else 9999
        entry_reason = pos.get("entry_reason", "")

        # PREBUY/PRESELL: tighter exit — EMA9 rejection (close on wrong side of EMA9)
        if entry_reason in ("PREBUY", "PRESELL") and hold_seconds >= 60:
            if is_bullish and candle.close < candle.fast_ema:
                print(
                    f"🔁 PREBUY EMA9 EXIT | close={candle.close:.2f} < EMA9={candle.fast_ema:.2f} | "
                    f"hold={hold_seconds:.0f}s"
                )
                _exit_position("REVERSAL", price)
                return
            if is_bearish and candle.close > candle.fast_ema:
                print(
                    f"🔁 PRESELL EMA9 EXIT | close={candle.close:.2f} > EMA9={candle.fast_ema:.2f} | "
                    f"hold={hold_seconds:.0f}s"
                )
                _exit_position("REVERSAL", price)
                return

        # ALL ENTRIES: EMA21 rejection — close on wrong side of EMA21
        if is_bullish and candle.close < candle.slow_ema and hold_seconds >= 120:
            print(
                f"🔁 EMA REJECTION EXIT BUY | close={candle.close:.2f} < EMA21={candle.slow_ema:.2f} | "
                f"hold={hold_seconds:.0f}s"
            )
            _exit_position("REVERSAL", price)
            return

        if is_bearish and candle.close > candle.slow_ema and hold_seconds >= 120:
            print(
                f"🔁 EMA REJECTION EXIT SELL | close={candle.close:.2f} > EMA21={candle.slow_ema:.2f} | "
                f"hold={hold_seconds:.0f}s"
            )
            _exit_position("REVERSAL", price)
            return

        # 3) REVERSAL EXIT — EMA crossed with strong gap + min hold time
        # CE/bullish: bearish crossover is reversal. PE/bearish: bullish crossover is reversal.
        if is_bullish and candle.fast_ema < candle.slow_ema and ema_gap > (atr * 0.60) and hold_seconds >= 300:
            print(f"🔁 REVERSAL EXIT CE/BUY | gap={ema_gap:.2f} | hold={hold_seconds:.0f}s")
            _exit_position("REVERSAL", price)
            return

        if is_bearish and candle.fast_ema > candle.slow_ema and ema_gap > (atr * 0.60) and hold_seconds >= 300:
            print(f"🔁 REVERSAL EXIT PE/SELL | gap={ema_gap:.2f} | hold={hold_seconds:.0f}s")
            _exit_position("REVERSAL", price)
            return

        # 4) SWING EXIT — use EMA cycle phase + peak separation for smarter giveback
        # EMAs lag price — by the time gap contracts, price has already dropped.
        # So activate giveback at PEAK itself (gap stopped expanding), not after contraction.
        entry_price = float(pos.get("entry_price", 0))
        current_price = float(price)
        recent_high = float(pos["highest_price"])
        move_from_entry = recent_high - entry_price
        ema_phase = getattr(STATE, "ema_cycle_phase", "EXPANDING")
        peak_gap = getattr(STATE, "ema_cycle_peak_gap", 0.0)
        curr_ema_gap = abs(candle.fast_ema - candle.slow_ema) if candle.fast_ema and candle.slow_ema else 0.0

        # How much has the gap fallen from its peak? (0.0 = at peak, 1.0 = fully collapsed)
        gap_retracement = (peak_gap - curr_ema_gap) / peak_gap if peak_gap > 0 else 0.0

        # ATR-relative thresholds — scales with instrument volatility
        giveback_min_move = atr * 1.5       # minimum move to activate giveback
        giveback_medium_move = atr * 3.0    # medium move threshold
        giveback_large_move = atr * 4.0     # large move threshold

        if move_from_entry >= giveback_min_move:
            # ── PEAK: gap just stopped expanding — activate giveback immediately ──
            # Price drops faster than EMA gap, so lock profit at the first sign of peak
            if ema_phase == "PEAK" and peak_gap >= atr * 0.25:
                giveback_pct = 0.10   # tight lock — peak means momentum is fading

                swing_exit = recent_high - (move_from_entry * giveback_pct)
                swing_exit = max(swing_exit, entry_price)

                if current_price <= swing_exit:
                    print(
                        f"🎯 SWING EXIT {trend_side} | PEAK | "
                        f"peak_gap={peak_gap:.2f} curr_gap={curr_ema_gap:.2f} | "
                        f"high={recent_high:.2f} move={move_from_entry:.2f} pct={giveback_pct:.0%} "
                        f"exit={swing_exit:.2f} price={current_price:.2f}"
                    )
                    _exit_position("GIVEBACK", current_price)
                    return
                else:
                    print(
                        f"📊 GIVEBACK ACTIVE {trend_side} | PEAK | "
                        f"peak_gap={peak_gap:.2f} | exit_at={swing_exit:.2f} price={current_price:.2f}"
                    )

            # ── CONTRACTING: gap shrinking — even tighter lock ───────────────
            elif ema_phase == "CONTRACTING":
                if gap_retracement >= 0.50:
                    giveback_pct = 0.06   # gap half gone — lock very hard
                elif gap_retracement >= 0.30:
                    giveback_pct = 0.08
                else:
                    giveback_pct = 0.10

                swing_exit = recent_high - (move_from_entry * giveback_pct)
                swing_exit = max(swing_exit, entry_price)

                if current_price <= swing_exit:
                    print(
                        f"🎯 SWING EXIT {trend_side} | CONTRACTING | "
                        f"separation_drop={gap_retracement:.0%} | "
                        f"peak={peak_gap:.2f} curr={curr_ema_gap:.2f} | "
                        f"high={recent_high:.2f} move={move_from_entry:.2f} pct={giveback_pct:.0%} "
                        f"exit={swing_exit:.2f} price={current_price:.2f}"
                    )
                    _exit_position("GIVEBACK", current_price)
                    return
                else:
                    print(
                        f"📊 GIVEBACK ACTIVE {trend_side} | CONTRACTING | "
                        f"drop={gap_retracement:.0%} | exit_at={swing_exit:.2f} price={current_price:.2f}"
                    )

            elif trend_strong and move_from_entry < giveback_medium_move:
                # EXPANDING + strong trend + small move — suppress giveback
                print(
                    f"🚀 GIVEBACK SUPPRESSED {trend_side} | trend strong | high={recent_high:.2f} "
                    f"move={move_from_entry:.2f} | {'EMA9>EMA21 & price>EMA21' if is_bullish else 'EMA9<EMA21 & price<EMA21'}"
                )
            else:
                # Large move OR weak trend — normal giveback
                if move_from_entry >= giveback_large_move:
                    giveback_pct = 0.20
                elif move_from_entry >= giveback_medium_move:
                    giveback_pct = 0.18
                elif move_from_entry < atr:
                    giveback_pct = 0.12
                else:
                    giveback_pct = 0.15

                swing_exit = recent_high - (move_from_entry * giveback_pct)
                swing_exit = max(swing_exit, entry_price)

                if current_price <= swing_exit:
                    print(
                        f"🎯 SWING EXIT {trend_side} | high={recent_high:.2f} "
                        f"move={move_from_entry:.2f} pct={giveback_pct:.0%} "
                        f"exit={swing_exit:.2f} price={current_price:.2f}"
                    )
                    _exit_position("GIVEBACK", current_price)
                    return

        # 5) target milestone — exit during PEAK/CONTRACTING, informational during EXPANDING
        if price >= float(pos["target"]):
            ema_phase_exit = getattr(STATE, "ema_cycle_phase", "EXPANDING")
            if ema_phase_exit in ("PEAK", "CONTRACTING", "SIDEWAYS"):
                print(
                    f"🎯 TARGET EXIT {trend_side} | cycle={ema_phase_exit} | "
                    f"price={price:.2f} target={float(pos['target']):.2f} | "
                    f"trend fading — taking profit"
                )
                _exit_position("TARGET", price)
                return
            else:
                print(
                    f"🎯 TARGET ZONE REACHED | price={price:.2f} | "
                    f"target={float(pos['target']):.2f} | cycle={ema_phase_exit} — letting it run"
                )

    else:
        # FUT SELL side — not used for options (options always side=BUY)
        pos["lowest_price"] = min(float(pos.get("lowest_price", price)), price)

        # Adaptive trailing — slower when trend is strong and move is big
        entry_price_val = float(pos.get("entry_price", 0))
        move_from_entry = entry_price_val - pos["lowest_price"]
        fut_trend_side = pos.get("trend_side", "SELL")

        trend_strong_sell = (
            fut_trend_side == "SELL"
            and candle.fast_ema is not None and candle.slow_ema is not None
            and candle.fast_ema < candle.slow_ema
            and candle.close < candle.slow_ema
            and candle.fast_ema < float(candle.slow_ema) - (atr * 0.15)
        )

        if trend_strong_sell and move_from_entry >= atr * 1.5:
            trail_multiplier = ATR_TRAIL_MULTIPLIER * 2.0
        else:
            trail_multiplier = ATR_TRAIL_MULTIPLIER

        # Move SL to breakeven once move >= 1.5*ATR — let EMA rejection be primary exit
        if move_from_entry >= atr * 1.5:
            breakeven_sl = entry_price_val - 1.0
            pos["sl"] = min(float(pos["sl"]), breakeven_sl)

        # ── LEADING PHASE PROFIT LOCK (SELL side) ────────────────────────
        leading_phase = getattr(STATE, "leading_phase", "SIDEWAYS")
        if move_from_entry > 0:
            if leading_phase in ("PEAKED_UP", "PEAKED_DOWN", "COMPRESSING_DOWN", "COMPRESSING_UP"):
                tight_sl = pos["lowest_price"] + (atr * 0.3)
                if tight_sl < float(pos["sl"]):
                    pos["sl"] = tight_sl
                    print(
                        f"🔒 LEADING PHASE LOCK | phase={leading_phase} | "
                        f"SL tightened to {tight_sl:.2f} | trough={pos['lowest_price']:.2f}"
                    )
            elif leading_phase in ("EXPANDING_UP", "EXPANDING_DOWN"):
                if move_from_entry >= atr * 0.5:
                    be_sl = entry_price_val - 1.0
                    if be_sl < float(pos["sl"]):
                        pos["sl"] = be_sl

        trail_sl = pos["lowest_price"] + (atr * trail_multiplier)
        pos["sl"] = min(float(pos["sl"]), trail_sl)

        # activate profit-lock only after meaningful move
        if pos["lowest_price"] <= entry_price_val - (atr * ATR_PROFIT_LOCK_MULTIPLIER):
            pos["profit_lock_price"] = pos["lowest_price"]

        # 1) HARD STOP / TRAILING STOP — always first
        if price >= float(pos["sl"]):
            _exit_position("SL", price)
            return

        # 2) ENTRY-TYPE BASED EXIT
        ema_gap = abs(float(candle.fast_ema) - float(candle.slow_ema))
        entry_time = _parse_entry_time(pos)
        hold_seconds = (datetime.now() - entry_time).total_seconds() if entry_time else 9999
        entry_reason = pos.get("entry_reason", "")

        # PREBUY/PRESELL: tighter exit — EMA9 rejection
        if entry_reason in ("PREBUY", "PRESELL") and hold_seconds >= 60:
            if fut_trend_side == "SELL" and candle.close > candle.fast_ema:
                print(
                    f"🔁 PRESELL EMA9 EXIT | close={candle.close:.2f} > EMA9={candle.fast_ema:.2f} | "
                    f"hold={hold_seconds:.0f}s"
                )
                _exit_position("REVERSAL", price)
                return
            if fut_trend_side == "BUY" and candle.close < candle.fast_ema:
                print(
                    f"🔁 PREBUY EMA9 EXIT | close={candle.close:.2f} < EMA9={candle.fast_ema:.2f} | "
                    f"hold={hold_seconds:.0f}s"
                )
                _exit_position("REVERSAL", price)
                return

        # ALL ENTRIES: EMA21 rejection
        if fut_trend_side == "SELL" and candle.close > candle.slow_ema and hold_seconds >= 120:
            print(
                f"🔁 EMA REJECTION EXIT SELL | close={candle.close:.2f} > EMA21={candle.slow_ema:.2f} | "
                f"hold={hold_seconds:.0f}s"
            )
            _exit_position("REVERSAL", price)
            return

        if fut_trend_side == "BUY" and candle.close < candle.slow_ema and hold_seconds >= 120:
            print(
                f"🔁 EMA REJECTION EXIT BUY | close={candle.close:.2f} < EMA21={candle.slow_ema:.2f} | "
                f"hold={hold_seconds:.0f}s"
            )
            _exit_position("REVERSAL", price)
            return

        # 3) REVERSAL EXIT with ATR buffer + min hold time
        if (
            fut_trend_side == "BUY"
            and candle.fast_ema < candle.slow_ema
            and ema_gap > (atr * 0.60)
            and hold_seconds >= 300
        ):
            print(f"🔁 REVERSAL EXIT FUT BUY | gap={ema_gap:.2f} | hold={hold_seconds:.0f}s")
            _exit_position("REVERSAL", price)
            return

        if (
            fut_trend_side == "SELL"
            and candle.fast_ema > candle.slow_ema
            and ema_gap > (atr * 0.60)
            and hold_seconds >= 300
        ):
            print(f"🔁 REVERSAL EXIT FUT SELL | gap={ema_gap:.2f} | hold={hold_seconds:.0f}s")
            _exit_position("REVERSAL", price)
            return

        # 4) SWING LOW EXIT — use EMA cycle phase + peak separation for smarter giveback
        entry_price = float(pos.get("entry_price", 0))
        current_price = float(price)
        recent_low = float(pos["lowest_price"])
        move_from_entry = entry_price - recent_low
        ema_phase = getattr(STATE, "ema_cycle_phase", "EXPANDING")
        peak_gap = getattr(STATE, "ema_cycle_peak_gap", 0.0)
        curr_ema_gap = abs(candle.fast_ema - candle.slow_ema) if candle.fast_ema and candle.slow_ema else 0.0

        gap_retracement = (peak_gap - curr_ema_gap) / peak_gap if peak_gap > 0 else 0.0

        # ATR-relative thresholds
        giveback_min_move = atr * 1.5
        giveback_medium_move = atr * 3.0
        giveback_large_move = atr * 4.0

        if move_from_entry >= giveback_min_move:
            # ── PEAK: gap just stopped expanding — activate giveback immediately ──
            if ema_phase == "PEAK" and peak_gap >= atr * 0.25:
                giveback_pct = 0.10

                swing_exit = recent_low + (move_from_entry * giveback_pct)
                swing_exit = min(swing_exit, entry_price)

                if current_price >= swing_exit:
                    print(
                        f"🎯 SWING LOW EXIT SELL | PEAK | "
                        f"peak_gap={peak_gap:.2f} curr_gap={curr_ema_gap:.2f} | "
                        f"low={recent_low:.2f} move={move_from_entry:.2f} pct={giveback_pct:.0%} "
                        f"exit={swing_exit:.2f} price={current_price:.2f}"
                    )
                    _exit_position("GIVEBACK", current_price)
                    return
                else:
                    print(
                        f"📊 GIVEBACK ACTIVE SELL | PEAK | "
                        f"peak_gap={peak_gap:.2f} | exit_at={swing_exit:.2f} price={current_price:.2f}"
                    )

            # ── CONTRACTING: gap shrinking — even tighter lock ───────────────
            elif ema_phase == "CONTRACTING":
                if gap_retracement >= 0.50:
                    giveback_pct = 0.06
                elif gap_retracement >= 0.30:
                    giveback_pct = 0.08
                else:
                    giveback_pct = 0.10

                swing_exit = recent_low + (move_from_entry * giveback_pct)
                swing_exit = min(swing_exit, entry_price)

                if current_price >= swing_exit:
                    print(
                        f"🎯 SWING LOW EXIT SELL | CONTRACTING | "
                        f"separation_drop={gap_retracement:.0%} | "
                        f"peak={peak_gap:.2f} curr={curr_ema_gap:.2f} | "
                        f"low={recent_low:.2f} move={move_from_entry:.2f} pct={giveback_pct:.0%} "
                        f"exit={swing_exit:.2f} price={current_price:.2f}"
                    )
                    _exit_position("GIVEBACK", current_price)
                    return
                else:
                    print(
                        f"📊 GIVEBACK ACTIVE SELL | CONTRACTING | "
                        f"drop={gap_retracement:.0%} | exit_at={swing_exit:.2f} price={current_price:.2f}"
                    )

            elif trend_strong_sell and move_from_entry < giveback_medium_move:
                print(
                    f"🚀 GIVEBACK SUPPRESSED SELL | trend strong | low={recent_low:.2f} "
                    f"move={move_from_entry:.2f} | EMA9<EMA21 & price<EMA21"
                )
            else:
                if move_from_entry >= giveback_large_move:
                    giveback_pct = 0.20
                elif move_from_entry >= giveback_medium_move:
                    giveback_pct = 0.18
                elif move_from_entry < atr:
                    giveback_pct = 0.12
                else:
                    giveback_pct = 0.15

                swing_exit = recent_low + (move_from_entry * giveback_pct)
                swing_exit = min(swing_exit, entry_price)

                if current_price >= swing_exit:
                    print(
                        f"🎯 SWING LOW EXIT SELL | low={recent_low:.2f} "
                        f"move={move_from_entry:.2f} pct={giveback_pct:.0%} "
                        f"exit={swing_exit:.2f} price={current_price:.2f}"
                    )
                    _exit_position("GIVEBACK", current_price)
                    return
    write_state("POSITION_MANAGED")

def rebuild_position_after_restart():
    pos = STATE.current_position
    if not pos or not STATE.atr:
        return

    entry = float(pos.get("entry_price", 0.0))
    atr = float(STATE.atr)
    side = pos.get("side")
    if side == "BUY":
        pos["sl"] = max(float(pos.get("sl", entry - atr * ATR_SL_MULTIPLIER)), entry - atr * ATR_SL_MULTIPLIER)
        pos["target"] = max(float(pos.get("target", entry + atr * ATR_TARGET_MULTIPLIER)), entry + atr * ATR_TARGET_MULTIPLIER)
        pos["highest_price"] = max(float(pos.get("highest_price", entry)), entry)
    else:
        pos["sl"] = min(float(pos.get("sl", entry + atr * ATR_SL_MULTIPLIER)), entry + atr * ATR_SL_MULTIPLIER)
        pos["target"] = min(float(pos.get("target", entry - atr * ATR_TARGET_MULTIPLIER)), entry - atr * ATR_TARGET_MULTIPLIER)
        pos["lowest_price"] = min(float(pos.get("lowest_price", entry)), entry)
    print(f"🔧 Rebuilt position | SL={pos['sl']:.2f} TARGET={pos['target']:.2f}")

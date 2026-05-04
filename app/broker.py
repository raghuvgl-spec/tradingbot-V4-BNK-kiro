import json
import sys
import time as time_module
from datetime import datetime, date
from pathlib import Path
import pandas as pd
import pyotp
import requests
from requests.exceptions import RequestException, SSLError
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2


from app.config import (
    API_KEY,
    CLIENT_ID,
    PASSWORD,
    TOTP_SECRET,
    INDEX_TOKEN,
    DEBUG_MODE,
    DEBUG_TOKEN,
    DEBUG_EXCHANGE_TYPE,
    LIVE_SYMBOL,
    PAPER_TRADING,
    RECONCILE_WITH_BROKER_ON_STARTUP,
    SL_POINTS,
    TARGET_POINTS,
    ATR_SL_MULTIPLIER,
    ATR_TARGET_MULTIPLIER,
    MARKET_DATA_FILE
)
from app.state import STATE
from app.utils import safe_api_call
from app.files import write_market_data, write_state

STATE.reconnecting = False

INSTRUMENT_CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "instrument_master.json"

# 🔥 Dashboard throttle writer
_last_state_push_ts = 0.0

def push_dashboard_state_throttled(interval_sec=0.5):
    global _last_state_push_ts
    now = time_module.time()

    if now - _last_state_push_ts >= interval_sec:
        write_state("WS_TICK")
        _last_state_push_ts = now

#Login into Broker
def login():
    today = date.today()
    week_day = today.strftime("%A")

    if week_day in ["Sunday", "Saturday"] and not DEBUG_MODE:
        print(f"Today is {week_day}. Market is closed.")
        sys.exit()

    STATE.obj = SmartConnect(api_key=API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    session = safe_api_call(lambda: STATE.obj.generateSession(CLIENT_ID, PASSWORD, totp))

    if not session or not session.get("data"):
        raise RuntimeError("Login failed")

    STATE.auth_token = session["data"]["jwtToken"]
    STATE.feed_token = STATE.obj.getfeedToken()
    print("Login successful")
    write_state("LOGIN_SUCCESS")

#Loading Trading Instruments 
def load_instruments():
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

    # ✅ Step 1: Try cache first
    if INSTRUMENT_CACHE_FILE.exists():
        try:
            print("⚡ Loading instrument master from cache")
            STATE.instrument_data = json.loads(
                INSTRUMENT_CACHE_FILE.read_text(encoding="utf-8")
            )
            print("✅ Instrument master loaded from cache")
            return
        except Exception as e:
            print("⚠️ Cache read failed, downloading fresh:", e)

    # 🔁 Step 2: Download only if cache missing or failed
    last_error = None

    for attempt in range(1, 4):
        try:
            print(f"Downloading instrument master... attempt {attempt}/3")
            response = requests.get(url, timeout=20)
            response.raise_for_status()

            data = response.json()
            if not isinstance(data, list) or not data:
                raise ValueError("Invalid instrument data")

            STATE.instrument_data = data

            INSTRUMENT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            INSTRUMENT_CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")

            print("✅ Instrument master downloaded and cached")
            return

        except (RequestException, SSLError, ValueError) as e:
            last_error = e
            print(f"❌ Attempt {attempt} failed: {e}")
            time_module.sleep(2)

    raise RuntimeError(f"❌ Instrument load failed: {last_error}")

#From Loaded Instrument selecting Options 
def get_option_symbol(index_ltp, option_type):
    strike = round(index_ltp / 100) * 100
    matches = []
    today = date.today()
    for item in getattr(STATE, "instrument_data", []):
        symbol = item.get("symbol", "")
        expiry_str = item.get("expiry", "")

        if not (
            LIVE_SYMBOL in symbol
            and str(strike) in symbol
            and option_type in symbol
            and item.get("exch_seg") == "NFO"
        ):
            continue
        try:
            expiry_date = datetime.strptime(expiry_str, "%d%b%Y").date()
        except Exception:
            try:
                expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            except Exception:
                continue
        if expiry_date < today:
            continue
        matches.append((expiry_date, item))
    if not matches:
        return None, None
   
    matches.sort(key=lambda x: x[0])
    current_year = today.year
    current_month = today.month
    # 🟢 CASE 1: NIFTY → use WEEKLY (nearest expiry)
    if LIVE_SYMBOL == "NIFTY":
        best = matches[0][1]
        print("✅ NIFTY WEEKLY SELECTED:", best["symbol"], "| EXPIRY:", best.get("expiry"))
        return best["symbol"], best["token"]

    # 🔵 CASE 2: BANKNIFTY → use CURRENT MONTH MONTHLY
    elif LIVE_SYMBOL == "BANKNIFTY":
        # get current month expiries (or next month if no current month left)
        current_month_matches = [
            (expiry_date, item)
            for expiry_date, item in matches
            if (expiry_date.year, expiry_date.month) >= (today.year, today.month)
        ]
        if not current_month_matches:
            print("❌ No BANKNIFTY monthly options found")
            return None, None
        # sort and pick nearest expiry
        current_month_matches.sort(key=lambda x: x[0])
        best = current_month_matches[0][1]
        print("✅ BANKNIFTY MONTHLY:", best["symbol"], "| EXPIRY:", best.get("expiry"))
        return best["symbol"], best["token"]

#Getting Option Last Traded Price
def get_option_ltp(symbol, token, exchange="NFO"):
    data = safe_api_call(lambda: STATE.obj.ltpData(exchange, symbol, token))
    if not data or not data.get("data"):
        return None

    try:
        return float(data["data"]["ltp"])
    except Exception:
        return None
    
#Orders with Borker Position Status 
def get_broker_positions():
    if PAPER_TRADING:
        return []

    response = safe_api_call(lambda: STATE.obj.position())
    if not response:
        return []

    if isinstance(response, dict):
        return response.get("data") or response.get("positions") or []
    if isinstance(response, list):
        return response
    return []

# Future Instrument 
def _infer_instrument_from_symbol(symbol):
    symbol = str(symbol).upper()
    if "FUT" in symbol:
        return "FUT"
    if symbol.endswith("CE") or "CE" in symbol:
        return "CE"
    if symbol.endswith("PE") or "PE" in symbol:
        return "PE"
    return "OPT"

#Rebuilding StopLoss and Targer from Broker  
def _rebuild_sl_target_from_broker(entry_price, side):
    atr = getattr(STATE, "atr", None)

    if atr is not None and atr > 0:
        sl_distance = atr * ATR_SL_MULTIPLIER
        target_distance = atr * ATR_TARGET_MULTIPLIER
        print(f"📊 Using ATR rebuild | ATR={atr:.2f}")
    else:
        sl_distance = float(SL_POINTS)
        target_distance = float(TARGET_POINTS)
        print("⚠️ ATR unavailable -> using fixed SL/TARGET")

    if side == "BUY":
        sl = entry_price - sl_distance
        target = entry_price + target_distance
    else:
        sl = entry_price + sl_distance
        target = entry_price - target_distance

    return sl, target

#seed trailing from ticks
def _seed_trailing_from_ticks(entry_price, side, entry_dt):
    from app.files import load_ticks_after_entry

    ticks = load_ticks_after_entry(entry_dt)
    if not ticks:
        print("⚠️ No tick history after entry -> falling back to candle-based trailing")
        return None

    prices = []
    for row in ticks:
        try:
            prices.append(float(row[1]))
        except Exception:
            continue

    if not prices:
        print("⚠️ Tick history invalid -> falling back to candle-based trailing")
        return None

    highest_price = max(prices)
    lowest_price = min(prices)
    latest_price = prices[-1]

    if side == "BUY":
        last_trail_price = max(entry_price, latest_price)
    else:
        last_trail_price = min(entry_price, latest_price)

    print(
        f"📈 Tick-level trailing rebuild | "
        f"ticks={len(prices)} | "
        f"last_trail={last_trail_price:.2f} | "
        f"high={highest_price:.2f} | low={lowest_price:.2f}"
    )

    return last_trail_price, highest_price, lowest_price

def _seed_trailing_from_history(entry_price, side):
    candles = getattr(STATE, "latest_closed_candles", []) or []

    if not candles:
        print("⚠️ No candle history -> trailing seeded from entry price")
        return entry_price, entry_price, entry_price

    closes = []
    highs = []
    lows = []

    for row in candles:
        try:
            highs.append(float(row[2]))
            lows.append(float(row[3]))
            closes.append(float(row[4]))
        except Exception:
            continue

    if not closes:
        print("⚠️ Candle history invalid -> trailing seeded from entry price")
        return entry_price, entry_price, entry_price

    latest_close = closes[-1]

    if side == "BUY":
        highest_price = max(highs) if highs else latest_close
        lowest_price = min(lows) if lows else latest_close
        last_trail_price = max(entry_price, latest_close)
    else:
        highest_price = max(highs) if highs else latest_close
        lowest_price = min(lows) if lows else latest_close
        last_trail_price = min(entry_price, latest_close)

    print(
        f"📈 Trailing rebuilt from history | "
        f"last_trail={last_trail_price:.2f} | "
        f"high={highest_price:.2f} | low={lowest_price:.2f}"
    )

    return last_trail_price, highest_price, lowest_price

def _parse_dt(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    text = str(value).strip()

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass

    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _get_saved_local_entry_time():
    pos = getattr(STATE, "current_position", None)
    if not pos:
        return None
    return _parse_dt(pos.get("entry_time"))


def _find_broker_entry_time_for_symbol(symbol, side_hint=None):
    """
    Best effort:
    tries order book if available on broker object.
    Returns datetime or None.
    """
    if getattr(STATE, "obj", None) is None:
        return None

    try:
        # Smart API environments differ; this is best-effort only.
        response = safe_api_call(lambda: STATE.obj.orderBook())
        if not response:
            return None

        data = response.get("data", response) if isinstance(response, dict) else response
        if not isinstance(data, list):
            return None

        matched_times = []

        for order in data:
            if not isinstance(order, dict):
                continue

            order_symbol = (
                order.get("tradingsymbol")
                or order.get("symbolname")
                or order.get("symbol")
                or ""
            )

            if str(order_symbol).upper() != str(symbol).upper():
                continue

            status = str(order.get("status") or order.get("orderstatus") or "").upper()
            if "COMPLETE" not in status and "FILLED" not in status:
                continue

            txn = str(order.get("transactiontype") or order.get("transactionType") or "").upper()
            if side_hint and txn and txn != str(side_hint).upper():
                continue

            t = (
                order.get("updatetime")
                or order.get("exchtime")
                or order.get("filledtime")
                or order.get("orderdatetime")
                or order.get("norentm")
            )

            dt = _parse_dt(t)
            if dt is not None:
                matched_times.append(dt)

        if matched_times:
            entry_dt = min(matched_times)
            print(f"🕒 Broker entry time identified: {entry_dt}")
            return entry_dt

    except Exception as e:
        print(f"⚠️ Could not read broker order book for entry time: {e}")

    return None


def _seed_trailing_from_entry_time(entry_price, side, entry_dt):
    candles = getattr(STATE, "latest_closed_candles", []) or []

    if not candles:
        print("⚠️ No candle history -> trailing seeded from entry price")
        return entry_price, entry_price, entry_price

    filtered = []
    for row in candles:
        try:
            candle_dt = _parse_dt(row[0])
            if candle_dt is None:
                continue
            if entry_dt is None or candle_dt > entry_dt:
                filtered.append(row)
        except Exception:
            continue

    if not filtered:
        print("⚠️ No post-entry candles -> trailing seeded from entry price")
        return entry_price, entry_price, entry_price

    highs, lows, closes = [], [], []
    for row in filtered:
        try:
            highs.append(float(row[2]))
            lows.append(float(row[3]))
            closes.append(float(row[4]))
        except Exception:
            continue

    if not closes:
        print("⚠️ Post-entry candle parse failed -> trailing seeded from entry price")
        return entry_price, entry_price, entry_price

    highest_price = max(highs)
    lowest_price = min(lows)
    latest_close = closes[-1]

    if side == "BUY":
        last_trail_price = max(entry_price, latest_close)
    else:
        last_trail_price = min(entry_price, latest_close)

    print(
        f"📈 Entry-time trailing rebuild | "
        f"candles={len(filtered)} | "
        f"last_trail={last_trail_price:.2f} | "
        f"high={highest_price:.2f} | low={lowest_price:.2f}"
    )

    return last_trail_price, highest_price, lowest_price

def reconcile_runtime_with_broker():
    if PAPER_TRADING or not RECONCILE_WITH_BROKER_ON_STARTUP:
        print("Broker reconciliation skipped")
        return

    if STATE.obj is None:
        print("⚠️ Broker not initialized, skipping reconciliation")
        return

    try:
        response = STATE.obj.position()
        if not response:
            print("⚠️ Empty broker response")
            return

        data = response.get("data", response) if isinstance(response, dict) else response
        if not isinstance(data, list):
            print("⚠️ Unexpected broker response format")
            return

        open_position = None

        for pos in data:
            if not isinstance(pos, dict):
                continue

            net_qty = pos.get("netqty") or pos.get("netQty") or pos.get("net_quantity") or 0
            try:
                net_qty = int(float(net_qty))
            except Exception:
                net_qty = 0

            if net_qty == 0:
                continue

            symbol = pos.get("tradingsymbol") or pos.get("symbol") or ""
            token = pos.get("symboltoken") or pos.get("token") or ""
            exchange = pos.get("exchange") or "NFO"

            avg_price = pos.get("averageprice") or pos.get("avgnetprice") or 0
            try:
                avg_price = float(avg_price)
            except Exception:
                avg_price = 0.0

            side = "BUY" if net_qty > 0 else "SELL"
            instrument = _infer_instrument_from_symbol(symbol)

            sl_value, target_value = _rebuild_sl_target_from_broker(avg_price, side)
            print(f"🔧 Rebuilt SL={sl_value:.2f} TARGET={target_value:.2f}")
            entry_dt = _find_broker_entry_time_for_symbol(symbol, side_hint=side)
            
            if entry_dt is None:
               entry_dt = _get_saved_local_entry_time()
               if entry_dt is not None:
                    print(f"🕒 Using saved local entry time: {entry_dt}")

            tick_seed = _seed_trailing_from_ticks(avg_price, side, entry_dt)
            if tick_seed is not None:
                last_trail_price, highest_price, lowest_price = tick_seed
            else:
                last_trail_price, highest_price, lowest_price = _seed_trailing_from_entry_time(
                   avg_price, side, entry_dt
                )
            latest_close = None
            candles = getattr(STATE, "latest_closed_candles", [])
            if candles:
                try:
                    latest_close = float(candles[-1][4])
                except:
                    latest_close = None

            open_position = {
                "symbol": symbol,
                "token": str(token),
                "instrument": instrument,
                "exchange": exchange,
                "side": side,
                "trend_side": side,
                "qty": abs(net_qty),
                "entry_price": avg_price,
                "entry_index_ltp": None,
                "signal_price": avg_price,
                "desired_entry": avg_price,
                "sl": sl_value,
                "target": target_value,
                "initial_sl": sl_value,
                "initial_target": target_value,
                "last_trail_price": last_trail_price,
                "highest_price": highest_price,
                "lowest_price": lowest_price,
                "partial_booked": False,
                "partial_qty": 0,
                "remaining_qty": abs(net_qty),
                "status": "OPEN",
                "entry_order_id": "BROKER_SYNC",
                "entry_time":(
                    entry_dt.strftime("%Y-%m-%d %H:%M:%S")
                    if entry_dt else datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                "paper_trade": False,
                "entry_logged": True,
                "trade_id": None,
                "entry_spot_ltp": latest_close,
                "entry_fast_ema": getattr(STATE, "ema20", None),
                "reentry_trade": False,
                "entry_reason": "BROKER_SYNC",
            }
            break

        if open_position:
            print(f"✅ Broker position found: {open_position['symbol']}")
            with STATE.lock:
                STATE.current_position = open_position
                STATE.pending_trade = None
                STATE.current_trade_id = None
                STATE.last_exit_time = None
                STATE.bot_block_reason = None
            write_state("BROKER_SYNC_OPEN")
            return

        print("ℹ️ No broker position found")
        with STATE.lock:
            STATE.current_position = None
            STATE.pending_trade = None
            STATE.current_trade_id = None
        write_state("BROKER_SYNC_RESET")

    except Exception as e:
        print(f"❌ Broker reconciliation failed: {e}")

def _minute_key(dt):
    return dt.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _append_tick_to_local_candle(ltp: float, msg: dict):
    current = getattr(STATE, "current_candle", None)
    closed = getattr(STATE, "latest_closed_candles", [])

    now = datetime.now()
    key = _minute_key(now)

    try:
        ltq = msg.get("last_traded_quantity")
        tick_volume = 0.0 if ltq is None else float(ltq)
    except Exception:
        tick_volume = 0.0

    if current is None:
    # 🔥 FIX: Resume from last candle if exists
        if closed:
            last = closed[-1]

            STATE.current_candle = {
                "key": last[0],
                "time": last[0],
                "open": float(last[1]),
                "high": float(last[2]),
                "low": float(last[3]),
                "close": float(last[4]),
                "volume": float(last[5]) if len(last) > 5 else 0.0,
            }

            print("🔁 Resuming last candle instead of creating new:", STATE.current_candle)

        else:
            # Fresh start case
            STATE.current_candle = {
                "key": key,
                "time": key,
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
                "volume": tick_volume,
            }
            print("FIRST CANDLE CREATED:", STATE.current_candle)
            write_market_data()
        return False
    if current["key"] == key:
        current["high"] = max(current["high"], ltp)
        current["low"] = min(current["low"], ltp)
        current["close"] = ltp
        current["volume"] += tick_volume
        STATE.current_candle = current
        return False
    closed.append([
        current["time"],
        current["open"],
        current["high"],
        current["low"],
        current["close"],
        current["volume"],
    ])
    STATE.latest_closed_candles = closed[-500:]
    print("CANDLE SAVED:", current)

    STATE.current_candle = {
        "key": key,
        "time": key,
        "open": ltp,
        "high": ltp,
        "low": ltp,
        "close": ltp,
        "volume": tick_volume,
    }
    return True


def _check_tick_ema_rejection(pos, index_tick):
    """Tick-level EMA21 rejection — exit if index price touches EMA21 wrong side."""
    try:
        ema21 = getattr(STATE, "ema50", None)  # slow EMA = EMA21
        if ema21 is None:
            return

        entry_time_str = pos.get("entry_time")
        if entry_time_str:
            try:
                if isinstance(entry_time_str, datetime):
                    entry_dt = entry_time_str
                else:
                    entry_dt = datetime.strptime(str(entry_time_str), "%Y-%m-%d %H:%M:%S")
                hold_seconds = (datetime.now() - entry_dt).total_seconds()
                if hold_seconds < 120:
                    return  # min 2 min hold before rejection
            except Exception:
                pass

        trend_side = pos.get("trend_side")

        if trend_side == "BUY" and index_tick <= float(ema21):
            print(
                f"⚡ TICK EMA REJECTION BUY | tick={index_tick:.2f} touched EMA21={float(ema21):.2f}"
            )
            from app.orders import _exit_position
            option_ltp = pos.get("current_ltp") or pos.get("entry_price")
            _exit_position("REVERSAL", float(option_ltp))

        elif trend_side == "SELL" and index_tick >= float(ema21):
            print(
                f"⚡ TICK EMA REJECTION SELL | tick={index_tick:.2f} touched EMA21={float(ema21):.2f}"
            )
            from app.orders import _exit_position
            option_ltp = pos.get("current_ltp") or pos.get("entry_price")
            _exit_position("REVERSAL", float(option_ltp))
    except Exception as e:
        print(f"⚠️ Tick EMA rejection error: {e}")


def _check_tick_giveback(pos, tick_price):
    """Tick-level swing exit — suppressed when trend is strong."""
    try:
        entry_price = float(pos.get("entry_price", 0))
        atr = getattr(STATE, "atr", None)
        if atr is None or atr <= 0:
            return

        ema9 = getattr(STATE, "ema20", None)
        ema21 = getattr(STATE, "ema50", None)
        trend_side = pos.get("trend_side", pos.get("side", "BUY"))
        is_bullish = trend_side == "BUY"

        if pos["side"] == "BUY":
            pos["highest_price"] = max(float(pos.get("highest_price", entry_price)), tick_price)
            recent_high = float(pos["highest_price"])
            move_from_entry = recent_high - entry_price

            # Check trend strength using trend_side, not pos["side"]
            if is_bullish:
                trend_strong = (
                    ema9 is not None and ema21 is not None
                    and float(ema9) > float(ema21)
                    and float(ema9) > float(ema21) + (atr * 0.15)
                )
            else:
                # PE trade: bearish trend strong when EMA9 < EMA21 and expanding
                trend_strong = (
                    ema9 is not None and ema21 is not None
                    and float(ema9) < float(ema21)
                    and float(ema9) < float(ema21) - (atr * 0.15)
                )

            if move_from_entry >= 30 and not trend_strong:
                giveback_pct = 0.12 if move_from_entry < atr else 0.15
                swing_exit = max(recent_high - (move_from_entry * giveback_pct), entry_price)

                if tick_price <= swing_exit:
                    print(
                        f"⚡ TICK SWING EXIT {trend_side} | high={recent_high:.2f} "
                        f"move={move_from_entry:.2f} pct={giveback_pct:.0%} "
                        f"exit={swing_exit:.2f} tick={tick_price:.2f}"
                    )
                    from app.orders import _exit_position
                    _exit_position("GIVEBACK", tick_price)
        else:
            # FUT SELL side only
            pos["lowest_price"] = min(float(pos.get("lowest_price", entry_price)), tick_price)
            recent_low = float(pos["lowest_price"])
            move_from_entry = entry_price - recent_low

            trend_strong = (
                ema9 is not None and ema21 is not None
                and trend_side == "SELL"
                and float(ema9) < float(ema21)
                and float(ema9) < float(ema21) - (atr * 0.15)
            )

            if move_from_entry >= 30 and not trend_strong:
                giveback_pct = 0.12 if move_from_entry < atr else 0.15
                swing_exit = min(recent_low + (move_from_entry * giveback_pct), entry_price)

                if tick_price >= swing_exit:
                    print(
                        f"⚡ TICK SWING EXIT SELL | low={recent_low:.2f} "
                        f"move={move_from_entry:.2f} pct={giveback_pct:.0%} "
                        f"exit={swing_exit:.2f} tick={tick_price:.2f}"
                    )
                    from app.orders import _exit_position
                    _exit_position("GIVEBACK", tick_price)
    except Exception as e:
        print(f"⚠️ Tick swing exit error: {e}")


def on_data(wsapp, message):
    if getattr(STATE, "shutting_down", False):
        print("WS Closed during shutdown")
        return

    if getattr(STATE, "reconnecting", False):
        return

    try:
        if isinstance(message, dict) and "last_traded_price" in message:
            tick_ltp = float(message["last_traded_price"]) / 100.0
            tick_token = str(message.get("token", ""))

            try:
                ltq = message.get("last_traded_quantity", 0) or 0
            except Exception:
                ltq = 0

            with STATE.lock:
                candle_closed = False

                # ✅ main subscribed instrument tick
                expected_token = str(DEBUG_TOKEN) if DEBUG_MODE else str(INDEX_TOKEN)

                if tick_token == expected_token:
                    STATE.ltp = tick_ltp
                    STATE.last_tick_time = datetime.now()

                    from app.files import write_tick_data
                    write_tick_data(STATE.ltp, STATE.last_tick_time, ltq)

                    candle_closed = _append_tick_to_local_candle(STATE.ltp, message)

                    if STATE.current_position:
                        STATE.current_position["spot_ltp"] = tick_ltp
                
                pos = STATE.current_position
                if pos and tick_token == str(pos.get("token", "")):
                    pos["current_ltp"] = tick_ltp

                    qty = pos.get("qty") or pos.get("remaining_qty") or "NA"

                    if pos["side"] == "BUY":
                        pos["mtm_points"] = tick_ltp - float(pos["entry_price"])
                    else:
                        pos["mtm_points"] = float(pos["entry_price"]) - tick_ltp

                    pos["mtm_pnl"] = pos["mtm_points"] * qty

                    # ─── TICK-LEVEL GIVEBACK CHECK ────────────────────────
                    _check_tick_giveback(pos, tick_ltp)

            # strategy only on index candle close
            if candle_closed:
                try:
                    from app.strategy import strategy_loop
                    strategy_loop()
                except Exception as strategy_error:
                    print("Strategy error:", strategy_error)
                # Persist market data AFTER strategy runs (don't delay entry)
                try:
                    write_market_data()
                except Exception:
                    pass

            push_dashboard_state_throttled(0.2)

    except Exception as e:
        print("Tick parse error:", e)

def on_open(wsapp):
    STATE.ws_connected = True
    print("WS Connected")

    if DEBUG_MODE and DEBUG_TOKEN:
        print("DEBUG MODE ON")
        print("Subscribing to debug token:", DEBUG_TOKEN)
        STATE.ws.subscribe(
            "stream_1",
            1,
            [{"exchangeType": DEBUG_EXCHANGE_TYPE, "tokens": [DEBUG_TOKEN]}]
        )
    else:
        token_list = [{"exchangeType": 1, "tokens": [INDEX_TOKEN]}]

        # ✅ also subscribe current option token if position exists
        pos = getattr(STATE, "current_position", None)
        if pos and pos.get("token"):
            print("Subscribing to option token:", pos["token"])
            token_list.append({"exchangeType": 2, "tokens": [str(pos["token"])]})  # NFO

        print("Subscribing to:", token_list)
        STATE.ws.subscribe("stream_1", 1, token_list)

    print("Subscription sent")

def on_error(wsapp, error):
    STATE.ws_connected = False
    print("WS Error:", error)
    write_state("WS_ERROR")

    try:
        wsapp.close()
    except Exception:
        pass


def on_close(wsapp):
    if getattr(STATE, "reconnecting", False):
        return

    STATE.reconnecting = True
    STATE.ws_connected = False

    print("WS Closed → Attempting reconnect...")
    write_state("WS_CLOSED")
    time_module.sleep(3)

    try:
        print("🔄 Re-login and reconnect WS...")
        login()
        start_websocket()
    except Exception as e:
        print("❌ Reconnect failed:", e)

    STATE.reconnecting = False


def start_websocket():
    STATE.ws = SmartWebSocketV2(STATE.auth_token, API_KEY, CLIENT_ID, STATE.feed_token)
    STATE.ws.on_open = on_open
    STATE.ws.on_data = on_data
    STATE.ws.on_error = on_error
    STATE.ws.on_close = on_close
    STATE.ws.connect()

def fetch_historical_candles(token, from_dt, to_dt, interval="ONE_MINUTE", exchange=None):
    try:
        print(f"📡 Fetching candles from API: {from_dt} → {to_dt} | exchange={exchange} | token={token}")

        params = {
            "exchange": exchange,
            "symboltoken": str(token),
            "interval": interval,
            "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
        }

        response = safe_api_call(lambda: STATE.obj.getCandleData(params))
        print("📦 Raw candle API response:", response)

        if not response or "data" not in response or not response["data"]:
            print("❌ No candle data received from API")
            return []

        candles = response["data"]

        formatted = []
        for c in candles:
            try:
                formatted.append([
                    c[0],
                    float(c[1]),
                    float(c[2]),
                    float(c[3]),
                    float(c[4]),
                    float(c[5]) if len(c) > 5 else 0.0,
                ])
            except Exception:
                continue

        print(f"✅ API returned {len(formatted)} candles")
        return formatted

    except Exception as e:
        print("❌ API candle fetch error:", e)
        return []    
from datetime import timedelta

def backfill_gap_after_restore(token, exchange, last_saved_candle_time):
    """
    Fill missing candles after restored CSV candles and before websocket start.
    Best for DEBUG/MCX where market continues after 15:30.
    """
    try:
        if not last_saved_candle_time:
            print("ℹ️ No last saved candle time -> skipping gap backfill")
            return []

        from_dt = last_saved_candle_time + timedelta(minutes=1)
        now_dt = datetime.now().replace(second=0, microsecond=0)

        # for debug crude, fetch till now
        if DEBUG_MODE:
            to_dt = now_dt
        else:
            # NSE/index normal session cap
            session_end = datetime.combine(date.today(), datetime.min.time()).replace(
                hour=15, minute=30
            )
            to_dt = min(now_dt, session_end)

        if from_dt > to_dt:
            print(f"ℹ️ No gap to backfill | from={from_dt} | to={to_dt}")
            return []

        print(f"🔧 Gap backfill after restore | token={token} | exchange={exchange} | from={from_dt} | to={to_dt}")

        candles = fetch_historical_candles(
            token=token,
            from_dt=from_dt,
            to_dt=to_dt,
            interval="ONE_MINUTE",
            exchange=exchange,
        )

        if not candles:
            print("ℹ️ Gap backfill returned no candles")
            return []

        print(f"✅ Gap backfill returned {len(candles)} candles")
        return candles

    except Exception as e:
        print(f"❌ Gap backfill failed: {e}")
        return []

def subscribe_open_position_token():
    try:
        pos = getattr(STATE, "current_position", None)
        if not pos or not pos.get("token") or not getattr(STATE, "ws", None):
            return

        instrument_token = str(pos["token"])
        exchange_type = 5 if str(pos.get("exchange", "")).upper() == "MCX" else 2

        print(f"Subscribing open position token: {instrument_token} | exchangeType={exchange_type}")

        STATE.ws.subscribe(
            "stream_pos",
            1,
            [{"exchangeType": exchange_type, "tokens": [instrument_token]}]
        )
    except Exception as e:
        print("⚠️ Option token subscribe failed:", e)
import threading
import time
from app.state import STATE
from app.orders import rebuild_position_after_restart
from app.broker import (
    login, 
    load_instruments,
    start_websocket,
    fetch_historical_candles,
    reconcile_runtime_with_broker,
)
from app.config import (
    INDEX_TOKEN,
    DEBUG_MODE,
    DEBUG_TOKEN,
    FAST_EMA_PERIOD,
    SLOW_EMA_PERIOD,
    STARTUP_PREWARM_ENABLED,
    STARTUP_PREWARM_CANDLES,
)

from datetime import datetime, timedelta
from app.files import (
    ensure_control_file, write_state, ensure_market_file,
    restore_runtime_state, restore_candles_from_market_data,
    detect_candle_gap, merge_backfill_candles
)
from app.logger_excel import init_excel
from app.utils import market_is_open




def _prewarm_history_if_needed():
   

    min_required = max(FAST_EMA_PERIOD, SLOW_EMA_PERIOD, STARTUP_PREWARM_CANDLES)
    current_count = len(getattr(STATE, "latest_closed_candles", []) or [])

    if not STARTUP_PREWARM_ENABLED or current_count >= min_required:
        return current_count

    now = datetime.now()

    # Full session boundaries
    session_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    session_end = now.replace(hour=23, minute=50, second=0, microsecond=0)

    # If no candles exist at all, fetch full session window
    if current_count == 0:
        from_dt = session_start
        to_dt = min(now.replace(second=0, microsecond=0), session_end)

        print(
            f"🧪 Full-session warm-up | token={DEBUG_TOKEN} | "
            f"from={from_dt.strftime('%Y-%m-%d %H:%M:%S')} | "
            f"to={to_dt.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if DEBUG_MODE:
            candles = fetch_historical_candles(DEBUG_TOKEN, from_dt, to_dt, exchange="MCX")
            if candles:
                merge_backfill_candles(candles)
                restored = restore_candles_from_market_data()
                print(f"✅ Full-session warm-up restored candles: {restored}")
                return restored
        else:
            candles = fetch_historical_candles(INDEX_TOKEN, from_dt, to_dt, exchange="NSE")
            if candles:
                merge_backfill_candles(candles)
                restored = restore_candles_from_market_data()
                print(f"✅ Full-session warm-up restored candles: {restored}")
                return restored
        print("❌ Full-session warm-up API returned no candles")
        return current_count

    # If some candles already exist, fetch only missing recent range
    to_dt = min(now.replace(second=0, microsecond=0), session_end)
    from_dt = to_dt - timedelta(minutes=min_required + 10)

    print(
        f"🧪 Warm-up request | token={DEBUG_TOKEN if DEBUG_MODE else INDEX_TOKEN} | "
        f"from={from_dt.strftime('%Y-%m-%d %H:%M:%S')} | "
        f"to={to_dt.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    candles = fetch_historical_candles(
        DEBUG_TOKEN if DEBUG_MODE else INDEX_TOKEN,
        from_dt,
        to_dt,
        exchange="MCX" if DEBUG_MODE else "NSE"
    )
    if candles:
        merge_backfill_candles(candles)
        restored = restore_candles_from_market_data()
        print(f"✅ Warm-up restored candles: {restored}")
        return restored

    print("❌ Warm-up API returned no candles")
    return current_count

def main():
    init_excel()
    ensure_control_file()
    ensure_market_file()
    restore_runtime_state()
    write_state("STARTING")
    

    login()
    load_instruments()

    # STEP 1: Restore existing candles
    restored = restore_candles_from_market_data()
    print(f"📊 Restored candles: {restored}")

   
    # STEP 2: Handle new day vs same-day gap
    from app.files import get_last_saved_candle_time, overwrite_market_data_with_candles

    last_time = get_last_saved_candle_time()
    now = datetime.now()

    # ✅ NEW DAY → FULL REBUILD
    if last_time and last_time.date() != now.date():
        print("🟡 New trading day detected")

        # Clear only today-specific runtime state, NOT candle history
        # Previous session candles are needed for EMA warm-up
        STATE.current_candle = None
        STATE.today_candles = []
        STATE.vwap_cum_price_volume = 0
        STATE.vwap_cum_volume = 0

        market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        to_time = now.replace(second=0, microsecond=0)
        # ✅ FIX: Skip rebuild before market open
        if now < market_start:
            print("⏳ Before market open → skipping full session rebuild")
        else:
            print("🔁 Rebuilding full session")
            
            candles = fetch_historical_candles(
                DEBUG_TOKEN if DEBUG_MODE else INDEX_TOKEN,
                market_start,
                to_time,
                exchange="MCX" if DEBUG_MODE else "NSE"
            )

            if candles:
                overwrite_market_data_with_candles(candles)

                restored = restore_candles_from_market_data()
                print(f"✅ Full session rebuild complete | Restored candles: {restored}")
            else:
                print("❌ Full session rebuild failed (no candles)")

       
        # ✅ SAME DAY → GAP FIX / DEBUG RESTORE TO NOW
    else:
        gap, from_dt, to_dt = detect_candle_gap()

        # 🔥 DEBUG MODE: for MCX crude, restore from last saved candle up to NOW
        if DEBUG_MODE and last_time:
            from_dt = last_time + timedelta(minutes=1)
            to_dt = now.replace(second=0, microsecond=0)

            if from_dt <= to_dt:
                print(
                    f"🔧 DEBUG gap backfill | token={DEBUG_TOKEN} | "
                    f"from={from_dt.strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"to={to_dt.strftime('%Y-%m-%d %H:%M:%S')}"
                )

                candles = fetch_historical_candles(
                    DEBUG_TOKEN,
                    from_dt,
                    to_dt,
                    exchange="MCX",
                )

                if candles:
                    merge_backfill_candles(candles)
                    restored = restore_candles_from_market_data()
                    
                else:
                    print("ℹ️ DEBUG gap backfill returned no candles")
            else:
                print("ℹ️ No DEBUG candle gap to repair")

        elif gap:
            print("🔧 Running API backfill...")

            candles = fetch_historical_candles(
                INDEX_TOKEN,
                from_dt,
                to_dt,
                exchange="NSE",
            )
            merge_backfill_candles(candles)

            restored = restore_candles_from_market_data()
            print(f"✅ Candle repair complete | Restored candles: {restored}")
    
    # STEP 3: NOW reconcile broker using restored history
    reconcile_runtime_with_broker()
    from app.orders import rebuild_position_after_restart
    rebuild_position_after_restart()
    
    if not market_is_open():
        print("⏳ Market is closed. Waiting for open...")
        write_state("MARKET_CLOSED")

        while True:
            if market_is_open():
                break
            print("⏳ Still waiting for market open...")
            time.sleep(30)  # check every 30 sec
            
    _prewarm_history_if_needed()

    print("✅ Market opened. Starting bot...")
   
    STATE.startup_cutoff_time = datetime.now().replace(second=0, microsecond=0)
    print(f"🚦 Signals allowed only after: {STATE.startup_cutoff_time}")

    ws_thread = threading.Thread(target=start_websocket, daemon=True)
    ws_thread.start()

    for _ in range(15):
        
        if STATE.ws_connected:
            break
        time.sleep(1)

    while True:
        time.sleep(1)

        # Auto-stop at 15:30 for NSE (BANKNIFTY) mode
        if not DEBUG_MODE:
            now = datetime.now()
            if now.hour == 15 and now.minute >= 30:
                print("🛑 Market closed (15:30) — auto-stopping bot")
                STATE.shutting_down = True
                write_state("MARKET_CLOSED")
                break


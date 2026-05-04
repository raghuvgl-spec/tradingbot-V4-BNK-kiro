import os
import sys
from datetime import datetime
import time
from app.state import STATE

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from app.files import (
    restore_candles_from_market_data,
    restore_runtime_state,
    ensure_market_file,
)
from app.bot import main
from app import trade_db

print("BOT STARTED")


def startup_restore():
    print("=" * 50)
    print("🔁 BOT STARTING - RESTORE MODE")

    # Initialize Trade Intelligence database early, before any trading logic
    try:
        trade_db.init_db()
        print("✅ Trade Intelligence DB initialized")
    except Exception as e:
        print(f"⚠️ Trade Intelligence DB init failed: {e}")

    ensure_market_file()
    restored = restore_candles_from_market_data()
    restore_runtime_state()

    if restored > 0:
        print(f"✅ Restored {restored} candles from history")
    else:
        print("⚠️ No previous candles found (fresh start)")

    print("=" * 50)


log_dir = os.path.join(os.getcwd(), "DailyTerminallogs")
os.makedirs(log_dir, exist_ok=True)

log_filename = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".log"
log_path = os.path.join(log_dir, log_filename)


class Tee:
    def __init__(self, *files):
        self.files = files

    def write(self, obj):
        for f in self.files:
            f.write(obj)
            f.flush()

    def flush(self):
        for f in self.files:
            f.flush()


log_file = open(log_path, "a", encoding="utf-8")
sys.stdout = Tee(sys.stdout, log_file)
sys.stderr = Tee(sys.stderr, log_file)
print(f"LOG FILE: {log_path}")


if __name__ == "__main__":
    try:
        # Initialize Trade Intelligence database before bot starts
        try:
            trade_db.init_db()
            print("✅ Trade Intelligence DB initialized")
        except Exception as e:
            print(f"⚠️ Trade Intelligence DB init failed: {e}")

        print("▶ Starting bot...")
        result = main()
        print(f"✅ main() returned: {result}")

    except KeyboardInterrupt:
        STATE.shutting_down = True
        print("\n🛑 Shutdown requested by user...")
        try:
            if getattr(STATE, "ws", None) is not None:
                STATE.ws.close_connection()
        except Exception:
            pass
        print("✅ Bot stopped cleanly.")

    except Exception as e:
        import traceback
        print("FATAL ERROR:", e)
        traceback.print_exc()

    finally:
        time.sleep(0.5)
        print("🛑 BOT PROCESS ENDED")
        try:
            input("Press Enter to close...")
        except EOFError:
            pass
        log_file.close()
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PID_FILE = BASE_DIR / "data" / "bot_pid.json"
RUN_BOT_FILE = BASE_DIR / "run_bot.py"


def _is_process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def get_saved_pid():
    if not PID_FILE.exists():
        return None
    try:
        data = json.loads(PID_FILE.read_text())
        return data.get("pid")
    except Exception:
        return None


def save_pid(pid: int):
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps({"pid": pid}, indent=2))


def clear_pid():
    if PID_FILE.exists():
        PID_FILE.unlink()


def start_bot():
    existing_pid = get_saved_pid()
    if existing_pid and _is_process_running(existing_pid):
        return {"ok": False, "message": f"Bot already running with PID {existing_pid}"}

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    process = subprocess.Popen(
        [sys.executable, str(RUN_BOT_FILE)],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )

    save_pid(process.pid)
    return {"ok": True, "message": f"Bot started with PID {process.pid}"}


def stop_bot():
    pid = get_saved_pid()
    if not pid:
        return {"ok": False, "message": "No saved bot PID found"}

    if not _is_process_running(pid):
        clear_pid()
        return {"ok": False, "message": "Saved PID not running; cleaned PID file"}

    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            os.kill(pid, signal.SIGTERM)

        time.sleep(1)
        clear_pid()
        return {"ok": True, "message": f"Bot stopped (PID {pid})"}
    except Exception as e:
        return {"ok": False, "message": f"Stop failed: {e}"}


def restart_bot():
    stop_bot()
    time.sleep(1)
    return start_bot()


if __name__ == "__main__":
    action = sys.argv[1].lower() if len(sys.argv) > 1 else ""

    if action == "start":
        print(start_bot())
    elif action == "stop":
        print(stop_bot())
    elif action == "restart":
        print(restart_bot())
    else:
        print({"ok": False, "message": "Use: start | stop | restart"})
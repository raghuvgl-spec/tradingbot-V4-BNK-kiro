import time
from datetime import datetime
from app.config import MARKET_OPEN, MARKET_CLOSE, START_TIME, END_TIME

def safe_api_call(func, retries=2, delay=2):
    for attempt in range(retries):
        try:
            return func()
        except Exception as e:
            print(f"API retry {attempt + 1}/{retries}: {e}")
            time.sleep(delay)
    return None

def market_is_open():
    now = datetime.now().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE

def trading_window_open():
    now = datetime.now().time()
    return START_TIME <= now <= END_TIME

def candle_key_now():
    now = datetime.now()
    return (now.date(), now.hour, now.minute // 5)
from datetime import datetime

def _serialize_for_json(value):
    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, dict):
        return {k: _serialize_for_json(v) for k, v in value.items()}

    if isinstance(value, list):
        return [_serialize_for_json(v) for v in value]

    return value


def _deserialize_possible_datetime(value):
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return value
    return value


def _deep_deserialize(data):
    if isinstance(data, dict):
        return {k: _deep_deserialize(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_deep_deserialize(v) for v in data]
    return _deserialize_possible_datetime(data)
"""
1H high/low sweep alert bot.

Strategy (simplified):
  1. Track the high/low of the current (most recently CLOSED) 1H candle per pair.
  2. Watch 5min candles. The moment a 5min candle's wick OR body touches or
     crosses the 1H high or low, fire a Telegram alert. It does not matter
     whether the candle closes back inside the range or beyond it.
  3. Each direction (high / low) only alerts ONCE per 1H level, so you don't
     get spammed every 5 minutes while price chops around the level. It can
     alert again once a new 1H candle forms with a fresh high/low.

State is persisted to state.json between runs (this script is meant to be
invoked every 5 minutes by a scheduler, e.g. GitHub Actions).
"""

import os
import json
import sys
from datetime import datetime, timezone, timedelta
import requests

IST = timezone(timedelta(hours=5, minutes=30))


def to_ist(utc_time_str):
    """Convert a 'YYYY-MM-DD HH:MM:SS' UTC string (as returned by Twelve Data)
    to a display string in IST."""
    dt = datetime.strptime(utc_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")


TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Twelve Data symbol format
PAIRS = {
    "XAUUSD": "XAU/USD",
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
}

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
TD_BASE = "https://api.twelvedata.com/time_series"


def td_get(symbols, interval, outputsize):
    """Batch-fetch time series for multiple symbols in one API call."""
    resp = requests.get(
        TD_BASE,
        params={
            "symbol": ",".join(symbols),
            "interval": interval,
            "outputsize": outputsize,
            "apikey": TWELVEDATA_API_KEY,
            "timezone": "UTC",
            "order": "ASC",
        },
        timeout=20,
    )
    data = resp.json()

    # Single-symbol responses aren't nested under the symbol key; normalize.
    if len(symbols) == 1:
        data = {symbols[0]: data}

    out = {}
    for sym in symbols:
        entry = data.get(sym, {})
        values = entry.get("values")
        if not values:
            print(f"WARNING: no data for {sym}: {entry.get('message', entry)}", file=sys.stderr)
            out[sym] = []
            continue
        candles = []
        for v in values:
            candles.append(
                {
                    "time": v["datetime"],
                    "open": float(v["open"]),
                    "high": float(v["high"]),
                    "low": float(v["low"]),
                    "close": float(v["close"]),
                }
            )
        out[sym] = candles
    return out


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"Telegram send failed: {resp.text}", file=sys.stderr)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def default_pair_state():
    return {
        "1h_time": None,
        "1h_high": None,
        "1h_low": None,
        "last_5m_time": None,  # last 5min candle timestamp we've processed
        "high_alerted": False,  # already alerted for a high-sweep this 1H level?
        "low_alerted": False,   # already alerted for a low-sweep this 1H level?
    }


def process_pair(display_name, td_symbol, state):
    ps = state.setdefault(display_name, default_pair_state())

    # --- 1H data: only need the last couple of closed candles ---
    h1 = td_get([td_symbol], "1h", 3)[td_symbol]
    if len(h1) < 2:
        return
    last_closed_1h = h1[-2]  # most recent fully closed 1H candle (last item may be forming)

    if ps["1h_time"] != last_closed_1h["time"]:
        # New 1H candle closed -> reset level and re-arm both alert flags
        ps["1h_time"] = last_closed_1h["time"]
        ps["1h_high"] = last_closed_1h["high"]
        ps["1h_low"] = last_closed_1h["low"]
        ps["high_alerted"] = False
        ps["low_alerted"] = False
        print(f"{display_name}: new 1H level set high={ps['1h_high']} low={ps['1h_low']} ({to_ist(ps['1h_time'])})")

    # --- 5min data ---
    m5 = td_get([td_symbol], "5min", 10)[td_symbol]
    if len(m5) < 2:
        return
    closed_5m = m5[:-1]  # drop the still-forming candle

    for candle in closed_5m:
        if ps["last_5m_time"] and candle["time"] <= ps["last_5m_time"]:
            continue  # already processed
        ps["last_5m_time"] = candle["time"]

        if not ps["high_alerted"] and candle["high"] >= ps["1h_high"]:
            msg = (
                f"<b>{display_name} - 1H HIGH swept</b>\n"
                f"1H high: {ps['1h_high']}\n"
                f"5min candle: O {candle['open']} H {candle['high']} L {candle['low']} C {candle['close']}\n"
                f"Time: {to_ist(candle['time'])}"
            )
            send_telegram(msg)
            ps["high_alerted"] = True
            print(f"{display_name}: HIGH swept at {candle['time']}")

        if not ps["low_alerted"] and candle["low"] <= ps["1h_low"]:
            msg = (
                f"<b>{display_name} - 1H LOW swept</b>\n"
                f"1H low: {ps['1h_low']}\n"
                f"5min candle: O {candle['open']} H {candle['high']} L {candle['low']} C {candle['close']}\n"
                f"Time: {to_ist(candle['time'])}"
            )
            send_telegram(msg)
            ps["low_alerted"] = True
            print(f"{display_name}: LOW swept at {candle['time']}")


def main():
    state = load_state()
    for display_name, td_symbol in PAIRS.items():
        try:
            process_pair(display_name, td_symbol, state)
        except Exception as e:
            print(f"ERROR processing {display_name}: {e}", file=sys.stderr)
    save_state(state)


if __name__ == "__main__":
    main()

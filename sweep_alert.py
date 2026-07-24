"""
1H sweep + 5min confirmation alert bot.

Strategy:
  1. Track the high/low of the current (most recently CLOSED) 1H candle per pair.
  2. Watch 5min candles. A "sweep" happens when a 5min candle's wick trades
     beyond the 1H high (bearish sweep) or 1H low (bullish sweep).
     - It does NOT matter whether that 5min candle closes back inside or not.
  3. After a sweep, watch up to CONFIRM_WINDOW subsequent 5min candles.
     Confirmation = a candle's BODY CLOSES beyond the sweep candle's own
     high (for a low-sweep / bullish setup) or low (for a high-sweep /
     bearish setup). It does not need to travel far past it.
  4. On confirmation -> send a Telegram alert. No entry/mitigation tracking.
  5. If no confirmation within CONFIRM_WINDOW candles, the sweep is invalidated
     and we go back to watching for a fresh sweep of the same 1H level.
  6. When a new 1H candle closes, the 1H high/low resets and all sweep state
     for that pair resets with it.

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

CONFIRM_WINDOW = 5  # max number of 5min candles to wait for confirmation
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
        "sweep": None,  # {"direction": "high"/"low", "sweep_high":.., "sweep_low":.., "candles_waited": n}
    }


def process_pair(display_name, td_symbol, state):
    ps = state.setdefault(display_name, default_pair_state())

    # --- 1H data: only need the last couple of closed candles ---
    h1 = td_get([td_symbol], "1h", 3)[td_symbol]
    if len(h1) < 2:
        return
    last_closed_1h = h1[-2]  # most recent fully closed 1H candle (last item may be forming)

    if ps["1h_time"] != last_closed_1h["time"]:
        # New 1H candle closed -> reset level and invalidate any pending sweep
        ps["1h_time"] = last_closed_1h["time"]
        ps["1h_high"] = last_closed_1h["high"]
        ps["1h_low"] = last_closed_1h["low"]
        ps["sweep"] = None
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

        sweep = ps["sweep"]

        if sweep is None:
            # Look for a fresh sweep of the 1H high or low
            if candle["high"] > ps["1h_high"]:
                ps["sweep"] = {
                    "direction": "high",  # swept the high -> bearish setup
                    "sweep_high": candle["high"],
                    "sweep_low": candle["low"],
                    "sweep_time": candle["time"],
                    "candles_waited": 0,
                }
                print(f"{display_name}: 1H HIGH swept at {candle['time']}")
            elif candle["low"] < ps["1h_low"]:
                ps["sweep"] = {
                    "direction": "low",  # swept the low -> bullish setup
                    "sweep_high": candle["high"],
                    "sweep_low": candle["low"],
                    "sweep_time": candle["time"],
                    "candles_waited": 0,
                }
                print(f"{display_name}: 1H LOW swept at {candle['time']}")
        else:
            sweep["candles_waited"] += 1

            confirmed = False
            if sweep["direction"] == "low" and candle["close"] > sweep["sweep_high"]:
                confirmed = True
                setup = "BULLISH"
            elif sweep["direction"] == "high" and candle["close"] < sweep["sweep_low"]:
                confirmed = True
                setup = "BEARISH"

            if confirmed:
                msg = (
                    f"<b>{display_name} - {setup} setup confirmed</b>\n"
                    f"1H level swept: {sweep['direction']} ({to_ist(sweep['sweep_time'])})\n"
                    f"Sweep candle range: {sweep['sweep_low']} - {sweep['sweep_high']}\n"
                    f"Confirmation candle close: {candle['close']} at {to_ist(candle['time'])}\n"
                    f"Watch for entry on mitigation of the sweep candle."
                )
                send_telegram(msg)
                print(f"{display_name}: CONFIRMED {setup}")
                ps["sweep"] = None  # reset, ready to watch for the next sweep
            elif sweep["candles_waited"] >= CONFIRM_WINDOW:
                print(f"{display_name}: sweep invalidated (no confirmation within {CONFIRM_WINDOW} candles)")
                ps["sweep"] = None


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

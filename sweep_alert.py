"""
Rolling multi-hour sweep alert bot.

Strategy:
  1. Keep a rolling window of the last MAX_HOURS_TRACKED completed 1H candles
     per pair (default 4). Each 1H high/low is computed ourselves from 5min
     candles (wall-clock aligned to :00-:55), not from a provider's separate
     1h endpoint (which can be misaligned).
  2. Watch 5min candles. The moment a 5min candle's wick OR body touches or
     crosses the high or low of ANY untouched candle in that rolling window,
     fire a Telegram alert naming exactly which hour was swept.
  3. Each direction (high / low) of each tracked hour only alerts ONCE. Once
     an hour ages out of the window (a newer hour pushes it out), it's
     dropped entirely, swept or not.

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
    """Convert a 'YYYY-MM-DD HH:MM:SS' UTC string to a display string in IST."""
    dt = datetime.strptime(utc_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def hour_range_ist(utc_time_str):
    """e.g. '14:00-15:00 IST' for the hour starting at utc_time_str."""
    dt = datetime.strptime(utc_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    start = dt.astimezone(IST)
    end = start + timedelta(hours=1)
    return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')} IST ({start.strftime('%Y-%m-%d')})"


TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Twelve Data symbol format
PAIRS = {
    "XAUUSD": "XAU/USD",
    "NAS100": "QQQ",
}

MAX_HOURS_TRACKED = 3  # how many recent 1H candles to keep watching (set to 3 if you prefer)
MIN_CANDLES_FOR_TRUSTED_HOUR = 10  # allow for minor provider data gaps
# Buffer big enough to always contain MAX_HOURS_TRACKED full hours regardless
# of where in the current hour the script happens to run.
FIVE_MIN_OUTPUTSIZE = (MAX_HOURS_TRACKED + 2) * 12

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
TD_BASE = "https://api.twelvedata.com/time_series"


def td_get(symbols, interval, outputsize):
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
        "hours": [],  # list of {time, high, low, high_alerted, low_alerted}, oldest first
        "last_5m_time": None,
    }


def closed_candles(candles, interval_minutes):
    now = datetime.now(timezone.utc)
    out = []
    for c in candles:
        open_time = datetime.strptime(c["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if open_time + timedelta(minutes=interval_minutes) <= now:
            out.append(c)
    return out


def hour_bucket(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def process_pair(display_name, td_symbol, state):
    ps = state.setdefault(display_name, default_pair_state())
    ps.setdefault("hours", [])
    ps.setdefault("last_5m_time", None)

    m5_raw = td_get([td_symbol], "5min", FIVE_MIN_OUTPUTSIZE)[td_symbol]
    closed_5m = closed_candles(m5_raw, 5)
    if not closed_5m:
        return

    now = datetime.now(timezone.utc)

    buckets = {}
    for c in closed_5m:
        open_time = datetime.strptime(c["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        b = hour_bucket(open_time)
        buckets.setdefault(b, []).append(c)

    complete_buckets = sorted(
        b for b in buckets
        if now >= b + timedelta(hours=1) and len(buckets[b]) >= MIN_CANDLES_FOR_TRUSTED_HOUR
    )

    known_times = {h["time"] for h in ps["hours"]}
    for b in complete_buckets:
        bucket_key = b.strftime("%Y-%m-%d %H:%M:%S")
        if bucket_key in known_times:
            continue
        candles_in_hour = buckets[b]
        ps["hours"].append(
            {
                "time": bucket_key,
                "high": max(c["high"] for c in candles_in_hour),
                "low": min(c["low"] for c in candles_in_hour),
                "high_alerted": False,
                "low_alerted": False,
            }
        )
        print(f"{display_name}: tracking new 1H candle {hour_range_ist(bucket_key)} "
              f"high={ps['hours'][-1]['high']} low={ps['hours'][-1]['low']} (from {len(candles_in_hour)} 5min candles)")

    ps["hours"].sort(key=lambda h: h["time"])
    if len(ps["hours"]) > MAX_HOURS_TRACKED:
        dropped = ps["hours"][:-MAX_HOURS_TRACKED]
        for d in dropped:
            print(f"{display_name}: {hour_range_ist(d['time'])} aged out of the tracking window")
        ps["hours"] = ps["hours"][-MAX_HOURS_TRACKED:]

    if not ps["hours"]:
        return

    for candle in closed_5m:
        if ps["last_5m_time"] and candle["time"] <= ps["last_5m_time"]:
            continue
        ps["last_5m_time"] = candle["time"]

        for hour_entry in ps["hours"]:
            if not hour_entry["high_alerted"] and candle["high"] >= hour_entry["high"]:
                msg = (
                    f"<b>{display_name} - 1H HIGH swept</b>\n"
                    f"Swept candle: {hour_range_ist(hour_entry['time'])}\n"
                    f"Level: {hour_entry['high']}\n"
                    f"5min candle: O {candle['open']} H {candle['high']} L {candle['low']} C {candle['close']}\n"
                    f"Time: {to_ist(candle['time'])}"
                )
                send_telegram(msg)
                hour_entry["high_alerted"] = True
                print(f"{display_name}: HIGH of {hour_range_ist(hour_entry['time'])} swept at {candle['time']}")

            if not hour_entry["low_alerted"] and candle["low"] <= hour_entry["low"]:
                msg = (
                    f"<b>{display_name} - 1H LOW swept</b>\n"
                    f"Swept candle: {hour_range_ist(hour_entry['time'])}\n"
                    f"Level: {hour_entry['low']}\n"
                    f"5min candle: O {candle['open']} H {candle['high']} L {candle['low']} C {candle['close']}\n"
                    f"Time: {to_ist(candle['time'])}"
                )
                send_telegram(msg)
                hour_entry["low_alerted"] = True
                print(f"{display_name}: LOW of {hour_range_ist(hour_entry['time'])} swept at {candle['time']}")


def in_quiet_hours():
    now_ist = datetime.now(timezone.utc).astimezone(IST)
    hour = now_ist.hour
    return hour >= 22 or hour < 6


def main():
    if in_quiet_hours():
        print("Quiet hours (10 PM - 6 AM IST) -- skipping this run, no API calls made.")
        return

    state = load_state()

    for display_name, td_symbol in PAIRS.items():
        try:
            process_pair(display_name, td_symbol, state)
        except Exception as e:
            print(f"ERROR processing {display_name}: {e}", file=sys.stderr)
    save_state(state)


if __name__ == "__main__":
    main()

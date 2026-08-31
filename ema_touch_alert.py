"""
15min 7EMA touch/cross alert bot.

Strategy:
  1. Fetch closed 15min candles plus the 7EMA and 30EMA values (computed
     server-side by Twelve Data, same as TradingView's EMA) for the same
     timestamps.
  2. For the most recently CLOSED 15min candle, check whether it touched
     or crossed the 7EMA:
       - touch  = the candle's high/low range overlaps the 7EMA value
       - cross  = the candle's open and close sit on opposite sides of
                  the 7EMA value
     Either condition qualifies.
  3. Apply a 30EMA trend filter:
       - bullish candle (close > open) only alerts if close > 30EMA
       - bearish candle (close < open) only alerts if close < 30EMA
     This keeps the alert to "with-trend" touches only. Candles against
     the 30EMA trend are skipped.
  4. Each closed 15min candle is only ever evaluated once (tracked via
     last_15m_time in state), so re-running the script doesn't re-alert.

Deployment note: this is meant to be triggered by the same kind of
scheduler as sweep_alert.py (e.g. GitHub Actions cron). Since 15min
candles only finalize once every 15 minutes, and most schedulers can't
go tighter than 1-minute cron granularity, run this every 1-2 minutes
for the alert to fire as close as possible to candle close -- true
"next second" timing isn't achievable with a polling cron job. If you
need sub-second reaction time you'd need a websocket/streaming feed
instead of polling the REST API.

State is persisted to ema_state.json between runs (separate from
sweep_alert.py's state.json so the two bots don't collide).
"""

import os
import json
import sys
from datetime import datetime, timezone, timedelta
import requests

IST = timezone(timedelta(hours=5, minutes=30))


def to_ist(utc_time_str):
    dt = datetime.strptime(utc_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")


TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Twelve Data symbol format -- keep in sync with sweep_alert.py's PAIRS
PAIRS = {
    "XAUUSD": "XAU/USD",
}

INTERVAL = "15min"
EMA_FAST = 7
EMA_SLOW = 30

# Small outputsize -- we only need the last couple of closed candles/EMA
# points, Twelve Data computes the EMA server-side using its own longer
# lookback internally.
OUTPUTSIZE = 10

STATE_FILE = os.path.join(os.path.dirname(__file__), "ema_state.json")
TD_BASE = "https://api.twelvedata.com"


def td_get_candles(symbol, interval, outputsize):
    resp = requests.get(
        f"{TD_BASE}/time_series",
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": TWELVEDATA_API_KEY,
            "timezone": "UTC",
            "order": "ASC",
        },
        timeout=20,
    )
    data = resp.json()
    values = data.get("values")
    if not values:
        print(f"WARNING: no candle data for {symbol}: {data.get('message', data)}", file=sys.stderr)
        return []
    return [
        {
            "time": v["datetime"],
            "open": float(v["open"]),
            "high": float(v["high"]),
            "low": float(v["low"]),
            "close": float(v["close"]),
        }
        for v in values
    ]


def td_get_ema(symbol, interval, time_period, outputsize):
    resp = requests.get(
        f"{TD_BASE}/ema",
        params={
            "symbol": symbol,
            "interval": interval,
            "time_period": time_period,
            "outputsize": outputsize,
            "apikey": TWELVEDATA_API_KEY,
            "timezone": "UTC",
            "order": "ASC",
        },
        timeout=20,
    )
    data = resp.json()
    values = data.get("values")
    if not values:
        print(f"WARNING: no EMA{time_period} data for {symbol}: {data.get('message', data)}", file=sys.stderr)
        return {}
    return {v["datetime"]: float(v["ema"]) for v in values}


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
    return {"last_15m_time": None}


def closed_candles(candles, interval_minutes):
    now = datetime.now(timezone.utc)
    out = []
    for c in candles:
        open_time = datetime.strptime(c["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if open_time + timedelta(minutes=interval_minutes) <= now:
            out.append(c)
    return out


def touched_or_crossed(candle, ema_value):
    touch = candle["low"] <= ema_value <= candle["high"]
    cross = (candle["open"] - ema_value) * (candle["close"] - ema_value) < 0
    return touch or cross


def process_pair(display_name, td_symbol, state):
    ps = state.setdefault(display_name, default_pair_state())

    candles = td_get_candles(td_symbol, INTERVAL, OUTPUTSIZE)
    closed = closed_candles(candles, 15)
    if not closed:
        return

    ema7_map = td_get_ema(td_symbol, INTERVAL, EMA_FAST, OUTPUTSIZE)
    ema30_map = td_get_ema(td_symbol, INTERVAL, EMA_SLOW, OUTPUTSIZE)
    if not ema7_map or not ema30_map:
        return

    for candle in closed:
        if ps["last_15m_time"] and candle["time"] <= ps["last_15m_time"]:
            continue  # already processed
        ps["last_15m_time"] = candle["time"]

        ema7 = ema7_map.get(candle["time"])
        ema30 = ema30_map.get(candle["time"])
        if ema7 is None or ema30 is None:
            print(f"{display_name}: missing EMA for {candle['time']}, skipping", file=sys.stderr)
            continue

        is_bullish = candle["close"] > candle["open"]
        is_bearish = candle["close"] < candle["open"]

        if not touched_or_crossed(candle, ema7):
            continue

        # 30EMA trend filter: only alert with-trend touches
        if is_bullish and candle["close"] <= ema30:
            print(f"{display_name}: bullish 7EMA touch at {candle['time']} skipped (below 30EMA)")
            continue
        if is_bearish and candle["close"] >= ema30:
            print(f"{display_name}: bearish 7EMA touch at {candle['time']} skipped (above 30EMA)")
            continue
        if not is_bullish and not is_bearish:
            continue  # doji / flat candle, no clear direction

        direction = "BULLISH" if is_bullish else "BEARISH"
        msg = (
            f"<b>{display_name} - 15min {direction} candle touched/crossed 7EMA</b>\n"
            f"7EMA: {ema7:.3f}  |  30EMA: {ema30:.3f}\n"
            f"Candle: O {candle['open']} H {candle['high']} L {candle['low']} C {candle['close']}\n"
            f"Time: {to_ist(candle['time'])}"
        )
        send_telegram(msg)
        print(f"{display_name}: {direction} 7EMA touch/cross alert sent for {candle['time']}")


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

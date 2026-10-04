import os
import json
import time
import requests
from datetime import datetime, timezone

# ============================================================
# OANDA PRACTICE - LIVE M1 CANDLE BUILDER
# READ ONLY - NO ORDERS
# ============================================================

TOKEN = os.getenv("OANDA_API_TOKEN", "").strip()

REST_URL = "https://api-fxpractice.oanda.com"
STREAM_URL = "https://stream-fxpractice.oanda.com"

INSTRUMENTS = [
    "EUR_USD",
    "GBP_USD",
    "USD_JPY",
    "EUR_NZD",
]

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
}


# ------------------------------------------------------------
# GET ACCOUNT
# ------------------------------------------------------------

def get_account_id():

    response = requests.get(
        f"{REST_URL}/v3/accounts",
        headers=HEADERS,
        timeout=20,
    )

    response.raise_for_status()

    data = response.json()

    accounts = data.get("accounts", [])

    if not accounts:
        raise RuntimeError("No OANDA accounts returned.")

    return accounts[0]["id"]


# ------------------------------------------------------------
# M1 CANDLE STORAGE
# ------------------------------------------------------------

candles = {}

for instrument in INSTRUMENTS:
    candles[instrument] = None


# ------------------------------------------------------------
# START NEW CANDLE
# ------------------------------------------------------------

def start_candle(instrument, minute_time, price):

    return {
        "instrument": instrument,
        "time": minute_time,
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "ticks": 1,
    }


# ------------------------------------------------------------
# UPDATE CANDLE
# ------------------------------------------------------------

def update_candle(instrument, timestamp, price):

    global candles

    minute_time = timestamp.replace(
        second=0,
        microsecond=0,
    )

    current = candles[instrument]

    # First tick
    if current is None:

        candles[instrument] = start_candle(
            instrument,
            minute_time,
            price,
        )

        return None

    # Same minute
    if current["time"] == minute_time:

        if price > current["high"]:
            current["high"] = price

        if price < current["low"]:
            current["low"] = price

        current["close"] = price
        current["ticks"] += 1

        return None

    # New minute -> close previous candle
    closed = current.copy()

    candles[instrument] = start_candle(
        instrument,
        minute_time,
        price,
    )

    return closed


# ------------------------------------------------------------
# PRINT CLOSED M1
# ------------------------------------------------------------

def print_candle(candle):

    print(
        f"[M1 CLOSED] "
        f"{candle['instrument']} | "
        f"{candle['time'].isoformat()} | "
        f"O={candle['open']} | "
        f"H={candle['high']} | "
        f"L={candle['low']} | "
        f"C={candle['close']} | "
        f"TICKS={candle['ticks']}",
        flush=True,
    )


# ------------------------------------------------------------
# PRICE STREAM
# ------------------------------------------------------------

def price_stream(account_id):

    instruments = ",".join(INSTRUMENTS)

    url = (
        f"{STREAM_URL}/v3/accounts/"
        f"{account_id}/pricing/stream"
        f"?instruments={instruments}"
    )

    stream_headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/octet-stream",
    }

    print("=" * 75)
    print("OANDA LIVE M1 CANDLE BUILDER")
    print("=" * 75)
    print("Account     :", account_id)
    print("Instruments :", ", ".join(INSTRUMENTS))
    print("Environment : PRACTICE")
    print("Timeframe   : M1")
    print("Mode        : READ ONLY")
    print("Orders      : DISABLED")
    print("=" * 75)

    with requests.get(
        url,
        headers=stream_headers,
        stream=True,
        timeout=(20, 90),
    ) as response:

        print(
            "STREAM HTTP STATUS:",
            response.status_code,
            flush=True,
        )

        response.raise_for_status()

        print("=" * 75)
        print("PRICE STREAM CONNECTED")
        print("M1 CANDLE BUILDING STARTED")
        print("=" * 75)

        for line in response.iter_lines():

            if not line:
                continue

            try:
                data = json.loads(
                    line.decode("utf-8")
                )
            except Exception:
                continue

            data_type = data.get("type")

            # ------------------------------------------------
            # HEARTBEAT
            # ------------------------------------------------

            if data_type == "HEARTBEAT":

                print(
                    "[HEARTBEAT]",
                    data.get("time"),
                    flush=True,
                )

                continue

            # ------------------------------------------------
            # PRICE
            # ------------------------------------------------

            instrument = data.get("instrument")

            if not instrument:
                continue

            bids = data.get("bids", [])
            asks = data.get("asks", [])

            if not bids or not asks:
                continue

            try:
                bid = float(bids[0]["price"])
                ask = float(asks[0]["price"])
            except Exception:
                continue

            # Mid-price
            mid = (bid + ask) / 2.0

            # OANDA timestamp
            raw_time = data.get("time")

            if not raw_time:
                continue

            try:
                timestamp = datetime.fromisoformat(
                    raw_time.replace("Z", "+00:00")
                )
            except Exception:
                continue

            # Build M1
            closed = update_candle(
                instrument,
                timestamp,
                mid,
            )

            # Print completed candle
            if closed is not None:
                print_candle(closed)


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():

    if not TOKEN:

        print(
            "ERROR: OANDA_API_TOKEN is missing.",
            flush=True,
        )

        while True:
            time.sleep(60)

    print("=" * 75)
    print("OANDA PAPER BOT - M1 TEST")
    print("=" * 75)
    print("Environment : PRACTICE")
    print("Mode        : READ ONLY")
    print("Orders      : DISABLED")
    print("=" * 75)

    while True:

        try:

            account_id = get_account_id()

            print(
                "Authorized account:",
                account_id,
                flush=True,
            )

            price_stream(account_id)

        except Exception as e:

            print("=" * 75)
            print("STREAM ERROR")
            print("=" * 75)
            print(
                type(e).__name__,
                ":",
                str(e),
                flush=True,
            )
            print(
                "Reconnecting in 10 seconds...",
                flush=True,
            )
            print("=" * 75)

            time.sleep(10)


if __name__ == "__main__":
    main()

import os
import json
import time
import urllib.request

# ============================================================
# OANDA PRACTICE - LIVE PRICE STREAM TEST
# READ ONLY - NO ORDERS
# ============================================================

TOKEN = os.getenv("OANDA_API_TOKEN", "").strip()

ACCOUNT_ID = os.getenv(
    "OANDA_ACCOUNT_ID",
    "101-004-40624222-001"
).strip()

STREAM_URL = "https://stream-fxpractice.oanda.com"

# Start with the main Forex pairs we will use
INSTRUMENTS = [
    "EUR_USD",
    "GBP_USD",
    "USD_JPY",
    "EUR_NZD",
]

def price_stream():

    instruments = ",".join(INSTRUMENTS)

    url = (
        f"{STREAM_URL}/v3/accounts/"
        f"{ACCOUNT_ID}/pricing/stream"
        f"?instruments={instruments}"
        f"&snapshot=true"
    )

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/octet-stream",
        },
        method="GET",
    )

    print("=" * 70)
    print("OANDA LIVE PRICE STREAM")
    print("=" * 70)
    print(f"Account      : {ACCOUNT_ID}")
    print(f"Instruments  : {', '.join(INSTRUMENTS)}")
    print("Environment  : PRACTICE")
    print("Mode         : READ ONLY")
    print("Orders       : DISABLED")
    print("=" * 70)

    with urllib.request.urlopen(request, timeout=60) as response:

        print("PRICE STREAM CONNECTED")
        print("-" * 70)

        while True:

            line = response.readline()

            if not line:
                print("Stream closed by OANDA.")
                break

            line = line.decode("utf-8").strip()

            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Heartbeat
            if data.get("type") == "HEARTBEAT":
                print(
                    f"[HEARTBEAT] {data.get('time')}",
                    flush=True
                )
                continue

            instrument = data.get("instrument")

            if not instrument:
                continue

            bids = data.get("bids", [])
            asks = data.get("asks", [])

            bid = bids[0]["price"] if bids else None
            ask = asks[0]["price"] if asks else None

            print(
                f"[PRICE] "
                f"{instrument} | "
                f"BID={bid} | "
                f"ASK={ask} | "
                f"TIME={data.get('time')}",
                flush=True
            )


def main():

    if not TOKEN:
        print("ERROR: OANDA_API_TOKEN is missing.")
        return

    while True:

        try:
            price_stream()

        except Exception as e:

            print("=" * 70)
            print("STREAM ERROR")
            print("=" * 70)
            print(str(e))
            print("Reconnecting in 5 seconds...")
            print("=" * 70)

            time.sleep(5)


if __name__ == "__main__":
    main()

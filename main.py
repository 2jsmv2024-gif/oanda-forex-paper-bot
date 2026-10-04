import os
import json
import time
import requests

# ============================================================
# OANDA PRACTICE - LIVE PRICE STREAM
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


def get_account_id():

    print("=" * 70)
    print("GETTING OANDA ACCOUNT")
    print("=" * 70)

    response = requests.get(
        f"{REST_URL}/v3/accounts",
        headers=HEADERS,
        timeout=20,
    )

    print("Account API HTTP:", response.status_code)

    response.raise_for_status()

    data = response.json()

    accounts = data.get("accounts", [])

    if not accounts:
        raise RuntimeError("No OANDA accounts returned.")

    account_id = accounts[0]["id"]

    print("Authorized account:", account_id)

    return account_id


def price_stream(account_id):

    instruments = ",".join(INSTRUMENTS)

    url = (
        f"{STREAM_URL}/v3/accounts/"
        f"{account_id}/pricing/stream"
        f"?instruments={instruments}"
    )

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/octet-stream",
    }

    print("=" * 70)
    print("OANDA LIVE PRICE STREAM")
    print("=" * 70)
    print("Account     :", account_id)
    print("Instruments :", ", ".join(INSTRUMENTS))
    print("Environment : PRACTICE")
    print("Mode        : READ ONLY")
    print("Orders      : DISABLED")
    print("=" * 70)

    with requests.get(
        url,
        headers=headers,
        stream=True,
        timeout=(20, 90),
    ) as response:

        print("STREAM HTTP STATUS:", response.status_code)

        if response.status_code != 200:
            print("STREAM ERROR RESPONSE:")
            print(response.text[:2000])
            response.raise_for_status()

        print("=" * 70)
        print("PRICE STREAM CONNECTED")
        print("=" * 70)

        for line in response.iter_lines():

            if not line:
                continue

            try:
                data = json.loads(line.decode("utf-8"))
            except Exception:
                continue

            data_type = data.get("type")

            if data_type == "HEARTBEAT":

                print(
                    "[HEARTBEAT]",
                    data.get("time"),
                    flush=True,
                )

                continue

            instrument = data.get("instrument")

            if not instrument:
                continue

            bids = data.get("bids", [])
            asks = data.get("asks", [])

            bid = bids[0].get("price") if bids else None
            ask = asks[0].get("price") if asks else None

            print(
                f"[PRICE] {instrument} | "
                f"BID={bid} | "
                f"ASK={ask} | "
                f"TIME={data.get('time')}",
                flush=True,
            )


def main():

    if not TOKEN:

        print("ERROR: OANDA_API_TOKEN is missing.")

        while True:
            time.sleep(60)

    print("=" * 70)
    print("OANDA PAPER BOT - STREAM TEST")
    print("=" * 70)
    print("Environment : PRACTICE")
    print("Mode        : READ ONLY")
    print("Orders      : DISABLED")
    print("=" * 70)

    while True:

        try:

            account_id = get_account_id()

            price_stream(account_id)

        except Exception as e:

            print("=" * 70)
            print("STREAM ERROR")
            print("=" * 70)
            print(type(e).__name__, ":", str(e))
            print("Reconnecting in 10 seconds...")
            print("=" * 70)

            time.sleep(10)


if __name__ == "__main__":
    main()

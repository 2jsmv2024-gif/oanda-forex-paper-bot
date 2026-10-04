import os
import time
import json
import urllib.request
import urllib.error

# ============================================================
# OANDA PRACTICE - READ ONLY CONNECTION TEST
# NO ORDERS ARE PLACED
# ============================================================

TOKEN = os.getenv("OANDA_API_TOKEN", "").strip()
ENV = os.getenv("OANDA_ENV", "practice").strip().lower()

if ENV in ("live", "fxtrade", "production"):
    BASE_URL = "https://api-fxtrade.oanda.com"
    ENV_NAME = "LIVE"
else:
    BASE_URL = "https://api-fxpractice.oanda.com"
    ENV_NAME = "PRACTICE"


def oanda_get(path):
    url = BASE_URL + path

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="GET",
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def connect_oanda():
    print("=" * 60)
    print("OANDA PAPER BOT")
    print("=" * 60)
    print(f"Environment : {ENV_NAME}")
    print(f"Endpoint    : {BASE_URL}")
    print("Mode        : READ ONLY")
    print("Orders      : DISABLED")
    print("=" * 60)

    if not TOKEN:
        print("ERROR: OANDA_API_TOKEN is missing.")
        print("Add OANDA_API_TOKEN in Railway Variables.")
        return False

    try:
        # ----------------------------------------------------
        # STEP 1: Get accounts authorized for this token
        # ----------------------------------------------------
        data = oanda_get("/v3/accounts")

        accounts = data.get("accounts", [])

        if not accounts:
            print("ERROR: Token is valid but no OANDA accounts were returned.")
            return False

        print(f"Authorized accounts: {len(accounts)}")

        # ----------------------------------------------------
        # STEP 2: Read account summary
        # ----------------------------------------------------
        account_id = accounts[0]["id"]

        print(f"Account ID : {account_id}")

        summary = oanda_get(
            f"/v3/accounts/{account_id}/summary"
        )

        account = summary.get("account", {})

        print("-" * 60)
        print("OANDA CONNECTION SUCCESSFUL")
        print("-" * 60)

        print(f"Account       : {account.get('id')}")
        print(f"Currency      : {account.get('currency')}")
        print(f"Balance       : {account.get('balance')}")
        print(f"NAV           : {account.get('NAV')}")
        print(f"Margin Used   : {account.get('marginUsed')}")
        print(f"Margin Avail. : {account.get('marginAvailable')}")
        print(f"Open Trades   : {account.get('openTradeCount')}")
        print(f"Open Positions: {account.get('openPositionCount')}")
        print(f"Pending Orders: {account.get('pendingOrderCount')}")
        print(f"Unrealized PL : {account.get('unrealizedPL')}")

        print("-" * 60)
        print("READ-ONLY TEST PASSED")
        print("NO ORDER HAS BEEN SENT")
        print("-" * 60)

        return True

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")

        print("=" * 60)
        print("OANDA HTTP ERROR")
        print("=" * 60)
        print(f"HTTP Status: {e.code}")
        print(body)
        print("=" * 60)

        return False

    except Exception as e:
        print("=" * 60)
        print("OANDA CONNECTION ERROR")
        print("=" * 60)
        print(str(e))
        print("=" * 60)

        return False


def main():
    success = connect_oanda()

    if not success:
        print("Connection test failed.")
        print("Waiting before retry...")
    else:
        print("Bot is alive. No trading logic is active.")

    # Keep Railway container alive.
    # No orders are placed in this version.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()

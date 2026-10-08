#!/usr/bin/env python3
import os, json, csv
from pathlib import Path
from datetime import datetime, timezone, timedelta
import requests

REST_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com").rstrip("/")
TOKEN = (os.getenv("OANDA_API_TOKEN", "") or os.getenv("OANDA_TOKEN", "")).strip()
DAYS = int(os.getenv("OANDA_REPORT_DAYS", "30"))
CHUNK_HOURS = int(os.getenv("OANDA_REPORT_CHUNK_HOURS", "6"))
OUT = Path(os.getenv("OANDA_OUTPUT_DIR", "./oanda_output"))
OUT.mkdir(parents=True, exist_ok=True)

FIELDS = [
    "pair", "tradeID", "open_time", "close_time", "engine",
    "realizedPL", "financing", "spread", "net", "pre_spread"
]

def fetch_order_fills(session, account, start, end):
    url = f"{REST_URL}/v3/accounts/{account}/transactions"
    params = {
        "from": start.isoformat().replace("+00:00", "Z"),
        "to": end.isoformat().replace("+00:00", "Z"),
        "pageSize": 1000,
        "type": "ORDER_FILL",
    }
    meta = session.get(url, params=params, timeout=60)
    meta.raise_for_status()
    body = meta.json()

    for page_url in body.get("pages", []):
        page = session.get(page_url, timeout=60)
        page.raise_for_status()
        for tx in page.json().get("transactions", []):
            if tx.get("type") == "ORDER_FILL":
                yield tx

def main():
    if not TOKEN:
        raise RuntimeError("OANDA token missing")

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    })

    account = os.getenv("OANDA_ACCOUNT_ID", "").strip()
    if not account:
        a = session.get(f"{REST_URL}/v3/accounts", timeout=30)
        a.raise_for_status()
        accounts = a.json().get("accounts", [])
        if not accounts:
            raise RuntimeError("No OANDA accounts returned")
        account = accounts[0]["id"]

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=DAYS)

    opens = {}
    trades = []
    transaction_count = 0
    cursor = start
    step = timedelta(hours=max(1, CHUNK_HOURS))

    print(
        f"[SPREAD REPORT] start account={account} from={start.isoformat()} "
        f"to={end.isoformat()} chunk_hours={CHUNK_HOURS}",
        flush=True,
    )

    while cursor < end:
        chunk_end = min(cursor + step, end)
        for t in fetch_order_fills(session, account, cursor, chunk_end):
            transaction_count += 1
            instrument = t.get("instrument", "")
            opened = t.get("tradeOpened")

            if opened:
                tid = str(opened.get("tradeID"))
                opens[tid] = {
                    "open_time": t.get("time"),
                    "open_spread": float(opened.get("halfSpreadCost", 0) or 0),
                    "tag": (
                        t.get("clientExtensions", {}).get("tag", "")
                        or t.get("tradeOpened", {}).get("clientExtensions", {}).get("tag", "")
                    ),
                    "order_id": str(t.get("orderID", "")),
                }

            for closed in t.get("tradesClosed", []) or []:
                tid = str(closed.get("tradeID"))
                opened_info = opens.pop(tid, {})
                realized = float(closed.get("realizedPL", 0) or 0)
                financing = float(closed.get("financing", 0) or 0)
                close_spread = float(closed.get("halfSpreadCost", 0) or 0)
                spread = float(opened_info.get("open_spread", 0)) + close_spread
                net = realized + financing

                trades.append({
                    "pair": instrument,
                    "tradeID": tid,
                    "open_time": opened_info.get("open_time"),
                    "close_time": t.get("time"),
                    "engine": opened_info.get("tag", ""),
                    "realizedPL": realized,
                    "financing": financing,
                    "spread": spread,
                    "net": net,
                    "pre_spread": net + spread,
                })

        cursor = chunk_end

    with open(OUT / "oanda_trade_ledger.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(trades)

    summary = {}
    for trade in trades:
        pair = trade["pair"]
        row = summary.setdefault(pair, {
            "trades": 0, "wins": 0, "losses": 0,
            "gp": 0.0, "gl": 0.0, "net": 0.0,
            "spread": 0.0, "pre": 0.0
        })
        row["trades"] += 1
        row["wins"] += int(trade["net"] > 0)
        row["losses"] += int(trade["net"] < 0)
        row["gp"] += max(trade["net"], 0)
        row["gl"] += min(trade["net"], 0)
        row["net"] += trade["net"]
        row["spread"] += trade["spread"]
        row["pre"] += trade["pre_spread"]

    for row in summary.values():
        row["wr"] = 100 * row["wins"] / row["trades"] if row["trades"] else 0.0
        row["pf"] = row["gp"] / (-row["gl"]) if row["gl"] else 0.0

    report = {
        "account": account,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "chunk_hours": CHUNK_HOURS,
        "transaction_count": transaction_count,
        "closed_trades": len(trades),
        "open_trades_at_end": len(opens),
        "pairs": summary,
    }

    with open(OUT / "oanda_spread_report.json", "w") as f:
        json.dump(report, f, indent=2)

    print(
        f"[SPREAD REPORT] COMPLETE transactions={transaction_count} "
        f"closed_trades={len(trades)} pairs={len(summary)} "
        f"open_at_end={len(opens)}",
        flush=True,
    )

    for pair in sorted(summary):
        row = summary[pair]
        print(
            "[SPREAD PAIR] "
            f"{pair} trades={row['trades']} wins={row['wins']} "
            f"losses={row['losses']} WR={row['wr']:.2f}% "
            f"net={row['net']:.5f} spread={row['spread']:.5f} "
            f"pre_spread={row['pre']:.5f} PF={row['pf']:.3f}",
            flush=True,
        )

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[SPREAD REPORT ERROR] {type(exc).__name__}: {exc}", flush=True)
        raise

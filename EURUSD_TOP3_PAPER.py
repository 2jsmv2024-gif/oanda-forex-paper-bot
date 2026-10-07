#!/usr/bin/env python3
"""
EURUSD TOP-3 PAPER TRADER

Separate paper-trading runner. It does NOT modify or replace main_9STRATEGY_MASTER.py.

The three selected systems are:
  1) 57-59_TRUE_REVERSE / S1 / TF1
  2) 57-59_TRUE_REVERSE / S2 / TF1
  3) 57-59_TRUE_REVERSE / S1 / TF2

Strategy mechanics are imported directly from main_9STRATEGY_MASTER.py.
Only the market/strategy selection and paper-execution layer are separate.

Rules:
  - EURUSD only
  - OANDA Practice market stream
  - first 1 hour after weekly market open: NO entries
  - paper orders only; NO broker orders are submitted
  - each strategy has independent virtual position/P&L
  - existing 9-strategy Railway bot is untouched
"""

import json
import os
import time
from pathlib import Path

import pandas as pd
import requests

import main_9STRATEGY_MASTER as master

MARKET = "EURUSD"
INSTRUMENT = "EUR_USD"
TOP3 = [
    ("TOP1_TR_S1_TF1", "57-59_TRUE_REVERSE", "S1_03:15_08:15", 1),
    ("TOP2_TR_S2_TF1", "57-59_TRUE_REVERSE", "S2_10:15_14:15", 1),
    ("TOP3_TR_S1_TF2", "57-59_TRUE_REVERSE", "S1_03:15_08:15", 2),
]

INITIAL_PAPER_EQUITY = float(os.getenv("EURUSD_PAPER_EQUITY", "10000"))
PAPER_UNITS = int(os.getenv("EURUSD_PAPER_UNITS", "1000"))
OUTPUT_DIR = Path(os.getenv("EURUSD_TOP3_OUTPUT_DIR", "./eurusd_top3_paper"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# The master strategy remains the source of truth for mechanics.
REST_URL = master.REST_URL
STREAM_URL = master.STREAM_URL
TOKEN = master.TOKEN
ACCOUNT_ID_ENV = master.ACCOUNT_ID_ENV

class PaperBook:
    def __init__(self, name):
        self.name = name
        self.equity = INITIAL_PAPER_EQUITY
        self.position = None
        self.entry = None
        self.entry_time = None
        self.units = PAPER_UNITS
        self.realized = 0.0
        self.trades = []

    def open_or_reverse(self, side, price, ts, reason):
        side = side.upper()
        if self.position is None:
            self.position = side
            self.entry = float(price)
            self.entry_time = ts
            print(f"[PAPER OPEN] {self.name} {side} units={self.units} price={price} time={ts} reason={reason}", flush=True)
            return

        if self.position == side:
            return

        pnl = (float(price) - self.entry) * self.units if self.position == "BUY" else (self.entry - float(price)) * self.units
        self.realized += pnl
        self.equity += pnl
        self.trades.append({
            "strategy": self.name,
            "side": self.position,
            "entry_time": str(self.entry_time),
            "exit_time": str(ts),
            "entry": self.entry,
            "exit": float(price),
            "units": self.units,
            "pnl": pnl,
            "equity": self.equity,
            "reason": reason,
        })
        print(f"[PAPER CLOSE] {self.name} {self.position} pnl={pnl:.6f} equity={self.equity:.2f} time={ts} reason={reason}", flush=True)
        self.position = side
        self.entry = float(price)
        self.entry_time = ts
        print(f"[PAPER OPEN] {self.name} {side} units={self.units} price={price} time={ts} reason={reason}", flush=True)

    def mark(self, price):
        if self.position is None:
            return self.equity
        pnl = (float(price) - self.entry) * self.units if self.position == "BUY" else (self.entry - float(price)) * self.units
        return self.equity + pnl

    def save(self):
        pd.DataFrame(self.trades).to_csv(OUTPUT_DIR / f"{self.name}_trades.csv", index=False)
        with open(OUTPUT_DIR / f"{self.name}_state.json", "w", encoding="utf-8") as f:
            json.dump({
                "strategy": self.name,
                "equity": self.equity,
                "realized": self.realized,
                "position": self.position,
                "entry": self.entry,
                "entry_time": str(self.entry_time) if self.entry_time is not None else None,
                "units": self.units,
            }, f, indent=2)

class State:
    def __init__(self):
        self.raw = pd.DataFrame(columns=["datetime","open","high","low","close","volume"])
        self.last_signal = {}

    def append(self, candle):
        self.raw = pd.concat([self.raw, pd.DataFrame([candle])], ignore_index=True)
        self.raw["datetime"] = pd.to_datetime(self.raw["datetime"], utc=True)
        self.raw = self.raw.drop_duplicates("datetime").sort_values("datetime").tail(5000).reset_index(drop=True)

def session_full_for_name(name):
    return name

def latest_signal(raw, engine, tf, session_name):
    prepared = master.prepare_v3(raw, tf, session_name)
    if len(prepared) < 3:
        return None
    trades = master.engine_trades(prepared, MARKET, engine, tf, session_name)
    if trades.empty:
        return None
    candidates = trades[trades["reason"].astype(str) != "END_OF_DATA"].copy()
    if candidates.empty:
        return None
    last = candidates.iloc[-1]
    return {
        "side": str(last["side"]).upper(),
        "entry_time": pd.Timestamp(last["entry_time"]),
        "price": float(last["entry"]),
        "reason": str(last.get("reason", "SIGNAL")),
    }

def trading_allowed():
    state = master.trading_state()
    return state == "TRADING"

def run():
    if not TOKEN:
        raise RuntimeError("OANDA_API_TOKEN/OANDA_TOKEN is missing")

    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})

    account_id = ACCOUNT_ID_ENV
    if not account_id:
        r = s.get(f"{REST_URL}/v3/accounts", timeout=30)
        r.raise_for_status()
        accounts = r.json().get("accounts", [])
        if not accounts:
            raise RuntimeError("No OANDA Practice account returned")
        account_id = accounts[0]["id"]

    books = {name: PaperBook(name) for name, *_ in TOP3}
    state = State()
    current = None
    last_time_state = None

    print("=" * 90)
    print("EURUSD TOP-3 PAPER TRADER")
    print("OANDA:", REST_URL)
    print("Account:", account_id)
    print("Strategy 1: TRUE-REVERSE S1 TF1")
    print("Strategy 2: TRUE-REVERSE S2 TF1")
    print("Strategy 3: TRUE-REVERSE S1 TF2")
    print("First hour after market open: BLOCKED")
    print("Broker orders: DISABLED — PAPER ONLY")
    print("=" * 90)

    url = f"{STREAM_URL}/v3/accounts/{account_id}/pricing/stream"
    params = {"instruments": INSTRUMENT}

    while True:
        try:
            ts_state = master.trading_state()
            if ts_state != last_time_state:
                print(f"[TIME STATE] {ts_state}", flush=True)
                last_time_state = ts_state

            with s.get(url, params=params, stream=True, timeout=(30, 90)) as r:
                r.raise_for_status()
                for raw_line in r.iter_lines():
                    if not raw_line:
                        continue
                    msg = json.loads(raw_line.decode("utf-8"))
                    if msg.get("type") == "HEARTBEAT":
                        continue
                    if msg.get("type") != "PRICE":
                        continue

                    bids = msg.get("bids") or []
                    asks = msg.get("asks") or []
                    if not bids or not asks:
                        continue

                    bid = float(bids[0]["price"])
                    ask = float(asks[0]["price"])
                    mid = (bid + ask) / 2.0
                    ts = pd.Timestamp(msg["time"], tz="UTC")
                    bucket = ts.floor("min")

                    if current is None:
                        current = {"bucket": bucket, "open": mid, "high": mid, "low": mid, "close": mid, "volume": 1}
                        continue

                    if bucket == current["bucket"]:
                        current["high"] = max(current["high"], mid)
                        current["low"] = min(current["low"], mid)
                        current["close"] = mid
                        current["volume"] += 1
                        continue

                    closed = dict(current)
                    closed["datetime"] = closed.pop("bucket")
                    current = {"bucket": bucket, "open": mid, "high": mid, "low": mid, "close": mid, "volume": 1}
                    state.append(closed)

                    if pd.Timestamp(closed["datetime"]).tz_convert("Asia/Kolkata").dayofweek >= 5:
                        continue

                    # Keep building the exact strategy data, but enforce the
                    # requested first-hour execution block.
                    if not trading_allowed():
                        continue

                    for name, engine, session_name, tf in TOP3:
                        ist = pd.Timestamp(closed["datetime"]).tz_convert("Asia/Kolkata")
                        a, b = master.SESSIONS[session_name]
                        if not (pd.Timestamp(a).time() <= ist.time() < pd.Timestamp(b).time()):
                            continue
                        if pd.Timestamp(closed["datetime"]).minute % tf != 0:
                            continue

                        key = (name, str(pd.Timestamp(closed["datetime"])))
                        sig = latest_signal(state.raw, engine, tf, session_name)
                        if not sig:
                            continue

                        signature = (sig["entry_time"], sig["side"], sig["reason"])
                        if state.last_signal.get(name) == signature:
                            continue
                        state.last_signal[name] = signature

                        print(f"[PAPER SIGNAL] {name} {sig['side']} price={sig['price']} entry_time={sig['entry_time']} reason={sig['reason']}", flush=True)
                        books[name].open_or_reverse(sig["side"], sig["price"], sig["entry_time"], sig["reason"])
                        books[name].save()

        except Exception as e:
            print("[STREAM ERROR]", type(e).__name__, str(e), flush=True)
            time.sleep(5)

if __name__ == "__main__":
    run()

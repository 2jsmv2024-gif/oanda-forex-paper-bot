#!/usr/bin/env python3
"""
OANDA 15-MARKET LIVE / RESEARCH MASTER

Components:
  1) OANDA M1 live candle builder
  2) V2
  3) V3
  4) 57-59 NORMAL
  5) 57-59 OPPOSITE
  6) 57-59 TRUE REVERSE
  7) MFP FROZEN (external frozen portfolio consumer)

Safety:
  - OANDA PRACTICE by default
  - orders are DRY-RUN unless OANDA_LIVE_TRADING=true
  - no simultaneous BUY/SELL on one instrument
  - trading starts one hour after daily market open
  - final hour is flat; new entries are blocked

Daily research:
  1m, 3m, 5m, 15m, 30m, 1h on all 15 markets.

The frozen strategy mechanics below are copied from the supplied
OANDA research files. The frozen MFP source is NOT rewritten.
"""

import os
import sys
import json
import time
import math
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import requests


# ============================================================
# CONFIG
# ============================================================

REST_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com").rstrip("/")
STREAM_URL = os.getenv("OANDA_STREAM_URL", "https://stream-fxpractice.oanda.com").rstrip("/")
TOKEN = (os.getenv("OANDA_API_TOKEN", "") or os.getenv("OANDA_TOKEN", "")).strip()
ACCOUNT_ID_ENV = os.getenv("OANDA_ACCOUNT_ID", "").strip()

LIVE_TRADING_ENABLED = os.getenv("OANDA_LIVE_TRADING", "false").strip().lower() == "true"
DEFAULT_UNITS = int(os.getenv("OANDA_DEFAULT_UNITS", "1"))
OUTPUT_DIR = Path(os.getenv("OANDA_OUTPUT_DIR", "./oanda_output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MASTER_MODE = os.getenv("MASTER_MODE", "LIVE").strip().upper()
LIVE_EXECUTION_ENGINE = os.getenv("LIVE_EXECUTION_ENGINE", "57-59_NORMAL").strip().upper()
LIVE_TF = int(os.getenv("LIVE_TF", "15"))

# Exact 15 requested markets.
MARKETS = {
    "EURUSD": "EUR_USD",
    "EURNZD": "EUR_NZD",
    "EURJPY": "EUR_JPY",
    "EURCHF": "EUR_CHF",
    "EURAUD": "EUR_AUD",
    "EURCAD": "EUR_CAD",
    "GBPUSD": "GBP_USD",
    "GBPJPY": "GBP_JPY",
    "GBPCAD": "GBP_CAD",
    "GBPCHF": "GBP_CHF",
    "GBPAUD": "GBP_AUD",
    "GBPNZD": "GBP_NZD",
    "XAUUSD": "XAU_USD",
    "BTCUSD": "BTC_USD",
    "USDJPY": "USD_JPY",
}

DAILY_TFS = [1, 3, 5, 15, 30, 60]

SESSIONS = {
    "S1": ("03:15", "08:15"),
    "S2": ("10:15", "14:15"),
    "S3": ("16:15", "21:15"),
}

# Locked primary 57-59 selections from the supplied research source.
PRIMARY_5759_TFS = {
    "S1": [10, 13, 15],
    "S2": [6, 14, 15],
    "S3": [14, 10, 15],
}

PIP_SIZE = {
    "EURUSD": 0.0001, "EURNZD": 0.0001, "EURJPY": 0.01,
    "EURCHF": 0.0001, "EURAUD": 0.0001, "EURCAD": 0.0001,
    "GBPUSD": 0.0001, "GBPJPY": 0.01, "GBPCAD": 0.0001,
    "GBPCHF": 0.0001, "GBPAUD": 0.0001, "GBPNZD": 0.0001,
    "XAUUSD": 0.01, "BTCUSD": 0.01, "USDJPY": 0.01,
}

SPREAD_PIPS = float(os.getenv("SPREAD_PIPS", "0"))

# MFP is deliberately separate and untouched.
MFP_FROZEN_FILE = os.getenv(
    "MFP_FROZEN_FILE",
    "./XAUUSD_MFPP_T1_T2_T3_DAILY33_015_BASE_MODEL.py"
).strip()
MFP_CANDIDATE_FILE = os.getenv(
    "MFP_CANDIDATE_FILE",
    "./OANDA_3MARKETS_T1_T2_T3_CANDIDATE_TRADES_NO_SPREAD.csv"
).strip()

# Execution schedule. OANDA's exact schedule can vary by instrument/account;
# these are configurable safety defaults from the supplied execution layer.
DAILY_OPEN_UTC = os.getenv("OANDA_DAILY_OPEN_UTC", "22:00").strip()
DAILY_CLOSE_UTC = os.getenv("OANDA_DAILY_CLOSE_UTC", "21:00").strip()
OPEN_DELAY_HOURS = 1
CLOSE_BEFORE_HOURS = 1


# ============================================================
# HTTP
# ============================================================

def get_http_session():
    if not TOKEN:
        raise RuntimeError("OANDA_API_TOKEN/OANDA_TOKEN is missing.")
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    return s


def get_account_id(s):
    if ACCOUNT_ID_ENV:
        return ACCOUNT_ID_ENV
    r = s.get(f"{REST_URL}/v3/accounts", timeout=30)
    r.raise_for_status()
    accounts = r.json().get("accounts", [])
    if not accounts:
        raise RuntimeError("No OANDA accounts returned.")
    return accounts[0]["id"]


# ============================================================
# TIME CONTROL
# ============================================================

def parse_hhmm(value):
    h, m = value.split(":")
    return int(h), int(m)


def day_at(day, hhmm):
    h, m = parse_hhmm(hhmm)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)


def market_window(now=None):
    now = now or datetime.now(timezone.utc)
    wd = now.weekday()
    if wd == 5:  # Saturday
        return None, None
    if wd == 6:  # Sunday before open / Sunday session
        op = day_at(now.date(), DAILY_OPEN_UTC)
        if now < op:
            return None, None
        cl = day_at(now.date() + timedelta(days=1), DAILY_CLOSE_UTC)
        return op, cl
    op = day_at(now.date() - timedelta(days=1), DAILY_OPEN_UTC)
    cl = day_at(now.date(), DAILY_CLOSE_UTC)
    return op, cl


def trading_state(now=None):
    now = now or datetime.now(timezone.utc)
    op, cl = market_window(now)
    if op is None or cl is None:
        return "CLOSED"
    start = op + timedelta(hours=OPEN_DELAY_HOURS)
    force_close = cl - timedelta(hours=CLOSE_BEFORE_HOURS)
    if now < start:
        return "OPEN_PROTECTION"
    if now >= force_close:
        return "CLOSE_PROTECTION"
    return "TRADING"


# ============================================================
# POSITION / EXECUTION
# ============================================================

def position_units(position):
    if not position:
        return 0
    long_u = int(float(position.get("long", {}).get("units", "0")))
    short_u = int(float(position.get("short", {}).get("units", "0")))
    return long_u + short_u


def get_open_positions(s, account_id):
    r = s.get(f"{REST_URL}/v3/accounts/{account_id}/openPositions", timeout=20)
    r.raise_for_status()
    return r.json().get("positions", [])


def get_position(s, account_id, instrument):
    r = s.get(f"{REST_URL}/v3/accounts/{account_id}/positions/{instrument}", timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("position")


def close_instrument(s, account_id, instrument):
    pos = get_position(s, account_id, instrument)
    if not pos:
        return None
    lu = float(pos.get("long", {}).get("units", "0"))
    su = float(pos.get("short", {}).get("units", "0"))
    if lu == 0 and su == 0:
        return None
    payload = {"longUnits": "ALL", "shortUnits": "ALL"}
    if not LIVE_TRADING_ENABLED:
        print(f"[DRY-RUN CLOSE] {instrument} long={lu} short={su}", flush=True)
        return payload
    r = s.put(f"{REST_URL}/v3/accounts/{account_id}/positions/{instrument}/close", json=payload, timeout=20)
    r.raise_for_status()
    print(f"[POSITION CLOSED] {instrument}", flush=True)
    return r.json()


def close_all_positions(s, account_id):
    try:
        positions = get_open_positions(s, account_id)
    except Exception as e:
        print("[FORCE CLOSE READ ERROR]", repr(e), flush=True)
        return
    instruments = []
    for p in positions:
        ins = p.get("instrument")
        if ins and position_units(p) != 0:
            instruments.append(ins)
    if not instruments:
        print("[FLAT] No open positions.", flush=True)
        return
    print("[FORCE CLOSE] Final-hour protection", flush=True)
    for ins in sorted(set(instruments)):
        try:
            close_instrument(s, account_id, ins)
        except Exception as e:
            print("[CLOSE ERROR]", ins, repr(e), flush=True)


def execute_signal(s, account_id, instrument, side, units=None, strategy_name="57-59_NORMAL"):
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if trading_state() != "TRADING":
        print(f"[SIGNAL BLOCKED] {instrument} {side} state={trading_state()}", flush=True)
        return None
    units = abs(int(DEFAULT_UNITS if units is None else units))
    if units == 0:
        return None

    pos = get_position(s, account_id, instrument)
    current = position_units(pos)
    if current != 0:
        current_side = "BUY" if current > 0 else "SELL"
        if current_side != side:
            close_instrument(s, account_id, instrument)
            time.sleep(0.5)
        else:
            print(f"[DUPLICATE SIGNAL BLOCKED] {instrument} {side}", flush=True)
            return None

    signed = units if side == "BUY" else -units
    payload = {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(signed),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "clientExtensions": {
                "tag": strategy_name[:20],
                "comment": "OANDA frozen-system live demo",
            },
        }
    }
    if not LIVE_TRADING_ENABLED:
        print(f"[DRY-RUN ORDER] {instrument} {side} units={units} engine={strategy_name}", flush=True)
        return {"dry_run": True, **payload}
    r = s.post(f"{REST_URL}/v3/accounts/{account_id}/orders", json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    print("[ORDER ACCEPTED]", json.dumps(data), flush=True)
    return data


# ============================================================
# DATA / HA
# ============================================================

def add_ha(df):
    if df.empty:
        return df.copy()
    x = df.copy()
    x["ha_close"] = (x["open"] + x["high"] + x["low"] + x["close"]) / 4.0
    ho = np.empty(len(x), dtype=float)
    ho[0] = (x["open"].iloc[0] + x["close"].iloc[0]) / 2.0
    for i in range(1, len(x)):
        ho[i] = (ho[i - 1] + x["ha_close"].iloc[i - 1]) / 2.0
    x["ha_open"] = ho
    x["ha_high"] = x[["high", "ha_open", "ha_close"]].max(axis=1)
    x["ha_low"] = x[["low", "ha_open", "ha_close"]].min(axis=1)
    return x


def resample_tf(raw, tf):
    if raw.empty:
        return raw.copy()
    x = raw.copy()
    x["datetime"] = pd.to_datetime(x["datetime"], utc=True)
    x = x.set_index("datetime")
    out = x.resample(f"{tf}min", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna().reset_index()
    return out


def session_filter(df, session_name):
    if df.empty:
        return df.copy()
    start_s, end_s = SESSIONS[session_name]
    x = df.copy()
    x["datetime"] = pd.to_datetime(x["datetime"], utc=True)
    ist = x["datetime"].dt.tz_convert("Asia/Kolkata")
    st = pd.Timestamp(start_s).time()
    en = pd.Timestamp(end_s).time()
    return x[(ist.dt.dayofweek < 5) & (ist.dt.time >= st) & (ist.dt.time < en)].copy()


def prepare_v2(raw, tf, session_name):
    return add_ha(resample_tf(session_filter(raw, session_name), tf))


def prepare_v3(raw, tf, session_name):
    return session_filter(add_ha(resample_tf(raw, tf)), session_name)


# ============================================================
# FROZEN NORMAL MECHANICS
# ============================================================

def run_frozen_mechanics(df):
    if len(df) < 3:
        return pd.DataFrame()
    rows = df.reset_index(drop=True)
    trades = []
    position = None
    entry = None
    entry_time = None
    reference = None

    for i in range(2, len(rows)):
        c2, c1, c0 = rows.iloc[i - 2], rows.iloc[i - 1], rows.iloc[i]
        c2_bull = c2.ha_close > c2.ha_open
        c2_bear = c2.ha_close < c2.ha_open
        buy_signal = c2_bull and c1.ha_open < c1.ha_high
        sell_signal = c2_bear and c1.ha_open > c1.ha_low

        if buy_signal:
            if position == "SELL":
                trades.append({"entry_time": entry_time, "exit_time": c0.datetime, "side": "SELL", "entry": entry, "exit": c0.ha_close, "reason": "NEW_BUY_SIGNAL"})
            position, entry, entry_time, reference = "BUY", c0.ha_close, c0.datetime, c1.ha_low
        elif sell_signal:
            if position == "BUY":
                trades.append({"entry_time": entry_time, "exit_time": c0.datetime, "side": "BUY", "entry": entry, "exit": c0.ha_close, "reason": "NEW_SELL_SIGNAL"})
            position, entry, entry_time, reference = "SELL", c0.ha_close, c0.datetime, c1.ha_high

        if position == "BUY":
            if c0.ha_low < reference:
                trades.append({"entry_time": entry_time, "exit_time": c0.datetime, "side": "BUY", "entry": entry, "exit": reference, "reason": "LOW_BREAK"})
                position, entry, entry_time, reference = "SELL", reference, c0.datetime, c0.ha_high
            else:
                reference = c0.ha_low
        elif position == "SELL":
            if c0.ha_high > reference:
                trades.append({"entry_time": entry_time, "exit_time": c0.datetime, "side": "SELL", "entry": entry, "exit": reference, "reason": "HIGH_BREAK"})
                position, entry, entry_time, reference = "BUY", reference, c0.datetime, c0.ha_low
            else:
                reference = c0.ha_high

    if position is not None:
        last = rows.iloc[-1]
        trades.append({"entry_time": entry_time, "exit_time": last.datetime, "side": position, "entry": entry, "exit": last.ha_close, "reason": "END_OF_DATA"})

    return pd.DataFrame(trades)


# ============================================================
# OPPOSITE: SAME FROZEN MECHANICS, EXECUTION DIRECTION INVERTED
# ============================================================

def run_5759_opposite(df):
    if len(df) < 3:
        return pd.DataFrame()
    rows = df.reset_index(drop=True)
    trades = []
    position = None
    entry = None
    reference = None
    entry_time = None

    for i in range(2, len(rows)):
        c2, c1, c0 = rows.iloc[i - 2], rows.iloc[i - 1], rows.iloc[i]
        ts = c0.datetime
        original_buy = (c2.ha_close > c2.ha_open) and (c1.ha_open < c1.ha_high)
        original_sell = (c2.ha_close < c2.ha_open) and (c1.ha_open > c1.ha_low)
        opposite_sell = original_buy
        opposite_buy = original_sell

        if position == "SELL" and opposite_buy:
            trades.append({"entry_time": entry_time, "exit_time": ts, "side": "SELL", "entry": entry, "exit": c0.ha_close, "reason": "OPPOSITE_NEW_BUY_SIGNAL"})
            position, entry, reference, entry_time = "BUY", c0.ha_close, c1.ha_high, ts
            continue
        if position == "BUY" and opposite_sell:
            trades.append({"entry_time": entry_time, "exit_time": ts, "side": "BUY", "entry": entry, "exit": c0.ha_close, "reason": "OPPOSITE_NEW_SELL_SIGNAL"})
            position, entry, reference, entry_time = "SELL", c0.ha_close, c1.ha_low, ts
            continue
        if position is None:
            if opposite_sell:
                position, entry, reference, entry_time = "SELL", c0.ha_close, c1.ha_high, ts
                continue
            if opposite_buy:
                position, entry, reference, entry_time = "BUY", c0.ha_close, c1.ha_low, ts
                continue

        if position == "BUY":
            if c0.ha_high > reference:
                trades.append({"entry_time": entry_time, "exit_time": ts, "side": "BUY", "entry": entry, "exit": reference, "reason": "OPPOSITE_HIGH_BREAK"})
                position, entry, reference, entry_time = "SELL", reference, c0.ha_low, ts
            else:
                reference = c0.ha_high
        elif position == "SELL":
            if c0.ha_low < reference:
                trades.append({"entry_time": entry_time, "exit_time": ts, "side": "SELL", "entry": entry, "exit": reference, "reason": "OPPOSITE_LOW_BREAK"})
                position, entry, reference, entry_time = "BUY", reference, c0.ha_high, ts
            else:
                reference = c0.ha_low

    if position is not None:
        last = rows.iloc[-1]
        trades.append({"entry_time": entry_time, "exit_time": last.datetime, "side": position, "entry": entry, "exit": last.ha_close, "reason": "END_OF_DATA"})
    return pd.DataFrame(trades)


def add_pnl(trades, market):
    if trades.empty:
        return trades.copy()
    x = trades.copy()
    x["gross"] = np.where(x["side"].eq("BUY"), x["exit"] - x["entry"], x["entry"] - x["exit"])
    x["spread_cost"] = PIP_SIZE.get(market, 0.0001) * SPREAD_PIPS
    x["net"] = x["gross"] - x["spread_cost"]
    return x


def true_reverse_completed_stream(trades):
    if trades.empty:
        return trades.copy()
    x = trades.copy()
    x["side"] = x["side"].map({"BUY": "SELL", "SELL": "BUY"})
    x["gross"] = -x["gross"]
    x["spread_cost"] = 0.0
    x["net"] = x["gross"]
    x["reason"] = "TRUE_REVERSE"
    return x


# ============================================================
# LIVE CLOSED-TF PROCESSOR
# ============================================================

class LiveEngine:
    """Build TF candles from completed M1 candles and detect new live entries."""
    def __init__(self, market, engine, tf, session_name):
        self.market = market
        self.engine = engine
        self.tf = tf
        self.session_name = session_name
        self.raw = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        self.last_processed = None
        self.last_event_time = None

    def append_m1(self, candle):
        row = pd.DataFrame([candle])
        self.raw = pd.concat([self.raw, row], ignore_index=True)
        self.raw["datetime"] = pd.to_datetime(self.raw["datetime"], utc=True)
        self.raw = self.raw.drop_duplicates("datetime").sort_values("datetime").tail(5000).reset_index(drop=True)
        return self.process_closed_tf()

    def process_closed_tf(self):
        if len(self.raw) < max(30, self.tf * 5):
            return None

        if self.engine == "V2":
            df = prepare_v2(self.raw, self.tf, self.session_name)
        else:
            # V3 is the base preparation for 57-59 NORMAL,
            # 57-59 OPPOSITE and the mathematical TRUE REVERSE.
            df = prepare_v3(self.raw, self.tf, self.session_name)

        if len(df) < 4:
            return None

        # Last TF bucket is still forming. Work only through the previous
        # completed TF candle.
        idx = len(df) - 2
        if idx < 2:
            return None
        candle_time = pd.Timestamp(df.iloc[idx].datetime)
        if self.last_processed is not None and candle_time <= self.last_processed:
            return None
        self.last_processed = candle_time

        work = df.iloc[:idx + 1].reset_index(drop=True)
        events = self._entry_events(work)
        if not events:
            return None

        event = events[-1]
        et = pd.Timestamp(event["time"])
        if self.last_event_time is not None and et <= self.last_event_time:
            return None
        self.last_event_time = et

        return {
            "side": event["side"],
            "time": et,
            "reason": event["reason"],
            "engine": self.engine,
            "tf": self.tf,
            "session": self.session_name,
        }

    def _entry_events(self, rows):
        """Replay the supplied frozen mechanics and return entry/reversal events."""
        events = []
        position = None
        reference = None

        for i in range(2, len(rows)):
            c2, c1, c0 = rows.iloc[i - 2], rows.iloc[i - 1], rows.iloc[i]
            ts = pd.Timestamp(c0.datetime)
            c2_bull = c2.ha_close > c2.ha_open
            c2_bear = c2.ha_close < c2.ha_open
            original_buy = c2_bull and c1.ha_open < c1.ha_high
            original_sell = c2_bear and c1.ha_open > c1.ha_low

            if self.engine == "57-59_OPPOSITE":
                want_buy = original_sell
                want_sell = original_buy
            else:
                want_buy = original_buy
                want_sell = original_sell

            if want_buy:
                # Frozen mechanics assign BUY here even if already BUY.
                # The execution layer blocks a duplicate same-side order.
                if position != "BUY":
                    position = "BUY"
                    events.append({"time": ts, "side": "BUY", "reason": "NEW_BUY_SIGNAL"})
                else:
                    position = "BUY"
                if self.engine == "57-59_OPPOSITE":
                    reference = c1.ha_high
                else:
                    reference = c1.ha_low

            elif want_sell:
                if position != "SELL":
                    position = "SELL"
                    events.append({"time": ts, "side": "SELL", "reason": "NEW_SELL_SIGNAL"})
                else:
                    position = "SELL"
                if self.engine == "57-59_OPPOSITE":
                    reference = c1.ha_low
                else:
                    reference = c1.ha_high

            if position == "BUY":
                if self.engine == "57-59_OPPOSITE":
                    if c0.ha_high > reference:
                        position = "SELL"
                        events.append({"time": ts, "side": "SELL", "reason": "OPPOSITE_HIGH_BREAK"})
                        reference = c0.ha_low
                    else:
                        reference = c0.ha_high
                else:
                    if c0.ha_low < reference:
                        position = "SELL"
                        events.append({"time": ts, "side": "SELL", "reason": "REFERENCE_LOW_BREAK"})
                        reference = c0.ha_high
                    else:
                        reference = c0.ha_low

            elif position == "SELL":
                if self.engine == "57-59_OPPOSITE":
                    if c0.ha_low < reference:
                        position = "BUY"
                        events.append({"time": ts, "side": "BUY", "reason": "OPPOSITE_LOW_BREAK"})
                        reference = c0.ha_high
                    else:
                        reference = c0.ha_low
                else:
                    if c0.ha_high > reference:
                        position = "BUY"
                        events.append({"time": ts, "side": "BUY", "reason": "REFERENCE_HIGH_BREAK"})
                        reference = c0.ha_low
                    else:
                        reference = c0.ha_high

        # TRUE REVERSE is the mathematical side inversion of the normal
        # completed/entry stream. It does not alter the normal mechanics.
        if self.engine == "57-59_TRUE_REVERSE":
            for e in events:
                e["side"] = "SELL" if e["side"] == "BUY" else "BUY"
                e["reason"] = "TRUE_REVERSE"

        return events


# ============================================================
# LIVE M1 STREAM
# ============================================================

def current_session_name(ts_utc):
    ist = pd.Timestamp(ts_utc).tz_convert("Asia/Kolkata")
    tm = ist.time()
    for name, (a, b) in SESSIONS.items():
        if pd.Timestamp(a).time() <= tm < pd.Timestamp(b).time():
            return name
    return None


def stream_live(s, account_id):
    instruments = ",".join(MARKETS.values())
    url = f"{STREAM_URL}/v3/accounts/{account_id}/pricing/stream"
    params = {"instruments": instruments}

    # One selected execution engine is allowed to send signals. Other engines
    # can be monitored separately by changing LIVE_EXECUTION_ENGINE and restarting.
    engines = {}
    current = {}

    print("=" * 100)
    print("OANDA 15-MARKET LIVE MASTER")
    print("=" * 100)
    print("Markets:", len(MARKETS))
    print("Execution engine:", LIVE_EXECUTION_ENGINE)
    print("Live TF:", LIVE_TF)
    print("Orders:", "ENABLED (PRACTICE)" if LIVE_TRADING_ENABLED else "DRY-RUN")
    print("Time state:", trading_state())
    print("=" * 100)

    while True:
        try:
            if trading_state() == "CLOSE_PROTECTION":
                close_all_positions(s, account_id)

            with s.get(url, params=params, stream=True, timeout=(30, 90)) as r:
                r.raise_for_status()
                for raw_line in r.iter_lines():
                    if not raw_line:
                        continue
                    msg = json.loads(raw_line.decode("utf-8"))
                    if msg.get("type") == "HEARTBEAT":
                        print("[HEARTBEAT]", msg.get("time"), "state=", trading_state(), flush=True)
                        continue
                    if msg.get("type") != "PRICE":
                        continue
                    instrument = msg.get("instrument")
                    bids, asks = msg.get("bids") or [], msg.get("asks") or []
                    if not bids or not asks or instrument not in MARKETS.values():
                        continue
                    bid, ask = float(bids[0]["price"]), float(asks[0]["price"])
                    mid = (bid + ask) / 2.0
                    ts = pd.Timestamp(msg["time"], tz="UTC")
                    bucket = ts.floor("min")
                    state = current.get(instrument)
                    if state is None:
                        current[instrument] = {"bucket": bucket, "open": mid, "high": mid, "low": mid, "close": mid, "volume": 1}
                        continue
                    if bucket == state["bucket"]:
                        state["high"] = max(state["high"], mid)
                        state["low"] = min(state["low"], mid)
                        state["close"] = mid
                        state["volume"] += 1
                        continue

                    # Complete M1 candle.
                    closed = dict(state)
                    closed["datetime"] = closed.pop("bucket")
                    market = next(k for k, v in MARKETS.items() if v == instrument)
                    print(f"[M1 CLOSED] {market} {closed['datetime']} O={closed['open']} H={closed['high']} L={closed['low']} C={closed['close']}", flush=True)
                    current[instrument] = {"bucket": bucket, "open": mid, "high": mid, "low": mid, "close": mid, "volume": 1}

                    # Session engines only process within the user's defined IST sessions.
                    session = current_session_name(closed["datetime"])
                    if session is None:
                        continue

                    key = (market, LIVE_EXECUTION_ENGINE, LIVE_TF, session)
                    eng = engines.get(key)
                    if eng is None:
                        eng = LiveEngine(market, LIVE_EXECUTION_ENGINE, LIVE_TF, session)
                        engines[key] = eng
                    signal = eng.append_m1(closed)
                    if signal is not None:
                        print("[SIGNAL]", market, signal, flush=True)
                        execute_signal(s, account_id, instrument, signal["side"], DEFAULT_UNITS, LIVE_EXECUTION_ENGINE)

        except Exception as e:
            print("[STREAM ERROR]", type(e).__name__, str(e), flush=True)
            time.sleep(5)


# ============================================================
# HISTORICAL DAILY RESEARCH
# ============================================================

def resolve_test_date():
    d = pd.Timestamp(os.getenv("TEST_DATE", ""), tz="UTC") if os.getenv("TEST_DATE", "").strip() else pd.Timestamp.now(tz="UTC")
    d = d.normalize()
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d


def download_oanda_m1(s, instrument, day):
    start = pd.Timestamp(day, tz="UTC")
    end = start + pd.Timedelta(days=1)
    rows, cursor = [], start
    while cursor < end:
        chunk_end = min(cursor + pd.Timedelta(hours=6), end)
        params = {"granularity": "M1", "price": "M", "from": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"), "to": chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ"), "smooth": "false"}
        r = s.get(f"{REST_URL}/v3/instruments/{instrument}/candles", params=params, timeout=60)
        r.raise_for_status()
        for c in r.json().get("candles", []):
            if not c.get("complete") or not c.get("mid"):
                continue
            m = c["mid"]
            rows.append({"datetime": c["time"], "open": float(m["o"]), "high": float(m["h"]), "low": float(m["l"]), "close": float(m["c"]), "volume": int(c.get("volume", 0))})
        cursor = chunk_end
        time.sleep(0.1)
    if not rows:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    x = pd.DataFrame(rows)
    x["datetime"] = pd.to_datetime(x["datetime"], utc=True)
    return x.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)


def metrics(trades):
    if trades.empty:
        return {"Trades": 0, "Wins": 0, "Losses": 0, "WR": 0.0, "PF": 0.0, "Net": 0.0, "MaxDD": 0.0}
    v = trades["net"].astype(float).to_numpy()
    gp = float(v[v > 0].sum())
    gl = float(-v[v < 0].sum())
    eq = np.cumsum(v)
    peak = np.maximum.accumulate(eq)
    return {"Trades": len(v), "Wins": int((v > 0).sum()), "Losses": int((v < 0).sum()), "WR": float((v > 0).mean() * 100), "PF": gp / gl if gl else math.inf, "Net": float(v.sum()), "MaxDD": float((peak - eq).max())}


def run_daily_research(s):
    day = resolve_test_date()
    all_results, all_trades = [], []
    for market, instrument in MARKETS.items():
        print(f"[DATA] {market} {instrument}", flush=True)
        raw = download_oanda_m1(s, instrument, day)
        raw.to_csv(OUTPUT_DIR / f"{market}_{day.date()}_M1.csv", index=False)
        if raw.empty:
            continue
        for tf in DAILY_TFS:
            for session_name in SESSIONS:
                v2 = add_pnl(run_frozen_mechanics(prepare_v2(raw, tf, session_name)), market)
                v3 = add_pnl(run_frozen_mechanics(prepare_v3(raw, tf, session_name)), market)
                normal = v3.copy()
                opposite = add_pnl(run_5759_opposite(prepare_v3(raw, tf, session_name)), market)
                true_reverse = true_reverse_completed_stream(opposite)
                engines = {"V2": v2, "V3": v3, "57-59_NORMAL": normal, "57-59_OPPOSITE": opposite, "57-59_TRUE_REVERSE": true_reverse}
                for name, trades in engines.items():
                    m = metrics(trades)
                    all_results.append({"Market": market, "TF": tf, "Session": session_name, "Engine": name, **m})
                    if not trades.empty:
                        z = trades.copy()
                        z.insert(0, "Market", market); z.insert(1, "TF", tf); z.insert(2, "Session", session_name); z.insert(3, "Engine", name)
                        all_trades.append(z)
    results = pd.DataFrame(all_results)
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    results.to_csv(OUTPUT_DIR / f"OANDA_15MARKET_DAILY_RESULTS_{day.date()}.csv", index=False)
    trades.to_csv(OUTPUT_DIR / f"OANDA_15MARKET_DAILY_TRADES_{day.date()}.csv", index=False)
    print("[DAILY RESEARCH COMPLETE]", day.date(), flush=True)
    if not results.empty:
        print(results.sort_values(["Engine", "PF"], ascending=[True, False]).head(50).to_string(index=False))


# ============================================================
# MFP FROZEN RUNNER
# ============================================================

def run_mfp_frozen_exact():
    frozen = Path(MFP_FROZEN_FILE).resolve()
    candidate = Path(MFP_CANDIDATE_FILE).resolve()
    print("MFP frozen source:", frozen)
    print("MFP candidate:", candidate)
    if not frozen.exists():
        print("[MFP] NOT RUN: frozen source file missing.")
        return 1
    if not candidate.exists():
        print("[MFP] NOT RUN: candidate file missing.")
        return 2
    # Do not rewrite or import/modify the frozen strategy. Execute the supplied file.
    return subprocess.run([sys.executable, str(frozen)], cwd=str(frozen.parent), env=os.environ.copy(), check=False).returncode


def status(s, account_id):
    print("OANDA MASTER STATUS")
    print("Practice REST:", REST_URL)
    print("Account:", account_id)
    print("Markets:", len(MARKETS))
    print("Daily TFs:", DAILY_TFS)
    print("Sessions:", SESSIONS)
    print("Primary 57-59 TFs:", PRIMARY_5759_TFS)
    print("Execution engine:", LIVE_EXECUTION_ENGINE)
    print("Execution TF:", LIVE_TF)
    print("Trading state:", trading_state())
    print("Live trading:", LIVE_TRADING_ENABLED)
    print("MFP frozen file:", MFP_FROZEN_FILE)


def main():
    s = get_http_session()
    account_id = get_account_id(s)
    print("=" * 100)
    print("OANDA 15-MARKET MASTER")
    print("Mode:", MASTER_MODE)
    print("Account:", account_id)
    print("Practice:", REST_URL)
    print("Markets:", ", ".join(MARKETS.values()))
    print("=" * 100)

    if MASTER_MODE == "LIVE":
        stream_live(s, account_id)
    elif MASTER_MODE == "DAILY_RESEARCH":
        run_daily_research(s)
    elif MASTER_MODE == "MFP":
        run_mfp_frozen_exact()
    elif MASTER_MODE == "STATUS":
        status(s, account_id)
    else:
        raise ValueError("MASTER_MODE must be LIVE, DAILY_RESEARCH, MFP, or STATUS")


if __name__ == "__main__":
    main()

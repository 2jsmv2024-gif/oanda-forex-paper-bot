#!/usr/bin/env python3
"""
OANDA 15-MARKET / MULTI-TIMEFRAME MASTER

ONE CODE, THREE WEEK-DEMO STRATEGIES + FROZEN MFP REFERENCE

Live/session framework:
  - 15 OANDA markets
  - 3 IST sessions
  - every timeframe 1M through 15M
  - V2
  - V3
  - 57-59 NORMAL
  - 57-59 OPPOSITE
  - 57-59 TRUE REVERSE

Full-day research framework:
  - 1M, 3M, 5M, 15M, 30M, 1H
  - all 15 markets
  - weekends skipped

Time protection:
  - market open + 1 hour: no new entries
  - market close - 1 hour: close all positions / no new entries
  - final hour: flat

Safety:
  - OANDA Practice by default
  - DRY-RUN by default
  - all 3 strategies are monitored across all requested session/full-day systems
  - virtual performance is tracked independently for every market/TF/session system
  - OANDA orders remain DRY-RUN until explicitly enabled
  - frozen MFP source is NOT rewritten

IMPORTANT:
  The strategy mechanics are based on the supplied research source.
  This file does not alter the frozen MFP portfolio model.
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

# Live execution: ONE selected combination may send orders.
LIVE_EXECUTION_ENGINE = os.getenv("LIVE_EXECUTION_ENGINE", "ALL3").strip().upper()
LIVE_TF = int(os.getenv("LIVE_TF", "0"))  # 0 = all session TFs 1-15
LIVE_SESSION = os.getenv("LIVE_SESSION", "AUTO").strip().upper()  # AUTO = all active sessions
LIVE_STRATEGIES = [x.strip().upper() for x in os.getenv(
    "LIVE_STRATEGIES", "A_TRUE_REVERSE,B_TRUE_REVERSE,V3_OPPOSITE"
).split(",") if x.strip()]

# Monitor all requested live engines/timeframes by default.
MONITOR_ALL_LIVE = os.getenv("MONITOR_ALL_LIVE", "true").strip().lower() == "true"

# Exact 15 markets.
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

# Live/session: EVERY minute timeframe from 1M through 15M.
SESSION_TFS = list(range(1, 16))

# Full-day test group.
DAILY_TFS = [1, 3, 5, 15, 30, 60]

ENGINES = [
    "V2",
    "V3",
    "57-59_NORMAL",
    "57-59_OPPOSITE",
    "57-59_TRUE_REVERSE",
]

LIVE_STRATEGY_NAMES = ["A_TRUE_REVERSE", "B_TRUE_REVERSE", "V3_OPPOSITE"]

# Full-day live-performance group. No S1/S2/S3 filter.
FULLDAY_LIVE_TFS = [1, 3, 5, 15, 30, 60]

SESSIONS = {
    "S1_03:15_08:15": ("03:15", "08:15"),
    "S2_10:15_14:15": ("10:15", "14:15"),
    "S3_16:15_21:15": ("16:15", "21:15"),
}

# OANDA's daily FX-style window is configurable because the exact schedule
# can vary by instrument/account and daylight-saving period.
MARKET_OPEN_UTC = os.getenv("OANDA_MARKET_OPEN_UTC", "22:00")
MARKET_CLOSE_UTC = os.getenv("OANDA_MARKET_CLOSE_UTC", "21:00")
OPEN_DELAY_HOURS = float(os.getenv("OANDA_OPEN_DELAY_HOURS", "1"))
CLOSE_BEFORE_HOURS = float(os.getenv("OANDA_CLOSE_BEFORE_HOURS", "1"))

# Research spread. Zero is the default for strategy comparison.
SPREAD_PIPS = float(os.getenv("SPREAD_PIPS", "0"))

PIP_SIZE = {
    "EURUSD": 0.0001, "EURNZD": 0.0001, "EURJPY": 0.01,
    "EURCHF": 0.0001, "EURAUD": 0.0001, "EURCAD": 0.0001,
    "GBPUSD": 0.0001, "GBPJPY": 0.01, "GBPCAD": 0.0001,
    "GBPCHF": 0.0001, "GBPAUD": 0.0001, "GBPNZD": 0.0001,
    "XAUUSD": 0.01, "BTCUSD": 0.01, "USDJPY": 0.01,
}

MFP_FROZEN_FILE = os.getenv(
    "MFP_FROZEN_FILE",
    "./XAUUSD_MFPP_T1_T2_T3_DAILY33_015_BASE_MODEL.py",
).strip()
MFP_CANDIDATE_FILE = os.getenv(
    "MFP_CANDIDATE_FILE",
    "./OANDA_3MARKETS_T1_T2_T3_CANDIDATE_TRADES_NO_SPREAD.csv",
).strip()

# ============================================================
# OANDA CONNECTION
# ============================================================
def get_http_session():
    if not TOKEN:
        raise RuntimeError("OANDA_API_TOKEN/OANDA_TOKEN is missing")
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    })
    return s


def get_account_id(s):
    if ACCOUNT_ID_ENV:
        return ACCOUNT_ID_ENV
    r = s.get(f"{REST_URL}/v3/accounts", timeout=30)
    r.raise_for_status()
    accounts = r.json().get("accounts", [])
    if not accounts:
        raise RuntimeError("No OANDA accounts returned")
    return accounts[0]["id"]

# ============================================================
# TIME CONTROL
# ============================================================
def parse_hhmm(v):
    h, m = v.split(":")
    return int(h), int(m)


def day_at(day, hhmm):
    h, m = parse_hhmm(hhmm)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)


def market_window(now=None):
    now = now or datetime.now(timezone.utc)
    wd = now.weekday()
    if wd == 5:  # Saturday
        return None, None
    if wd == 6:  # Sunday before weekly open
        op = day_at(now.date(), MARKET_OPEN_UTC)
        if now < op:
            return None, None
        cl = day_at(now.date() + timedelta(days=1), MARKET_CLOSE_UTC)
        return op, cl
    op = day_at(now.date() - timedelta(days=1), MARKET_OPEN_UTC)
    cl = day_at(now.date(), MARKET_CLOSE_UTC)
    return op, cl


def trading_state(now=None):
    now = now or datetime.now(timezone.utc)
    op, cl = market_window(now)
    if op is None or cl is None:
        return "WEEKEND_CLOSED"
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
    lu = int(float(position.get("long", {}).get("units", "0")))
    su = int(float(position.get("short", {}).get("units", "0")))
    return lu + su


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
    r = s.put(
        f"{REST_URL}/v3/accounts/{account_id}/positions/{instrument}/close",
        json=payload,
        timeout=20,
    )
    r.raise_for_status()
    print(f"[POSITION CLOSED] {instrument}", flush=True)
    return r.json()


def close_all_positions(s, account_id):
    try:
        positions = get_open_positions(s, account_id)
    except Exception as e:
        print("[FORCE CLOSE READ ERROR]", repr(e), flush=True)
        return
    instruments = [
        p.get("instrument") for p in positions
        if p.get("instrument") and position_units(p) != 0
    ]
    if not instruments:
        print("[FLAT] No open positions", flush=True)
        return
    print("[FORCE CLOSE] Final-hour protection", flush=True)
    for instrument in sorted(set(instruments)):
        try:
            close_instrument(s, account_id, instrument)
        except Exception as e:
            print("[CLOSE ERROR]", instrument, repr(e), flush=True)


def execute_signal(s, account_id, instrument, side, strategy_name):
    side = side.upper()
    if trading_state() != "TRADING":
        print(
            f"[SIGNAL BLOCKED] {instrument} {side} "
            f"state={trading_state()} engine={strategy_name}",
            flush=True,
        )
        return None
    units = abs(DEFAULT_UNITS)
    if units == 0:
        return None

    pos = get_position(s, account_id, instrument)
    current = position_units(pos)
    if current:
        current_side = "BUY" if current > 0 else "SELL"
        if current_side != side:
            close_instrument(s, account_id, instrument)
            time.sleep(0.5)
        else:
            print(f"[DUPLICATE BLOCKED] {instrument} {side} {strategy_name}", flush=True)
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
                "comment": "OANDA multi-timeframe demo",
            },
        }
    }
    if not LIVE_TRADING_ENABLED:
        print(
            f"[DRY-RUN ORDER] {instrument} {side} units={units} engine={strategy_name}",
            flush=True,
        )
        return {"dry_run": True, **payload}
    r = s.post(f"{REST_URL}/v3/accounts/{account_id}/orders", json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    print("[ORDER ACCEPTED]", json.dumps(data), flush=True)
    return data

# ============================================================
# CANDLES / HEIKEN ASHI
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
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna().reset_index()
    return out


def session_filter(df, session_name):
    if df.empty:
        return df.copy()
    a, b = SESSIONS[session_name]
    x = df.copy()
    x["datetime"] = pd.to_datetime(x["datetime"], utc=True)
    ist = x["datetime"].dt.tz_convert("Asia/Kolkata")
    st = pd.Timestamp(a).time()
    en = pd.Timestamp(b).time()
    return x[(ist.dt.dayofweek < 5) & (ist.dt.time >= st) & (ist.dt.time < en)].copy()


def prepare_v2(raw, tf, session_name):
    # Raw M1 -> IST session filter -> TF resample -> HA
    return add_ha(resample_tf(session_filter(raw, session_name), tf))


def prepare_v3(raw, tf, session_name):
    # Raw M1 -> TF resample -> continuous HA -> IST session filter
    return session_filter(add_ha(resample_tf(raw, tf)), session_name)

# ============================================================
# FROZEN 57-59 NORMAL MECHANICS
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
                trades.append({
                    "entry_time": entry_time, "exit_time": c0.datetime,
                    "side": "SELL", "entry": entry, "exit": c0.ha_close,
                    "reason": "NEW_BUY_SIGNAL",
                })
            position, entry, entry_time, reference = "BUY", c0.ha_close, c0.datetime, c1.ha_low
        elif sell_signal:
            if position == "BUY":
                trades.append({
                    "entry_time": entry_time, "exit_time": c0.datetime,
                    "side": "BUY", "entry": entry, "exit": c0.ha_close,
                    "reason": "NEW_SELL_SIGNAL",
                })
            position, entry, entry_time, reference = "SELL", c0.ha_close, c0.datetime, c1.ha_high

        if position == "BUY":
            if c0.ha_low < reference:
                trades.append({
                    "entry_time": entry_time, "exit_time": c0.datetime,
                    "side": "BUY", "entry": entry, "exit": reference,
                    "reason": "LOW_BREAK",
                })
                position, entry, entry_time, reference = "SELL", reference, c0.datetime, c0.ha_high
            else:
                reference = c0.ha_low
        elif position == "SELL":
            if c0.ha_high > reference:
                trades.append({
                    "entry_time": entry_time, "exit_time": c0.datetime,
                    "side": "SELL", "entry": entry, "exit": reference,
                    "reason": "HIGH_BREAK",
                })
                position, entry, entry_time, reference = "BUY", reference, c0.datetime, c0.ha_low
            else:
                reference = c0.ha_high

    if position is not None:
        last = rows.iloc[-1]
        trades.append({
            "entry_time": entry_time, "exit_time": last.datetime,
            "side": position, "entry": entry, "exit": last.ha_close,
            "reason": "END_OF_DATA",
        })
    return pd.DataFrame(trades)

# ============================================================
# 57-59 OPPOSITE
# Same frozen signal framework with execution direction inverted.
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


def true_reverse_completed_stream(trades):
    if trades.empty:
        return trades.copy()
    x = trades.copy()
    x["side"] = x["side"].map({"BUY": "SELL", "SELL": "BUY"})
    if "gross" in x.columns:
        x["gross"] = -x["gross"]
    if "net" in x.columns:
        x["net"] = -x["net"]
    x["reason"] = "TRUE_REVERSE"
    return x


def add_pnl(trades, market):
    if trades.empty:
        return trades.copy()
    x = trades.copy()
    x["gross"] = np.where(
        x["side"].eq("BUY"),
        x["exit"] - x["entry"],
        x["entry"] - x["exit"],
    )
    x["spread_cost"] = PIP_SIZE.get(market, 0.0001) * SPREAD_PIPS
    x["net"] = x["gross"] - x["spread_cost"]
    return x

# ============================================================
# THREE WEEK-DEMO STRATEGY ADAPTERS
# ============================================================
def strategy_trades(raw, market, strategy, tf, session_name):
    strategy = strategy.upper()
    if strategy == "A_TRUE_REVERSE":
        prepared = prepare_v2(raw, tf, session_name)
        return true_reverse_completed_stream(add_pnl(run_5759_opposite(prepared), market))
    if strategy == "B_TRUE_REVERSE":
        prepared = prepare_v2(raw, tf, session_name)
        a = add_pnl(run_5759_opposite(prepared), market)
        return true_reverse_completed_stream(a)
    if strategy == "V3_OPPOSITE":
        prepared = prepare_v3(raw, tf, session_name)
        return add_pnl(run_5759_opposite(prepared), market)
    raise ValueError(f"Unknown live strategy: {strategy}")


def strategy_trades_full_day(raw, market, strategy, tf):
    strategy = strategy.upper()
    prepared = add_ha(resample_tf(raw, tf))
    opposite = add_pnl(run_5759_opposite(prepared), market)
    if strategy in ("A_TRUE_REVERSE", "B_TRUE_REVERSE"):
        return true_reverse_completed_stream(opposite)
    if strategy == "V3_OPPOSITE":
        return opposite
    raise ValueError(f"Unknown live strategy: {strategy}")


# ============================================================
# ENGINE HELPERS
# ============================================================
def engine_trades(raw, market, engine, tf, session_name):
    if engine == "V2":
        prepared = prepare_v2(raw, tf, session_name)
        return add_pnl(run_frozen_mechanics(prepared), market)
    if engine == "V3" or engine == "57-59_NORMAL":
        prepared = prepare_v3(raw, tf, session_name)
        return add_pnl(run_frozen_mechanics(prepared), market)
    if engine == "57-59_OPPOSITE":
        prepared = prepare_v3(raw, tf, session_name)
        return add_pnl(run_5759_opposite(prepared), market)
    if engine == "57-59_TRUE_REVERSE":
        prepared = prepare_v3(raw, tf, session_name)
        opposite = add_pnl(run_5759_opposite(prepared), market)
        return true_reverse_completed_stream(opposite)
    raise ValueError(f"Unknown engine: {engine}")


def latest_entry_signal(raw, engine, tf, session_name):
    """Replay the selected engine and return the newest completed entry/reversal event."""
    trades = engine_trades(raw, "EURUSD", engine, tf, session_name)
    if trades.empty:
        return None
    last = trades.iloc[-1]
    return {
        "side": str(last["side"]),
        "time": pd.Timestamp(last["entry_time"]),
        "reason": str(last.get("reason", "SIGNAL")),
    }

# ============================================================
# LIVE ENGINE STATE
# ============================================================
class LiveMarketState:
    def __init__(self):
        self.raw = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        self.last_processed = {}
        self.last_signature = {}
        self.trade_signatures = set()
        self.performance_file = OUTPUT_DIR / "WEEK_DEMO_LIVE_TRADES.csv"
        self.performance_file.parent.mkdir(parents=True, exist_ok=True)

    def append(self, candle):
        row = pd.DataFrame([candle])
        self.raw = pd.concat([self.raw, row], ignore_index=True)
        self.raw["datetime"] = pd.to_datetime(self.raw["datetime"], utc=True)
        self.raw = self.raw.drop_duplicates("datetime").sort_values("datetime").tail(2500).reset_index(drop=True)


def current_session_name(ts_utc):
    ts = pd.Timestamp(ts_utc)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ist = ts.tz_convert("Asia/Kolkata")
    tm = ist.time()
    for name, (a, b) in SESSIONS.items():
        if pd.Timestamp(a).time() <= tm < pd.Timestamp(b).time():
            return name
    return None


def tf_bucket_closed(ts, tf):
    ts = pd.Timestamp(ts)
    return ts.minute % tf == 0


def _event_signature(row, market, strategy, tf, scope):
    return (
        market, strategy, int(tf), scope,
        str(row.get("entry_time", "")),
        str(row.get("exit_time", "")),
        str(row.get("side", "")),
        str(row.get("reason", "")),
    )


def _append_performance(market, strategy, tf, scope, session_name, trades, bucket_start, state):
    """Persist newly completed virtual trades for the one-week comparison.

    This ledger is independent for every strategy/market/TF/scope, so an
    OANDA account-level position cannot contaminate the research comparison.
    """
    if trades.empty:
        return
    x = trades.copy()
    x["entry_time"] = pd.to_datetime(x["entry_time"], utc=True)
    x["exit_time"] = pd.to_datetime(x["exit_time"], utc=True)
    x = x[x["exit_time"] < pd.Timestamp(bucket_start)].copy()
    if x.empty:
        return

    rows = []
    for _, tr in x.iterrows():
        sig = _event_signature(tr, market, strategy, tf, scope)
        if sig in state.trade_signatures:
            continue
        state.trade_signatures.add(sig)
        rows.append({
            "market": market,
            "strategy": strategy,
            "scope": scope,
            "session": session_name if scope == "SESSION" else "FULL_DAY",
            "tf_minutes": int(tf),
            "entry_time": tr["entry_time"].isoformat(),
            "exit_time": tr["exit_time"].isoformat(),
            "side": str(tr["side"]),
            "entry": float(tr["entry"]),
            "exit": float(tr["exit"]),
            "gross": float(tr.get("gross", 0.0)),
            "spread_cost": float(tr.get("spread_cost", 0.0)),
            "net": float(tr.get("net", 0.0)),
            "reason": str(tr.get("reason", "")),
        })
    if not rows:
        return
    out = pd.DataFrame(rows)
    header = not state.performance_file.exists()
    out.to_csv(state.performance_file, mode="a", header=header, index=False)
    for r in rows:
        print(
            f"[WEEK-DEMO TRADE] {r['market']} {r['strategy']} "
            f"{r['scope']} TF={r['tf_minutes']} {r['session']} "
            f"{r['side']} NET={r['net']:.6f} reason={r['reason']}",
            flush=True,
        )


def _process_one_system(market, state, raw, strategy, tf, scope, session_name, bucket_start, s, account_id):
    if scope == "SESSION":
        prepared = (
            prepare_v2(raw, tf, session_name)
            if strategy in ("A_TRUE_REVERSE", "B_TRUE_REVERSE")
            else prepare_v3(raw, tf, session_name)
        )
    else:
        prepared = (
            add_ha(resample_tf(raw, tf))
            if strategy in ("A_TRUE_REVERSE", "B_TRUE_REVERSE")
            else add_ha(resample_tf(raw, tf))
        )

    if len(prepared) < 3:
        return

    bucket_start = pd.Timestamp(bucket_start)
    completed = prepared[
        pd.to_datetime(prepared["datetime"], utc=True) < bucket_start
    ].reset_index(drop=True)
    if len(completed) < 3:
        return

    signature = pd.Timestamp(completed.iloc[-1]["datetime"])
    key = (strategy, scope, tf, session_name if scope == "SESSION" else "FULL_DAY")
    if state.last_processed.get(key) == signature:
        return
    state.last_processed[key] = signature

    trades = strategy_trades(
        raw, market, strategy, tf,
        session_name if scope == "SESSION" else None,
    ) if scope == "SESSION" else strategy_trades_full_day(raw, market, strategy, tf)
    if trades.empty:
        return

    # Record all trades that have actually closed by this boundary.
    _append_performance(
        market, strategy, tf, scope, session_name,
        trades, bucket_start, state,
    )

    # A new entry is only a live signal when the newest completed strategy bar
    # is the trade's entry bar. Never treat END_OF_DATA as an entry signal.
    candidates = trades.copy()
    candidates["entry_time"] = pd.to_datetime(candidates["entry_time"], utc=True)
    candidates = candidates[candidates["entry_time"] == signature]
    candidates = candidates[candidates["reason"].astype(str) != "END_OF_DATA"]
    if candidates.empty:
        return

    last = candidates.iloc[-1]
    event_key = (
        strategy, scope, tf, session_name if scope == "SESSION" else "FULL_DAY",
        str(last["entry_time"]), str(last["side"]), str(last.get("reason", "")),
    )
    if state.last_signature.get(key) == event_key:
        return
    state.last_signature[key] = event_key

    side = str(last["side"]).upper()
    print(
        f"[3-STRATEGY SIGNAL] {market} {strategy} {scope} "
        f"TF={tf} {session_name if scope == 'SESSION' else 'FULL_DAY'} "
        f"{side} entry_time={signature} reason={last.get('reason', '')}",
        flush=True,
    )

    # All requested systems are monitored. When order execution is enabled,
    # each signal may request a Practice order. The OANDA account remains a
    # shared account, so the independent comparison is always the CSV ledger.
    if LIVE_EXECUTION_ENGINE == "ALL3" and strategy in LIVE_STRATEGIES and LIVE_TRADING_ENABLED:
        execute_signal(s, account_id, MARKETS[market], side, strategy)


def process_live_market(market, state, closed_m1, boundary_ts, s, account_id):
    """Process ALL requested session and full-day systems from the M1 stream."""
    state.append(closed_m1)
    raw = state.raw
    session = current_session_name(closed_m1["datetime"])

    # Session systems: all 1M-15M for the currently active S1/S2/S3.
    if session is not None:
        for strategy in LIVE_STRATEGIES:
            for tf in SESSION_TFS:
                if LIVE_TF and tf != LIVE_TF:
                    continue
                if not tf_bucket_closed(pd.Timestamp(boundary_ts), tf):
                    continue
                _process_one_system(
                    market, state, raw, strategy, tf, "SESSION", session,
                    boundary_ts, s, account_id,
                )

    # Full-day systems: 1M, 3M, 5M, 15M, 30M, 1H, with no session filter.
    for strategy in LIVE_STRATEGIES:
        for tf in FULLDAY_LIVE_TFS:
            if not tf_bucket_closed(pd.Timestamp(boundary_ts), tf):
                continue
            _process_one_system(
                market, state, raw, strategy, tf, "FULL_DAY", None,
                boundary_ts, s, account_id,
            )

# ============================================================
# LIVE OANDA M1 STREAM
# ============================================================
def stream_live(s, account_id):
    instruments = ",".join(MARKETS.values())
    url = f"{STREAM_URL}/v3/accounts/{account_id}/pricing/stream"
    params = {"instruments": instruments}
    states = {m: LiveMarketState() for m in MARKETS}
    current = {}

    print("=" * 100)
    print("OANDA 15-MARKET MULTI-TIMEFRAME MASTER")
    print("Markets:", len(MARKETS))
    print("Session TFs:", "1M-15M")
    print("Daily TFs:", "1M,3M,5M,15M,30M,1H")
    print("Engines:", ", ".join(ENGINES))
    print("Monitor all requested systems: True")
    print("Week-demo execution:", LIVE_EXECUTION_ENGINE, "ALL SESSION TFs=1-15", "FULL-DAY TFs=1,3,5,15,30,60")
    print("Orders:", "ENABLED (PRACTICE)" if LIVE_TRADING_ENABLED else "DRY-RUN")
    print("Time state:", trading_state())
    print("=" * 100)

    last_state = None
    while True:
        try:
            state_now = trading_state()
            if state_now != last_state:
                print("[TIME STATE]", state_now, flush=True)
                last_state = state_now
            if state_now == "CLOSE_PROTECTION":
                close_all_positions(s, account_id)

            with s.get(url, params=params, stream=True, timeout=(30, 90)) as r:
                r.raise_for_status()
                for raw_line in r.iter_lines():
                    if not raw_line:
                        continue
                    msg = json.loads(raw_line.decode("utf-8"))
                    if msg.get("type") == "HEARTBEAT":
                        print("[HEARTBEAT]", msg.get("time"), "state=", trading_state(), flush=True)
                        if trading_state() == "CLOSE_PROTECTION":
                            close_all_positions(s, account_id)
                        continue
                    if msg.get("type") != "PRICE":
                        continue

                    instrument = msg.get("instrument")
                    bids = msg.get("bids") or []
                    asks = msg.get("asks") or []
                    if not bids or not asks or instrument not in MARKETS.values():
                        continue

                    bid = float(bids[0]["price"])
                    ask = float(asks[0]["price"])
                    mid = (bid + ask) / 2.0
                    ts = pd.Timestamp(msg["time"], tz="UTC")
                    bucket = ts.floor("min")
                    state = current.get(instrument)
                    if state is None:
                        current[instrument] = {
                            "bucket": bucket, "open": mid, "high": mid,
                            "low": mid, "close": mid, "volume": 1,
                        }
                        continue
                    if bucket == state["bucket"]:
                        state["high"] = max(state["high"], mid)
                        state["low"] = min(state["low"], mid)
                        state["close"] = mid
                        state["volume"] += 1
                        continue

                    closed = dict(state)
                    closed["datetime"] = closed.pop("bucket")
                    market = next(k for k, v in MARKETS.items() if v == instrument)
                    print(
                        f"[M1 CLOSED] {market} {closed['datetime']} "
                        f"O={closed['open']} H={closed['high']} "
                        f"L={closed['low']} C={closed['close']}",
                        flush=True,
                    )
                    current[instrument] = {
                        "bucket": bucket, "open": mid, "high": mid,
                        "low": mid, "close": mid, "volume": 1,
                    }

                    # Weekend: never process live strategy signals.
                    if pd.Timestamp(closed["datetime"]).tz_convert("Asia/Kolkata").dayofweek >= 5:
                        continue

                    # Time protection applies to execution. Data can still be built.
                    process_live_market(market, states[market], closed, bucket, s, account_id)

        except Exception as e:
            print("[STREAM ERROR]", type(e).__name__, str(e), flush=True)
            time.sleep(5)

# ============================================================
# HISTORICAL FULL-DAY RESEARCH
# ============================================================
def resolve_test_date():
    v = os.getenv("TEST_DATE", "").strip()
    d = pd.Timestamp(v, tz="UTC") if v else pd.Timestamp.now(tz="UTC")
    d = d.normalize()
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d


def download_oanda_m1(s, instrument, day):
    start = pd.Timestamp(day, tz="UTC")
    end = start + pd.Timedelta(days=1)
    rows = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + pd.Timedelta(hours=6), end)
        params = {
            "granularity": "M1",
            "price": "M",
            "from": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "smooth": "false",
        }
        r = s.get(f"{REST_URL}/v3/instruments/{instrument}/candles", params=params, timeout=60)
        r.raise_for_status()
        for c in r.json().get("candles", []):
            if not c.get("complete") or not c.get("mid"):
                continue
            m = c["mid"]
            rows.append({
                "datetime": c["time"],
                "open": float(m["o"]), "high": float(m["h"]),
                "low": float(m["l"]), "close": float(m["c"]),
                "volume": int(c.get("volume", 0)),
            })
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
    return {
        "Trades": len(v),
        "Wins": int((v > 0).sum()),
        "Losses": int((v < 0).sum()),
        "WR": float((v > 0).mean() * 100),
        "PF": gp / gl if gl else math.inf,
        "Net": float(v.sum()),
        "MaxDD": float((peak - eq).max()),
    }


def run_daily_research(s):
    day = resolve_test_date()
    print("[FULL-DAY TEST]", day.date(), "weekend skipped automatically")
    all_results = []
    all_trades = []

    for market, instrument in MARKETS.items():
        print(f"[DATA] {market} {instrument}", flush=True)
        raw = download_oanda_m1(s, instrument, day)
        raw.to_csv(OUTPUT_DIR / f"{market}_{day.date()}_M1.csv", index=False)
        if raw.empty:
            print("[NO DATA]", market, flush=True)
            continue

        for tf in DAILY_TFS:
            for engine in ENGINES:
                # Full-day means no S1/S2/S3 restriction here.
                if engine == "V2":
                    prepared = add_ha(resample_tf(raw, tf))
                    trades = run_frozen_mechanics(prepared)
                elif engine in ("V3", "57-59_NORMAL"):
                    prepared = add_ha(resample_tf(raw, tf))
                    trades = run_frozen_mechanics(prepared)
                elif engine == "57-59_OPPOSITE":
                    prepared = add_ha(resample_tf(raw, tf))
                    trades = run_5759_opposite(prepared)
                else:
                    prepared = add_ha(resample_tf(raw, tf))
                    opposite = add_pnl(run_5759_opposite(prepared), market)
                    trades = true_reverse_completed_stream(opposite)

                trades = add_pnl(trades, market) if engine != "57-59_TRUE_REVERSE" else trades
                m = metrics(trades)
                all_results.append({
                    "Market": market, "TF": tf, "Engine": engine, **m,
                })
                if not trades.empty:
                    z = trades.copy()
                    z.insert(0, "Market", market)
                    z.insert(1, "TF", tf)
                    z.insert(2, "Engine", engine)
                    all_trades.append(z)

    results = pd.DataFrame(all_results)
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    results.to_csv(OUTPUT_DIR / f"OANDA_15MARKET_FULLDAY_RESULTS_{day.date()}.csv", index=False)
    trades.to_csv(OUTPUT_DIR / f"OANDA_15MARKET_FULLDAY_TRADES_{day.date()}.csv", index=False)
    print("[FULL-DAY RESEARCH COMPLETE]", day.date(), flush=True)
    if not results.empty:
        print(results.sort_values(["Engine", "PF"], ascending=[True, False]).head(100).to_string(index=False), flush=True)

# ============================================================
# FROZEN MFP RUNNER
# ============================================================
def run_mfp_frozen_exact():
    frozen = Path(MFP_FROZEN_FILE).resolve()
    candidate = Path(MFP_CANDIDATE_FILE).resolve()
    print("MFP frozen source:", frozen)
    print("MFP candidate:", candidate)
    if not frozen.exists():
        print("[MFP] NOT RUN: frozen source missing")
        return 1
    if not candidate.exists():
        print("[MFP] NOT RUN: candidate CSV missing")
        return 2
    return subprocess.run(
        [sys.executable, str(frozen)],
        cwd=str(frozen.parent),
        env=os.environ.copy(),
        check=False,
    ).returncode

# ============================================================
# STATUS
# ============================================================
def print_status(s, account_id):
    print("=" * 100)
    print("OANDA MASTER STATUS")
    print("Practice REST:", REST_URL)
    print("Account:", account_id)
    print("Markets:", len(MARKETS))
    print("Session TFs:", SESSION_TFS)
    print("Daily TFs:", DAILY_TFS)
    print("Sessions:", SESSIONS)
    print("Engines:", ENGINES)
    print("Selected execution:", LIVE_EXECUTION_ENGINE, LIVE_TF, LIVE_SESSION)
    print("Monitor all live:", MONITOR_ALL_LIVE)
    print("Market open UTC:", MARKET_OPEN_UTC, "+", OPEN_DELAY_HOURS, "h")
    print("Market close UTC:", MARKET_CLOSE_UTC, "-", CLOSE_BEFORE_HOURS, "h")
    print("Trading state:", trading_state())
    print("Live orders:", LIVE_TRADING_ENABLED)
    print("MFP frozen:", MFP_FROZEN_FILE)
    print("=" * 100)

# ============================================================
# MAIN
# ============================================================
def main():
    s = get_http_session()
    account_id = get_account_id(s)

    print("=" * 100)
    print("OANDA 15-MARKET MULTI-TIMEFRAME MASTER")
    print("Mode:", MASTER_MODE)
    print("Account:", account_id)
    print("Practice:", REST_URL)
    print("Markets:", ", ".join(MARKETS.values()))
    print("Session TFs: 1M through 15M")
    print("Full-day TFs: 1M, 3M, 5M, 15M, 30M, 1H")
    print("Engines:", ", ".join(ENGINES))
    print("Orders:", "ENABLED (PRACTICE)" if LIVE_TRADING_ENABLED else "DRY-RUN")
    print("Time state:", trading_state())
    print("=" * 100)

    if MASTER_MODE == "LIVE":
        stream_live(s, account_id)
    elif MASTER_MODE == "DAILY_RESEARCH":
        run_daily_research(s)
    elif MASTER_MODE == "MFP":
        raise SystemExit(run_mfp_frozen_exact())
    elif MASTER_MODE == "STATUS":
        print_status(s, account_id)
    else:
        raise ValueError("MASTER_MODE must be LIVE, DAILY_RESEARCH, MFP, or STATUS")


if __name__ == "__main__":
    main()

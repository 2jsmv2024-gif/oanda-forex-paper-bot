#!/usr/bin/env python3
"""
OANDA 15-MARKET / MULTI-TIMEFRAME MASTER

ONE CODE, NINE STRATEGY FAMILIES + FROZEN MFP REFERENCE

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
  - one selected live execution combination at a time
  - all other engines/timeframes are monitored and logged
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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import requests

# ============================================================
# OANDA ORDER THROTTLE / RETRY PROTECTION
# ============================================================
# Keep all strategy signals intact; only serialize broker POST requests.
ORDER_MIN_INTERVAL_SEC = float(os.getenv("OANDA_ORDER_MIN_INTERVAL_SEC", "0.75"))
ORDER_MAX_RETRIES = int(os.getenv("OANDA_ORDER_MAX_RETRIES", "5"))
ORDER_BACKOFF_BASE_SEC = float(os.getenv("OANDA_ORDER_BACKOFF_BASE_SEC", "1.0"))
_LAST_ORDER_REQUEST_MONOTONIC = 0.0

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
LIVE_EXECUTION_ENGINE = os.getenv("LIVE_EXECUTION_ENGINE", "57-59_NORMAL").strip().upper()
LIVE_TF = int(os.getenv("LIVE_TF", "15"))
LIVE_SESSION = os.getenv("LIVE_SESSION", "AUTO").strip().upper()

# Monitor all requested live engines/timeframes by default.
MONITOR_ALL_LIVE = os.getenv("MONITOR_ALL_LIVE", "true").strip().lower() == "true"
MFP_FROZEN_ENABLED = os.getenv("MFP_FROZEN_ENABLED", "true").strip().lower() == "true"

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

SIGNAL_ENGINES = [
    "A",
    "B",
    "V2",
    "V3_NORMAL",
    "V3_OPPOSITE",
    "57-59_NORMAL",
    "57-59_OPPOSITE",
    "57-59_TRUE_REVERSE",
]

ENGINES = SIGNAL_ENGINES[:]

# ============================================================
# MT5 FX SIGNAL FEED
# Pair+engine PF rule:
#   PF > 1.65  -> execute original direction
#   PF < 0.65  -> execute reversed direction
#   0.65..1.65 -> no MT5 signal
# This feed is additive only: it does NOT change the OANDA strategy
# execution below.
# ============================================================
MT5_FEED_ENABLED = os.getenv("MT5_FEED_ENABLED", "true").strip().lower() == "true"
MT5_FEED_PORT = int(os.getenv("PORT", "8080"))
_MT5_SIGNAL_LOCK = threading.Lock()
_MT5_SIGNAL_SEQ = 0
_MT5_SIGNAL_QUEUE = []
_MT5_SIGNAL_QUEUE_MAX = 500

_MT5_HIGH_PF = {
    ("EURUSD", "V2"),
    ("EURAUD", "A"), ("EURAUD", "B"), ("EURAUD", "V2"), ("EURAUD", "V3"),
    ("EURCAD", "57-59"), ("EURCAD", "A"), ("EURCAD", "B"), ("EURCAD", "V2"), ("EURCAD", "V3"),
    ("GBPAUD", "57-59"), ("GBPAUD", "A"), ("GBPAUD", "B"), ("GBPAUD", "V2"), ("GBPAUD", "V3"),
    ("USDJPY", "A"), ("USDJPY", "B"),
}

_MT5_LOW_PF = {
    ("EURUSD", "A"), ("EURUSD", "B"),
    ("EURNZD", "57-59"), ("EURNZD", "A"), ("EURNZD", "B"), ("EURNZD", "V2"), ("EURNZD", "V3"),
    ("EURJPY", "V3"), ("EURJPY", "57-59"), ("EURJPY", "A"), ("EURJPY", "B"),
    ("EURCHF", "A"), ("EURCHF", "B"), ("EURCHF", "V2"), ("EURCHF", "V3"), ("EURCHF", "57-59"),
    ("GBPUSD", "57-59"), ("GBPUSD", "V3"),
    ("GBPCAD", "B"), ("GBPCAD", "V3"), ("GBPCAD", "57-59"), ("GBPCAD", "V2"),
    ("GBPJPY", "57-59"), ("GBPJPY", "A"), ("GBPJPY", "B"), ("GBPJPY", "V2"), ("GBPJPY", "V3"),
    ("GBPNZD", "V2"), ("GBPNZD", "V3"), ("GBPNZD", "57-59"), ("GBPNZD", "A"), ("GBPNZD", "B"),
}

def _mt5_engine_bucket(engine):
    return {
        "V3_NORMAL": "V3",
        "57-59_NORMAL": "57-59",
    }.get(engine, engine)

def _mt5_pf_action(market, engine):
    key = (market, _mt5_engine_bucket(engine))
    if key in _MT5_HIGH_PF:
        return "AS_IS", False
    if key in _MT5_LOW_PF:
        return "REVERSE", True
    return "", False

def publish_mt5_signal(market, instrument, engine, tf, session, event):
    global _MT5_SIGNAL_SEQ
    action, reverse = _mt5_pf_action(market, engine)
    if not action:
        return

    original_side = str(event["side"]).upper()
    final_side = ("SELL" if original_side == "BUY" else "BUY") if reverse else original_side
    entry_time = pd.Timestamp(event["entry_time"]).isoformat()
    reason = str(event.get("reason", "SIGNAL"))
    signal_price = float(event.get("entry", 0.0) or 0.0)

    with _MT5_SIGNAL_LOCK:
        _MT5_SIGNAL_SEQ += 1
        seq = _MT5_SIGNAL_SEQ
        payload = {
            "seq": seq,
            "id": f"MT5-{seq}-{market}-{_mt5_engine_bucket(engine)}-{tf}-{entry_time}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal_time": entry_time,
            "market": market,
            "instrument": instrument,
            "mt5_symbol": market,
            "engine": _mt5_engine_bucket(engine),
            "engine_source": engine,
            "tf": int(tf),
            "session": session,
            "original_side": original_side,
            "side": final_side,
            "pf_action": action,
            "reason": reason,
            "price": signal_price,
        }
        _MT5_SIGNAL_QUEUE.append(payload)
        if len(_MT5_SIGNAL_QUEUE) > _MT5_SIGNAL_QUEUE_MAX:
            del _MT5_SIGNAL_QUEUE[:-_MT5_SIGNAL_QUEUE_MAX]

    print(
        f"[MT5 FEED] seq={seq} {market} {_mt5_engine_bucket(engine)} "
        f"TF={tf} {session} {original_side}->{final_side} action={action}",
        flush=True,
    )

class _MT5FeedHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code, body, content_type="application/json"):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == "/mt5/health":
            with _MT5_SIGNAL_LOCK:
                count = len(_MT5_SIGNAL_QUEUE)
                seq = _MT5_SIGNAL_SEQ
            self._send(200, json.dumps({
                "ok": True,
                "service": "OANDA 15-MARKET MT5 PF FEED",
                "signal_count": count,
                "latest_seq": seq,
            }, separators=(",", ":")))
            return

        if path.path == "/mt5/signals":
            try:
                after = int(parse_qs(path.query).get("after", ["0"])[0])
            except (TypeError, ValueError):
                after = 0
            with _MT5_SIGNAL_LOCK:
                rows = [x for x in _MT5_SIGNAL_QUEUE if int(x["seq"]) > after]
            body = "\n".join(json.dumps(x, separators=(",", ":")) for x in rows)
            self._send(200, body, "application/x-ndjson")
            return

        self._send(404, json.dumps({"ok": False, "error": "not_found"}))

def start_mt5_feed_server():
    if not MT5_FEED_ENABLED:
        print("[MT5 FEED] disabled", flush=True)
        return None
    server = ThreadingHTTPServer(("0.0.0.0", MT5_FEED_PORT), _MT5FeedHandler)
    thread = threading.Thread(target=server.serve_forever, name="mt5-feed", daemon=True)
    thread.start()
    print(f"[MT5 FEED] listening port={MT5_FEED_PORT} paths=/mt5/health,/mt5/signals", flush=True)
    return server

# Nine requested components. MFP_FROZEN is the frozen portfolio layer,
# not an additional independent signal generator.
STRATEGY_COMPONENTS = SIGNAL_ENGINES[:] + ["MFP_FROZEN"]

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


def _wait_for_order_slot():
    global _LAST_ORDER_REQUEST_MONOTONIC
    now = time.monotonic()
    wait = ORDER_MIN_INTERVAL_SEC - (now - _LAST_ORDER_REQUEST_MONOTONIC)
    if wait > 0:
        time.sleep(wait)
    _LAST_ORDER_REQUEST_MONOTONIC = time.monotonic()


def _safe_order_error_body(response):
    try:
        data = response.json()
        if isinstance(data, dict):
            return json.dumps(data, separators=(",", ":"))[:2000]
    except Exception:
        pass
    return (response.text or "")[:2000]


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

    # ALL-ENGINE MODE: every qualifying signal remains eligible for execution.
    # Broker requests are throttled so simultaneous signals do not burst into
    # OANDA and trigger 429 rate limiting.
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

    last_error = None
    for attempt in range(1, ORDER_MAX_RETRIES + 1):
        _wait_for_order_slot()
        try:
            r = s.post(
                f"{REST_URL}/v3/accounts/{account_id}/orders",
                json=payload,
                timeout=20,
            )
        except requests.RequestException as e:
            # Do not blindly replay an ambiguous timeout: OANDA may already
            # have accepted the order before the client lost the response.
            print(
                f"[ORDER TRANSPORT ERROR] {instrument} {side} "
                f"engine={strategy_name} attempt={attempt}/{ORDER_MAX_RETRIES}: {e}",
                flush=True,
            )
            return None

        if r.ok:
            data = r.json()
            print("[ORDER ACCEPTED]", json.dumps(data), flush=True)
            return data

        body = _safe_order_error_body(r)
        if r.status_code == 429 or 500 <= r.status_code <= 599:
            retry_after = r.headers.get("Retry-After", "")
            try:
                retry_wait = float(retry_after)
            except (TypeError, ValueError):
                retry_wait = ORDER_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            retry_wait = min(max(retry_wait, ORDER_MIN_INTERVAL_SEC), 30.0)
            print(
                f"[ORDER RETRY] HTTP {r.status_code} {instrument} {side} "
                f"engine={strategy_name} attempt={attempt}/{ORDER_MAX_RETRIES} "
                f"wait={retry_wait:.2f}s body={body}",
                flush=True,
            )
            last_error = RuntimeError(f"HTTP {r.status_code}: {body}")
            if attempt < ORDER_MAX_RETRIES:
                time.sleep(retry_wait)
                continue
            break

        # Other 4xx responses are deterministic request/account errors.
        # Do not hammer OANDA with the same invalid request; log the exact
        # broker response so the cause is visible instead of a generic 400.
        print(
            f"[ORDER REJECTED] HTTP {r.status_code} {instrument} {side} "
            f"engine={strategy_name} body={body}",
            flush=True,
        )
        last_error = RuntimeError(f"HTTP {r.status_code}: {body}")
        break

    print(
        f"[ORDER FAILED] {instrument} {side} engine={strategy_name} "
        f"error={last_error}",
        flush=True,
    )
    return None

# ============================================================

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
# ENGINE HELPERS
# ============================================================
def engine_trades(raw, market, engine, tf, session_name):
    # A and B are kept as separate named components, using their supplied
    # official mechanics. The supplied A source is the completed 57-59
    # OPPOSITE stream followed by mathematical TRUE REVERSE; B applies the
    # same mathematical TRUE REVERSE to the completed OPPOSITE stream.
    # They therefore share the same underlying mechanics but remain
    # separately tagged in the nine-component master.
    original_engine = engine
    if engine == "A":
        prepared = prepare_v3(raw, tf, session_name)
        opposite = add_pnl(run_5759_opposite(prepared), market)
        return true_reverse_completed_stream(opposite)
    if engine == "B":
        prepared = prepare_v3(raw, tf, session_name)
        opposite = add_pnl(run_5759_opposite(prepared), market)
        return true_reverse_completed_stream(opposite)
    if engine == "V3_NORMAL":
        engine = "V3"
    if engine == "V2":
        prepared = prepare_v2(raw, tf, session_name)
        return add_pnl(run_frozen_mechanics(prepared), market)
    if engine == "V3" or engine == "57-59_NORMAL":
        prepared = prepare_v3(raw, tf, session_name)
        return add_pnl(run_frozen_mechanics(prepared), market)
    if engine == "V3_OPPOSITE":
        prepared = prepare_v3(raw, tf, session_name)
        return add_pnl(run_5759_opposite(prepared), market)
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
# LIVE MFP BRIDGE
# Frozen MFP portfolio rules; V2 mechanics remain unchanged.
# XAUUSD-only, with MFP trade IDs isolated from the other 8 engines.
# ============================================================
MFP_LIVE_ENABLED = os.getenv("MFP_LIVE_ENABLED", "true").strip().lower() == "true"
MFP_OANDA_UNITS = float(os.getenv("MFP_OANDA_UNITS", "1"))
MFP_SYSTEMS = {
    "S1": {11: "T1", 7: "T2", 13: "T3"},
    "S2": {15: "T1", 14: "T2", 13: "T3"},
    "S3": {15: "T1", 13: "T2", 14: "T3"},
}

def _mfp_allowed_second(first_priority, incoming_priority):
    if first_priority == "T1":
        return incoming_priority in ("T2", "T3")
    if first_priority in ("T2", "T3"):
        return incoming_priority == "T1"
    return False

def _mfp_close_trade(s, account_id, trade_id, reason):
    if not trade_id:
        return None
    if not LIVE_TRADING_ENABLED:
        print(f"[MFP DRY-RUN CLOSE] trade={trade_id} reason={reason}", flush=True)
        return {"dry_run": True}
    _wait_for_order_slot()
    r = s.put(f"{REST_URL}/v3/accounts/{account_id}/trades/{trade_id}/close",
              json={"units": "ALL"}, timeout=20)
    if not r.ok:
        print(f"[MFP CLOSE REJECTED] HTTP {r.status_code} trade={trade_id} body={_safe_order_error_body(r)}", flush=True)
        return None
    data = r.json()
    print("[MFP TRADE CLOSED]", json.dumps(data), flush=True)
    return data

def _mfp_open_trade(s, account_id, side, session, priority, entry_time):
    if MFP_OANDA_UNITS <= 0:
        print("[MFP ORDER BLOCKED] MFP_OANDA_UNITS must be > 0", flush=True)
        return None
    signed = MFP_OANDA_UNITS if side == "BUY" else -MFP_OANDA_UNITS
    payload = {"order": {
        "type": "MARKET", "instrument": MARKETS["XAUUSD"], "units": str(signed),
        "timeInForce": "FOK", "positionFill": "DEFAULT",
        "clientExtensions": {"tag": f"MFP_{session}_{priority}"[:20],
                             "comment": "Frozen MFP live bridge"},
    }}
    if not LIVE_TRADING_ENABLED:
        print(f"[MFP DRY-RUN ORDER] XAUUSD {side} units={MFP_OANDA_UNITS} {session} {priority} entry={entry_time}", flush=True)
        return {"dry_run": True, "trade_id": None}
    last_error = None
    for attempt in range(1, ORDER_MAX_RETRIES + 1):
        _wait_for_order_slot()
        try:
            r = s.post(f"{REST_URL}/v3/accounts/{account_id}/orders", json=payload, timeout=20)
        except requests.RequestException as e:
            print(f"[MFP ORDER TRANSPORT ERROR] {e}", flush=True)
            return None
        if r.ok:
            data = r.json()
            fill = data.get("orderFillTransaction", {})
            trade_id = (fill.get("tradeOpened") or {}).get("tradeID")
            print("[MFP ORDER ACCEPTED]", json.dumps(data), flush=True)
            print(f"[MFP ORDER_FILL] trade_id={trade_id} session={session} priority={priority} side={side}", flush=True)
            return {"trade_id": trade_id, "response": data}
        body = _safe_order_error_body(r)
        if r.status_code == 429 or 500 <= r.status_code <= 599:
            retry_after = r.headers.get("Retry-After", "")
            try:
                wait = float(retry_after)
            except (TypeError, ValueError):
                wait = ORDER_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            wait = min(max(wait, ORDER_MIN_INTERVAL_SEC), 30.0)
            last_error = RuntimeError(f"HTTP {r.status_code}: {body}")
            print(f"[MFP ORDER RETRY] HTTP {r.status_code} attempt={attempt}/{ORDER_MAX_RETRIES} wait={wait:.2f}s body={body}", flush=True)
            if attempt < ORDER_MAX_RETRIES:
                time.sleep(wait)
                continue
            break
        print(f"[MFP ORDER REJECTED] HTTP {r.status_code} body={body}", flush=True)
        last_error = RuntimeError(f"HTTP {r.status_code}: {body}")
        break
    print(f"[MFP ORDER FAILED] {last_error}", flush=True)
    return None

def _mfp_candidate(raw, tf, session, bucket_start):
    prepared = prepare_v2(raw, tf, session)
    if len(prepared) < 3:
        return None
    completed = prepared[pd.to_datetime(prepared["datetime"], utc=True) < pd.Timestamp(bucket_start)].reset_index(drop=True)
    if len(completed) < 3:
        return None
    trades = run_frozen_mechanics(completed)
    candidates = trades[trades["reason"].astype(str) != "END_OF_DATA"] if not trades.empty else pd.DataFrame()
    return candidates.iloc[-1] if not candidates.empty else None

def process_live_mfp(state, closed_m1, boundary_ts, s, account_id):
    if not MFP_LIVE_ENABLED or not MFP_FROZEN_ENABLED or state.raw.empty:
        return
    session_full = current_session_name(closed_m1["datetime"])
    if session_full is None:
        return
    session = session_full.split("_", 1)[0]
    bucket_start = pd.Timestamp(boundary_ts)
    for tf, priority in MFP_SYSTEMS[session].items():
        if not tf_bucket_closed(bucket_start, tf):
            continue
        key = ("MFP", session, tf)
        candidate = _mfp_candidate(state.raw, tf, session_full, bucket_start)
        if candidate is None:
            continue
        entry_time = pd.Timestamp(candidate["entry_time"])
        side = str(candidate["side"]).upper()
        sig = (entry_time, side, priority)
        if state.mfp_last_signature.get(key) == sig:
            continue
        state.mfp_last_signature[key] = sig
        slots = state.mfp_slots[session]
        survivors = []
        for slot in slots:
            if pd.Timestamp(slot["candidate_exit_time"]) <= entry_time:
                _mfp_close_trade(s, account_id, slot.get("trade_id"), "NORMAL_EXIT")
            else:
                survivors.append(slot)
        slots[:] = survivors

        if slots and any(slot["side"] != side for slot in slots):
            if priority == "T3":
                print(f"[MFP REJECTED] XAUUSD {session} {priority} {side} reason=OPPOSITE_T3_REJECTED", flush=True)
                continue
            print(f"[MFP REVERSAL] XAUUSD {session} {priority} {side} closing {len(slots)} MFP slot(s)", flush=True)
            for slot in list(slots):
                _mfp_close_trade(s, account_id, slot.get("trade_id"), "OPPOSITE_T1_T2_REVERSAL")
            slots.clear()

        if not slots:
            result = _mfp_open_trade(s, account_id, side, session, priority, entry_time)
            if result is not None:
                slots.append({"priority": priority, "side": side, "entry_time": entry_time,
                              "candidate_exit_time": candidate["exit_time"], "trade_id": result.get("trade_id")})
            continue

        if any(slot["priority"] == priority for slot in slots):
            print(f"[MFP REJECTED] XAUUSD {session} {priority} {side} reason=DUPLICATE_PRIORITY", flush=True)
            continue
        if len(slots) >= 2:
            print(f"[MFP REJECTED] XAUUSD {session} {priority} {side} reason=TWO_SLOTS_FULL", flush=True)
            continue
        if _mfp_allowed_second(slots[0]["priority"], priority):
            result = _mfp_open_trade(s, account_id, side, session, priority, entry_time)
            if result is not None:
                slots.append({"priority": priority, "side": side, "entry_time": entry_time,
                              "candidate_exit_time": candidate["exit_time"], "trade_id": result.get("trade_id")})
        else:
            print(f"[MFP REJECTED] XAUUSD {session} {priority} {side} reason=PRIORITY_RULE", flush=True)

# ============================================================
# LIVE ENGINE STATE
# ============================================================
class LiveMarketState:
    def __init__(self):
        self.raw = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        self.last_processed = {}
        self.last_signature = {}
        self.mfp_last_signature = {}
        self.mfp_slots = {"S1": [], "S2": [], "S3": []}

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


def process_live_market(market, state, closed_m1, boundary_ts, s, account_id):
    state.append(closed_m1)
    raw = state.raw
    session = current_session_name(closed_m1["datetime"])
    if session is None:
        return

    engines = SIGNAL_ENGINES if MONITOR_ALL_LIVE else [LIVE_EXECUTION_ENGINE]
    tfs = SESSION_TFS if MONITOR_ALL_LIVE else [LIVE_TF]

    for engine in engines:
        for tf in tfs:
            # A TF closes when a new M1 candle begins at a TF boundary.
            if not tf_bucket_closed(pd.Timestamp(boundary_ts), tf):
                continue

            key = (engine, tf, session)
            prepared = prepare_v2(raw, tf, session) if engine == "V2" else prepare_v3(raw, tf, session)
            if len(prepared) < 3:
                continue

            # The just-closed M1 is at the start of the current TF bucket only
            # when its minute is a boundary. The latest completed TF is therefore
            # the final row at that boundary minus the currently forming bucket.
            # Use all fully completed buckets only.
            bucket_start = pd.Timestamp(closed_m1["datetime"])
            completed = prepared[pd.to_datetime(prepared["datetime"], utc=True) < bucket_start].reset_index(drop=True)
            if len(completed) < 3:
                continue

            signature = pd.Timestamp(completed.iloc[-1]["datetime"])
            if state.last_processed.get(key) == signature:
                continue
            state.last_processed[key] = signature

            # Replay only the completed data and extract the latest completed trade entry.
            trades = engine_trades(completed, market, engine, tf, session)
            if trades.empty:
                continue

            # IMPORTANT: the replay function closes any still-open position at
            # the end of the supplied dataframe. That synthetic close is NOT a
            # new live entry signal. Never execute END_OF_DATA as an entry.
            candidates = trades[trades["reason"].astype(str) != "END_OF_DATA"].copy()
            if candidates.empty:
                continue

            # The newest completed trade event is the actual strategy event
            # that can be acted upon. END_OF_DATA is deliberately excluded.
            last = candidates.iloc[-1]
            entry_time = pd.Timestamp(last["entry_time"])
            sig_key = (engine, tf, session)
            signature2 = (entry_time, str(last["side"]), str(last.get("reason", "")))
            if state.last_signature.get(sig_key) == signature2:
                continue
            state.last_signature[sig_key] = signature2

            print(
                f"[SIGNAL] {market} {engine} TF={tf} {session} "
                f"{last['side']} entry_time={entry_time} reason={last.get('reason', '')}",
                flush=True,
            )

            # Publish only the PF-qualified pair+engine combinations to MT5.
            # This does not alter the OANDA order sent by execute_signal().
            publish_mt5_signal(
                market,
                MARKETS[market],
                engine,
                tf,
                session,
                last,
            )

            # ALL 8 SIGNAL ENGINES ARE LIVE-EXECUTION ENABLED.
            # Every engine/TF/session that produces a qualifying signal may
            # place its own OANDA order. There is intentionally NO
            # LIVE_EXECUTION_ENGINE / LIVE_TF / LIVE_SESSION execution gate.
            execution_name = f"{engine}_TF{tf}_{session}"
            execute_signal(
                s,
                account_id,
                MARKETS[market],
                str(last["side"]),
                execution_name,
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
    print("Signal engines:", ", ".join(SIGNAL_ENGINES))
    print("Nine components:", ", ".join(STRATEGY_COMPONENTS))
    print("MFP frozen layer:", "ENABLED" if MFP_FROZEN_ENABLED else "DISABLED")
    print("MFP live bridge:", "ENABLED" if MFP_LIVE_ENABLED else "DISABLED", "units=", MFP_OANDA_UNITS)
    print("Monitor all:", MONITOR_ALL_LIVE)
    print("Selected execution:", LIVE_EXECUTION_ENGINE, "TF", LIVE_TF, "SESSION", LIVE_SESSION)
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
                    if market == "XAUUSD":
                        process_live_mfp(states[market], closed, bucket, s, account_id)

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
            for engine in SIGNAL_ENGINES:
                # Full-day means no S1/S2/S3 restriction here.
                if engine == "A":
                    prepared = add_ha(resample_tf(raw, tf))
                    trades = run_5759_opposite(prepared)
                elif engine == "B":
                    prepared = add_ha(resample_tf(raw, tf))
                    trades = true_reverse_completed_stream(add_pnl(run_5759_opposite(prepared), market))
                elif engine == "V2":
                    prepared = add_ha(resample_tf(raw, tf))
                    trades = run_frozen_mechanics(prepared)
                elif engine in ("V3", "V3_NORMAL", "57-59_NORMAL"):
                    prepared = add_ha(resample_tf(raw, tf))
                    trades = run_frozen_mechanics(prepared)
                elif engine in ("V3_OPPOSITE", "57-59_OPPOSITE"):
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
    print("Signal engines:", SIGNAL_ENGINES)
    print("Portfolio layer: MFP_FROZEN")
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
    print("Nine components:", ", ".join(STRATEGY_COMPONENTS))
    print("MFP frozen layer:", "ENABLED" if MFP_FROZEN_ENABLED else "DISABLED")
    print("Execution: ALL 8 SIGNAL ENGINES MAY PLACE ORDERS")
    print("Orders:", "ENABLED (PRACTICE)" if LIVE_TRADING_ENABLED else "DRY-RUN")
    print("Time state:", trading_state())
    print("=" * 100)

    start_mt5_feed_server()

    if MASTER_MODE == "LIVE":
        print("[NINE-COMPONENT MODE] All 8 signal engines + frozen MFP layer enabled.", flush=True)
        print("[ALL-ENGINE EXECUTION] Every qualifying signal from all 8 signal engines may place an OANDA order.", flush=True)
        print("[MFP LIVE] XAUUSD V2 -> frozen MFP portfolio bridge enabled.", flush=True)
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
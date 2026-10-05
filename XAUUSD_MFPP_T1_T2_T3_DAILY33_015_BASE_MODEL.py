import os
import math
import pandas as pd
from collections import defaultdict

BASE = r"C:\HA_TEST\OANDA_4_MARKETS"

CANDIDATE = os.path.join(
    BASE,
    "OANDA_3MARKETS_T1_T2_T3_CANDIDATE_TRADES_NO_SPREAD.csv"
)

OUT_TRADES = os.path.join(
    BASE,
    "XAUUSD_MFPP_T1_T2_T3_BASE015_STEP015_DAILY33_MODEL_TRADES.csv"
)

OUT_SUMMARY = os.path.join(
    BASE,
    "XAUUSD_MFPP_T1_T2_T3_BASE015_STEP015_DAILY33_MODEL_SUMMARY.csv"
)

# ============================================================
# LOCKED PORTFOLIO RULES
# ============================================================
# T1 > T2 > T3
# Two portfolio slots per MARKET + SESSION
#
# T1 first -> T1 + (T2 or T3)
# T2 first -> T2 + T1 only
# T3 first -> T3 + T1 only
#
# Opposite T1/T2 -> close existing slot(s), execute incoming
# Opposite T3 -> reject
#
# Never BUY + SELL simultaneously inside one market/session.
#
# Each occupied slot uses the current actual portfolio quantity.
# The reported quantity is the quantity used for P&L.
#
# Frozen engines are NOT modified.
# This script only consumes the candidate trade file.
# ============================================================

MARKETS = ["XAUUSD"]

# MyFundedPerps round-trip commission
# EURUSD/USDJPY: 0.0025% each side = 0.005% round trip
# XAUUSD:         0.005% each side = 0.010% round trip
COMMISSION_RT = {
    "XAUUSD": 0.00010,
}

START_BALANCE = 2500.0

# ============================================================
# USER-LOCKED XAUUSD SIZING MODEL
# ============================================================
# Starting quantity = 0.03 GOLD
# MyFundedPerps daily loss limit = $75
# Use 40% of that limit as the sizing balance = $30
# 33.3% of $30 ~= $9.99
# Every $9.99 milestone adds exactly +0.03 GOLD.
#
# The $2,500 account balance is NOT used to calculate the
# sizing milestone. It is only the starting account balance.
#
# Actual trade quantity is the quantity used in P&L:
# P&L = price movement * actual trade quantity.
#
START_QTY = 0.150
DAILY_LOSS_LIMIT = 75.0
DAILY_LIMIT_SHARE = 0.40
MILESTONE_SHARE = 0.333
MILESTONE_PROFIT = DAILY_LOSS_LIMIT * DAILY_LIMIT_SHARE * MILESTONE_SHARE
LOT_STEP = 0.150

# Keep the reset concept, but do not use the $2,500 balance as
# the sizing anchor. A reset returns quantity to START_QTY and
# restarts milestone counting from the current post-reset balance.
RESET_RATE = 0.001333


# ============================================================
# LOAD
# ============================================================

if not os.path.exists(CANDIDATE):
    raise FileNotFoundError(
        "Candidate file not found:\n" + CANDIDATE
    )

print("=" * 110)
print("OANDA T1/T2/T3 PORTFOLIO")
print("XAUUSD ONLY | NO SPREAD | MFP COMMISSION | 0.15 BASE | +0.15 PER $9.99 MILESTONE")
print("=" * 110)

df = pd.read_csv(CANDIDATE)

required = [
    "market", "session", "priority", "engine", "tf",
    "entry_time", "exit_time", "side", "entry", "exit",
    "gross_pnl"
]

missing = [c for c in required if c not in df.columns]

if missing:
    raise ValueError("Missing columns: " + str(missing))

df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce")
df["exit_time"] = pd.to_datetime(df["exit_time"], errors="coerce")

for c in ["entry", "exit", "gross_pnl", "tf"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")

df = df[df["market"].astype(str).str.upper() == "XAUUSD"].copy()

if df.empty:
    raise ValueError("No XAUUSD candidate trades found in candidate file")

df = df.dropna(
    subset=[
        "market", "session", "priority",
        "entry_time", "exit_time",
        "entry", "exit", "gross_pnl"
    ]
).copy()

df["priority_num"] = (
    df["priority"]
    .astype(str)
    .str.extract(r"(\d+)", expand=False)
    .astype(int)
)

df = df.sort_values(
    ["entry_time", "exit_time", "market", "session",
     "priority_num", "tf"],
    kind="mergesort"
).reset_index(drop=True)

# Unique candidate trade id
df["candidate_id"] = range(len(df))

print("Candidate rows:", f"{len(df):,}")
print("Markets:", ", ".join(sorted(df["market"].unique())))
print()

# ============================================================
# COMPOUNDING STATE
# ============================================================

class Compound:
    def __init__(self, balance):
        self.balance = float(balance)
        self.qty = float(START_QTY)
        self.step_count = 0
        self.highest_balance = self.balance
        self.lowest_balance = self.balance
        self.step_base_balance = self.balance
        self.reset_threshold = self.balance * (1.0 - RESET_RATE)
        self.scaling_state = "BASE_WAITING"

    def update(self):
        # One milestone = $9.99 (daily loss limit $75 * 40% * 33.3%).
        # Each qualifying balance update adds exactly +0.03 GOLD.
        # After an add, the current balance becomes the new step base.
        # If balance falls 0.1333% below the current step base, reset
        # quantity to 0.03 and restart from the current balance.
        while self.balance >= self.step_base_balance + MILESTONE_PROFIT:
            self.step_count += 1
            self.qty = START_QTY + self.step_count * LOT_STEP
            self.step_base_balance += MILESTONE_PROFIT
            self.scaling_state = "SCALING_ACTIVE"

        if self.balance <= self.step_base_balance * (1.0 - RESET_RATE):
            self.qty = float(START_QTY)
            self.step_count = 0
            self.step_base_balance = self.balance
            self.scaling_state = "RESET_WAITING"

        self.highest_balance = max(self.highest_balance, self.balance)
        self.lowest_balance = min(self.lowest_balance, self.balance)

    def next_trigger(self):
        return self.step_base_balance + MILESTONE_PROFIT

    def add(self, pnl):
        self.balance += float(pnl)
        self.update()


# ============================================================
# PORTFOLIO
# ============================================================

comp = Compound(START_BALANCE)

# state[(market, session)] = {
#     "direction": BUY/SELL/None,
#     "slots": {
#         T1/T2/T3: position
#     }
# }
states = {}

for market in MARKETS:
    for session in df.loc[
        df["market"] == market, "session"
    ].dropna().unique():

        states[(market, session)] = {
            "direction": None,
            "slots": {}
        }


portfolio_trades = []
rejected = []

realized_gross = 0.0
total_commission = 0.0

wins = 0
losses = 0
breakeven = 0

equity_curve = []


# ============================================================
# HELPERS
# ============================================================

def commission(market, qty, entry, exit):
    rate = COMMISSION_RT[market]

    # Filled notional:
    # entry notional + exit notional
    return (
        abs(qty * entry) +
        abs(qty * exit)
    ) * rate


def scaled_gross(row, qty, exit_price=None):
    if exit_price is None:
        exit_price = float(row["exit"])

    entry = float(row["entry"])
    raw = float(row["gross_pnl"])

    # Candidate gross_pnl is 1-unit historical P&L.
    # Scale it to the actual portfolio slot quantity.
    #
    # To preserve exact direction/price behavior, calculate
    # from the stored 1-unit gross P&L.
    return raw * qty if exit_price == float(row["exit"]) else (
        (exit_price - entry) * qty
        if str(row["side"]).upper() == "BUY"
        else (entry - exit_price) * qty
    )


def close_position(key, priority, pos, exit_price, reason):
    global realized_gross, total_commission
    global wins, losses, breakeven

    market, session = key

    qty = float(pos["qty"])
    entry_price = float(pos["entry_price"])

    side = str(pos["side"]).upper()

    if side == "BUY":
        gross = (exit_price - entry_price) * qty
    else:
        gross = (entry_price - exit_price) * qty

    # Explicit XAUUSD quantity audit: actual P&L must use the actual qty.
    price_move = (exit_price - entry_price) if side == "BUY" else (entry_price - exit_price)
    gross_check = price_move * qty
    if abs(gross - gross_check) > 1e-10:
        raise RuntimeError("QTY/P&L mismatch")

    fee = commission(
        market,
        qty,
        entry_price,
        exit_price
    )

    net = gross - fee

    balance_before = comp.balance
    qty_before = comp.qty
    level_before = comp.step_count

    comp.add(net)

    balance_after = comp.balance
    qty_after = comp.qty
    level_after = comp.step_count

    realized_gross += gross
    total_commission += fee

    if net > 0:
        wins += 1
    elif net < 0:
        losses += 1
    else:
        breakeven += 1

    portfolio_trades.append({
        "market": market,
        "session": session,
        "priority": priority,
        "engine": pos["engine"],
        "tf": pos["tf"],
        "side": side,
        "entry_time": pos["entry_time"],
        "exit_time": pos["current_time"],
        "entry": entry_price,
        "exit": exit_price,
        "qty": qty,
        "gross_pnl": gross,
        "commission": fee,
        "net_pnl": net,
        "exit_reason": reason,
        "balance_before": balance_before,
        "balance_after": balance_after,
        "compound_qty_before": qty_before,
        "compound_qty_after": qty_after,
        "compound_level_before": level_before,
        "compound_level_after": level_after,
        "source_candidate_id": pos["candidate_id"],
    })

    return net


def close_all(key, exit_price, reason):
    state = states[key]

    for p in list(state["slots"].keys()):
        pos = state["slots"].pop(p)

        close_position(
            key,
            p,
            pos,
            exit_price,
            reason
        )

    state["direction"] = None


def accept_entry(row):
    key = (row["market"], row["session"])
    state = states[key]

    market = row["market"]
    session = row["session"]
    priority = row["priority"]
    side = str(row["side"]).upper()

    # Existing portfolio direction
    direction = state["direction"]

    # --------------------------------------------------------
    # EMPTY
    # --------------------------------------------------------
    if direction is None or not state["slots"]:

        qty = comp.qty

        state["direction"] = side

        state["slots"][priority] = {
            "candidate_id": int(row["candidate_id"]),
            "priority": priority,
            "engine": row["engine"],
            "tf": int(row["tf"]),
            "side": side,
            "entry_time": row["entry_time"],
            "entry_price": float(row["entry"]),
            "qty": qty,
            "current_time": row["entry_time"],
        }

        return True, "ENTRY_FIRST"

    # --------------------------------------------------------
    # OPPOSITE DIRECTION
    # --------------------------------------------------------
    if side != direction:

        if priority in ["T1", "T2"]:

            # Incoming T1/T2 has priority.
            close_all(
                key,
                float(row["entry"]),
                "OPPOSITE_" + priority
            )

            qty = comp.qty

            state["direction"] = side

            state["slots"][priority] = {
                "candidate_id": int(row["candidate_id"]),
                "priority": priority,
                "engine": row["engine"],
                "tf": int(row["tf"]),
                "side": side,
                "entry_time": row["entry_time"],
                "entry_price": float(row["entry"]),
                "qty": qty,
                "current_time": row["entry_time"],
            }

            return True, "REVERSE_" + priority

        # Opposite T3 rejected
        rejected.append({
            "candidate_id": int(row["candidate_id"]),
            "market": market,
            "session": session,
            "priority": priority,
            "side": side,
            "entry_time": row["entry_time"],
            "reason": "OPPOSITE_T3_REJECTED"
        })

        return False, "OPPOSITE_T3_REJECTED"

    # --------------------------------------------------------
    # SAME DIRECTION
    # --------------------------------------------------------

    # Already active same priority
    if priority in state["slots"]:

        rejected.append({
            "candidate_id": int(row["candidate_id"]),
            "market": market,
            "session": session,
            "priority": priority,
            "side": side,
            "entry_time": row["entry_time"],
            "reason": "PRIORITY_ALREADY_ACTIVE"
        })

        return False, "PRIORITY_ALREADY_ACTIVE"

    active = set(state["slots"].keys())

    # Two slots already full
    if len(active) >= 2:

        rejected.append({
            "candidate_id": int(row["candidate_id"]),
            "market": market,
            "session": session,
            "priority": priority,
            "side": side,
            "entry_time": row["entry_time"],
            "reason": "TWO_SLOTS_FULL"
        })

        return False, "TWO_SLOTS_FULL"

    # T1 can always fill the second slot.
    if priority == "T1":

        qty = comp.qty

        state["slots"][priority] = {
            "candidate_id": int(row["candidate_id"]),
            "priority": priority,
            "engine": row["engine"],
            "tf": int(row["tf"]),
            "side": side,
            "entry_time": row["entry_time"],
            "entry_price": float(row["entry"]),
            "qty": qty,
            "current_time": row["entry_time"],
        }

        return True, "ADD_T1"

    # If T1 is already first, T2 or T3 can fill slot 2.
    if "T1" in active:

        qty = comp.qty

        state["slots"][priority] = {
            "candidate_id": int(row["candidate_id"]),
            "priority": priority,
            "engine": row["engine"],
            "tf": int(row["tf"]),
            "side": side,
            "entry_time": row["entry_time"],
            "entry_price": float(row["entry"]),
            "qty": qty,
            "current_time": row["entry_time"],
        }

        return True, "ADD_" + priority

    # T2 first -> only T1 may fill second.
    if "T2" in active:

        rejected.append({
            "candidate_id": int(row["candidate_id"]),
            "market": market,
            "session": session,
            "priority": priority,
            "side": side,
            "entry_time": row["entry_time"],
            "reason": "T2_FIRST_ONLY_T1_ALLOWED"
        })

        return False, "T2_FIRST_ONLY_T1_ALLOWED"

    # T3 first -> only T1 may fill second.
    if "T3" in active:

        rejected.append({
            "candidate_id": int(row["candidate_id"]),
            "market": market,
            "session": session,
            "priority": priority,
            "side": side,
            "entry_time": row["entry_time"],
            "reason": "T3_FIRST_ONLY_T1_ALLOWED"
        })

        return False, "T3_FIRST_ONLY_T1_ALLOWED"

    return False, "UNHANDLED"


# ============================================================
# EVENT STREAM
# ============================================================

events = []

for _, row in df.iterrows():

    events.append({
        "time": row["entry_time"],
        "kind": "ENTRY",
        "row": row
    })

    events.append({
        "time": row["exit_time"],
        "kind": "EXIT",
        "row": row
    })

# EXIT FIRST at identical timestamp.
# Then ENTRY signals.
events.sort(
    key=lambda e: (
        e["time"],
        0 if e["kind"] == "EXIT" else 1,
        int(e["row"]["priority_num"]),
        int(e["row"]["candidate_id"])
    )
)


# ============================================================
# RUN
# ============================================================

print("Events:", f"{len(events):,}")
print("Starting balance:", f"${START_BALANCE:,.2f}")
print("Starting quantity:", START_QTY)
print("Daily loss limit:", f"${DAILY_LOSS_LIMIT:,.2f}")
print("40% sizing base:", f"${DAILY_LOSS_LIMIT * DAILY_LIMIT_SHARE:,.2f}")
print("33.3% milestone:", f"${MILESTONE_PROFIT:,.4f}")
print("Lot step:", LOT_STEP, "| Reset rate:", RESET_RATE)
print("First add trigger:", f"${comp.next_trigger():,.4f}")
print("Scaling rule       : each $9.99 milestone adds exactly +0.15 GOLD")
print()

for n, event in enumerate(events):

    row = event["row"]

    market = row["market"]
    session = row["session"]
    priority = row["priority"]

    key = (market, session)

    if key not in states:
        states[key] = {
            "direction": None,
            "slots": {}
        }

    # --------------------------------------------------------
    # EXIT EVENT
    # --------------------------------------------------------
    if event["kind"] == "EXIT":

        state = states[key]

        if priority in state["slots"]:

            pos = state["slots"][priority]

            # Only the exact candidate trade that opened
            # the portfolio slot may close it normally.
            if (
                int(pos["candidate_id"])
                == int(row["candidate_id"])
            ):

                pos["current_time"] = row["exit_time"]

                close_position(
                    key,
                    priority,
                    pos,
                    float(row["exit"]),
                    "SYSTEM_EXIT"
                )

                del state["slots"][priority]

                if not state["slots"]:
                    state["direction"] = None

        continue

    # --------------------------------------------------------
    # ENTRY EVENT
    # --------------------------------------------------------
    accepted, reason = accept_entry(row)

    if not accepted:
        continue

    # Record current time on all active positions
    for p in states[key]["slots"].values():
        p["current_time"] = row["entry_time"]


# ============================================================
# CLOSE ANY REMAINING OPEN POSITIONS
# ============================================================

# We do not have a final candle execution price in the candidate
# file for positions still open at the end of the dataset.
# Therefore DO NOT invent a final price.
#
# Instead report them as open and exclude them from realized PF.

open_positions = []

for key, state in states.items():

    for priority, pos in state["slots"].items():

        open_positions.append({
            "market": key[0],
            "session": key[1],
            "priority": priority,
            "side": pos["side"],
            "entry_time": pos["entry_time"],
            "entry": pos["entry_price"],
            "qty": pos["qty"],
            "candidate_id": pos["candidate_id"]
        })


# ============================================================
# RESULTS
# ============================================================

trades = pd.DataFrame(portfolio_trades)

if len(trades):

    gp = trades.loc[
        trades["net_pnl"] > 0,
        "net_pnl"
    ].sum()

    gl = -trades.loc[
        trades["net_pnl"] < 0,
        "net_pnl"
    ].sum()

    pf = gp / gl if gl > 0 else math.inf

    net = trades["net_pnl"].sum()

    win_rate = (
        (trades["net_pnl"] > 0).mean() * 100
    )

    equity = (
        pd.Series(
            trades["balance_after"].values,
            index=range(len(trades))
        )
    )

    running_peak = equity.cummax()

    drawdown = (
        (equity - running_peak)
        / running_peak
        * 100
    )

    max_dd = abs(drawdown.min())

else:

    gp = gl = pf = net = win_rate = max_dd = 0.0


# ============================================================
# SUMMARY BY MARKET
# ============================================================

summary_rows = []

for market in MARKETS:

    x = trades[
        trades["market"] == market
    ].copy()

    if len(x):

        gp_m = x.loc[
            x["net_pnl"] > 0,
            "net_pnl"
        ].sum()

        gl_m = -x.loc[
            x["net_pnl"] < 0,
            "net_pnl"
        ].sum()

        pf_m = (
            gp_m / gl_m
            if gl_m > 0 else math.inf
        )

        net_m = x["net_pnl"].sum()

        wr_m = (
            (x["net_pnl"] > 0).mean() * 100
        )

    else:
        gp_m = gl_m = pf_m = net_m = wr_m = 0.0

    summary_rows.append({
        "market": market,
        "trades": len(x),
        "gross_profit_after_fee": gp_m,
        "gross_loss_after_fee": gl_m,
        "pf": pf_m,
        "win_rate": wr_m,
        "net": net_m
    })


# ============================================================
# FINAL ACCOUNT STATUS
# ============================================================

final_balance = comp.balance

account_floor = 2425.0
profit_target = 2725.0

summary = pd.DataFrame([
    {
        "scope": "TOTAL",
        "market": "ALL",
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "pf": pf,
        "win_rate": win_rate,
        "net": net,
        "commission": total_commission,
        "realized_gross": realized_gross,
        "starting_balance": START_BALANCE,
        "final_balance": final_balance,
        "max_closed_balance_dd_pct": max_dd,
        "highest_closed_balance": comp.highest_balance,
        "lowest_closed_balance": comp.lowest_balance,
        "final_compound_level": comp.step_count,
        "final_qty": comp.qty,
        "final_step_base_balance": comp.step_base_balance,
        "next_step_trigger": comp.next_trigger(),
        "daily_loss_limit": DAILY_LOSS_LIMIT,
        "daily_limit_share": DAILY_LIMIT_SHARE,
        "milestone_share": MILESTONE_SHARE,
        "milestone_profit": MILESTONE_PROFIT,
        "lot_step": LOT_STEP,
        "reset_rate": RESET_RATE,
        "first_add_trigger": START_BALANCE + MILESTONE_PROFIT,
        "reset_trigger": START_BALANCE * (1.0 - RESET_RATE),
        "open_positions": len(open_positions),
        "funded_floor": account_floor,
        "profit_target": profit_target,
        "closed_balance_above_floor": final_balance > account_floor,
        "closed_balance_above_target": final_balance >= profit_target,
    }
])

market_summary = pd.DataFrame(summary_rows)

summary.to_csv(OUT_SUMMARY, index=False)
trades.to_csv(OUT_TRADES, index=False)

print()
print("=" * 110)
print("PORTFOLIO RESULT")
print("=" * 110)

print("Starting balance :", f"${START_BALANCE:,.2f}")
print("Final balance    :", f"${final_balance:,.2f}")
print("Net P&L          :", f"${net:,.2f}")
print("Portfolio PF     :", f"{pf:.6f}")
print("Win rate         :", f"{win_rate:.2f}%")
print("Realized trades  :", f"{len(trades):,}")
print("Commission       :", f"${total_commission:,.2f}")
print("Max closed DD    :", f"{max_dd:.2f}%")
print("Final lot        :", f"{comp.qty:.6f}")
print("Final step       :", comp.step_count)
print("Step base        :", f"${comp.step_base_balance:,.6f}")
print("Next step trigger:", f"${comp.next_trigger():,.6f}")
print("Open positions   :", len(open_positions))

print()
print("FUNDING CHECK (CLOSED BALANCE ONLY)")
print("Floor $2,425    :", "PASS" if final_balance > account_floor else "FAIL")
print("Target $2,725   :", "PASS" if final_balance >= profit_target else "NOT REACHED")

print()
print("=" * 110)
print("BY MARKET")
print("=" * 110)

print(market_summary.to_string(index=False))

print()
print("Portfolio trades:")
print(OUT_TRADES)

print()
print("Portfolio summary:")
print(OUT_SUMMARY)

print()
print("IMPORTANT:")
print("Funded daily/equity drawdown is NOT certified by this backtest.")
print("This file contains completed trades, not every intratrade price tick.")
print("Therefore floating equity and exact MFP daily-drawdown compliance")
print("must be tested separately from the original M1 price data.")

print()
print("DONE.")

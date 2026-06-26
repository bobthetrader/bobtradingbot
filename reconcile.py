#!/usr/bin/env python3
"""Balance reconciliation — run on the server to verify the reported balance
is consistent with the actual trade history.

Usage (on the server):
  docker exec tradingbot_local python reconcile.py

What it checks:
  initial_balance (pnl_state.json)
  + sum of all closed main-bot trade P&L (trade_events_paper.jsonl)
  + sum of all closed scalper trade P&L (scalper_trades.jsonl)
  = expected EUR cash balance
  + open position cost basis (purchase_prices_paper.json)
  = expected portfolio value

  vs. reported balance_eur and portfolio_value in bot_status.json
"""

import json
import os
from pathlib import Path
from datetime import datetime

DATA = Path(__file__).parent / "data"


def load_jsonl(path: Path) -> list:
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return rows


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else {}


# ── Load all data sources ──────────────────────────────────────────────────

pnl_state       = load_json(DATA / "pnl_state.json")
bot_status      = load_json(DATA / "bot_status.json")
purchase_prices = load_json(DATA / "purchase_prices_paper.json")
main_trades     = load_jsonl(DATA / "trade_events_paper.jsonl")
scalper_trades  = load_jsonl(DATA / "scalper_trades.jsonl")

# ── Baseline ───────────────────────────────────────────────────────────────

initial_balance = float(pnl_state.get("start_eur", 0.0))
created_at      = pnl_state.get("created_at", "unknown")

print("=" * 60)
print("BALANCE RECONCILIATION")
print("=" * 60)
print(f"  P&L baseline start  : €{initial_balance:.4f}  (since {created_at[:10]})")
print()

# ── Main bot trades ────────────────────────────────────────────────────────

sell_trades = [t for t in main_trades if t.get("type") in ("SELL", "CLOSE", "STOP_LOSS",
               "TAKE_PROFIT", "SELL_SHORT", "CLOSE_SHORT")]
buy_trades  = [t for t in main_trades if t.get("type") in ("BUY", "OPEN", "BUY_SHORT")]

main_pnl        = sum(float(t.get("pnl_eur", 0)) for t in sell_trades)
main_pnl_trades = len(sell_trades)

# Per-pair breakdown
pair_pnl: dict = {}
for t in sell_trades:
    p   = t.get("pair", "?")
    pnl = float(t.get("pnl_eur", 0))
    pair_pnl[p] = pair_pnl.get(p, 0.0) + pnl

print(f"  Main bot trades     : {len(buy_trades)} buys, {main_pnl_trades} sells")
print(f"  Main bot P&L        : €{main_pnl:+.4f}")
if pair_pnl:
    for pair, pnl in sorted(pair_pnl.items(), key=lambda x: x[1]):
        col = "+" if pnl >= 0 else ""
        print(f"    {pair:15s}: €{pnl:+.4f}")
print()

# ── Scalper trades ─────────────────────────────────────────────────────────

scalp_pnl   = sum(float(t.get("pnl_eur", 0)) for t in scalper_trades)
scalp_wins  = sum(1 for t in scalper_trades if float(t.get("pnl_eur", 0)) > 0)
scalp_loss  = sum(1 for t in scalper_trades if float(t.get("pnl_eur", 0)) < 0)
scalp_to    = sum(1 for t in scalper_trades if t.get("reason") == "TIMEOUT")

# Per-pair scalper breakdown
scalp_pair_pnl: dict = {}
scalp_pair_wl: dict  = {}
for t in scalper_trades:
    p   = t.get("pair", "?")
    pnl = float(t.get("pnl_eur", 0))
    scalp_pair_pnl[p] = scalp_pair_pnl.get(p, 0.0) + pnl
    if p not in scalp_pair_wl:
        scalp_pair_wl[p] = {"w": 0, "l": 0}
    if pnl > 0:
        scalp_pair_wl[p]["w"] += 1
    else:
        scalp_pair_wl[p]["l"] += 1

print(f"  Scalper trades      : {len(scalper_trades)} total | "
      f"{scalp_wins}W / {scalp_loss}L | {scalp_to} timeouts")
print(f"  Scalper P&L         : €{scalp_pnl:+.4f}")
if scalp_pair_pnl:
    for pair, pnl in sorted(scalp_pair_pnl.items(), key=lambda x: x[1]):
        wl  = scalp_pair_wl.get(pair, {})
        tot = wl.get("w", 0) + wl.get("l", 0)
        wr  = round(wl["w"] / tot * 100) if tot else 0
        print(f"    {pair:15s}: €{pnl:+.4f}  ({wl.get('w',0)}W/{wl.get('l',0)}L  {wr}%)")
print()

# ── Open positions ─────────────────────────────────────────────────────────

open_cost = 0.0
print("  Open positions (main bot):")
if purchase_prices:
    for pair, meta in purchase_prices.items():
        if isinstance(meta, dict):
            qty   = float(meta.get("qty", 0))
            entry = float(meta.get("entry_price_eur", 0))
        else:
            qty   = 0.0
            entry = float(meta) if meta else 0.0
        cost = qty * entry
        open_cost += cost
        if qty > 0:
            print(f"    {pair:15s}: qty={qty:.8f}  entry=€{entry:.4f}  cost=€{cost:.4f}")
    if open_cost == 0:
        print("    (none)")
else:
    print("    (none)")
print(f"  Open position cost  : €{open_cost:.4f}")
print()

# ── Scalper open positions ─────────────────────────────────────────────────

scalper_pos  = load_json(DATA / "scalper_positions.json")
scalper_open = 0.0
if scalper_pos:
    print("  Open scalper positions:")
    for pair, sv in scalper_pos.items():
        qty   = float(sv.get("qty", 0))
        entry = float(sv.get("entry", 0))
        cost  = qty * entry
        scalper_open += cost
        print(f"    {pair:15s}: qty={qty:.8f}  entry=€{entry:.4f}  cost=€{cost:.4f}")
    print(f"  Scalper open cost   : €{scalper_open:.4f}")
else:
    print("  Open scalper positions: (none)")
print()

# ── Reconciliation ─────────────────────────────────────────────────────────

expected_cash      = initial_balance + main_pnl + scalp_pnl
expected_portfolio = expected_cash + open_cost + scalper_open

reported_cash      = float(bot_status.get("balance_eur", 0))
reported_portfolio = float(bot_status.get("portfolio_value", reported_cash))
reported_pnl       = float(bot_status.get("adjusted_pnl", 0))

print("=" * 60)
print("RECONCILIATION SUMMARY")
print("=" * 60)
print(f"  Initial balance     : €{initial_balance:.4f}")
print(f"  + Main bot P&L      : €{main_pnl:+.4f}  ({main_pnl_trades} closed trades)")
print(f"  + Scalper P&L       : €{scalp_pnl:+.4f}  ({len(scalper_trades)} closed trades)")
print(f"  = Expected cash     : €{expected_cash:.4f}")
print(f"  + Open positions    : €{open_cost:.4f}")
print(f"  + Scalper open      : €{scalper_open:.4f}")
print(f"  = Expected portfolio: €{expected_portfolio:.4f}")
print()
print(f"  Reported cash (EUR) : €{reported_cash:.4f}")
print(f"  Reported portfolio  : €{reported_portfolio:.4f}")
print(f"  Reported P&L        : €{reported_pnl:+.4f}")
print()

cash_diff  = reported_cash - expected_cash
port_diff  = reported_portfolio - expected_portfolio

def check(label, diff, tolerance=0.01):
    if abs(diff) <= tolerance:
        status = "OK"
    elif abs(diff) <= 1.0:
        status = "WARN (small rounding)"
    else:
        status = f"MISMATCH — investigate"
    print(f"  {label:25s}: diff €{diff:+.4f}  [{status}]")

check("Cash balance", cash_diff)
check("Portfolio value", port_diff)

total_real_pnl = main_pnl + scalp_pnl
print()
print(f"  Total closed P&L    : €{total_real_pnl:+.4f}")
print(f"  (main bot €{main_pnl:+.4f}  +  scalper €{scalp_pnl:+.4f})")

if abs(cash_diff) > 1.0 or abs(port_diff) > 1.0:
    print()
    print("  *** MISMATCH DETECTED — checking last 5 trades for clues:")
    for t in main_trades[-5:]:
        print(f"    [{t.get('ts','')[:19]}] {t.get('type','?'):10s} "
              f"{t.get('pair','?'):12s} pnl=€{float(t.get('pnl_eur',0)):+.4f}  "
              f"bal_after=€{float(t.get('balance_eur',0)):.4f}")
else:
    print()
    print("  *** Balance is consistent with trade history. ***")

print("=" * 60)

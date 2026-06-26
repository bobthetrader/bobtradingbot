#!/usr/bin/env python3
"""One-shot balance corrector.

Calculates the correct paper_balance_eur from the actual trade history
(same formula as reconcile.py) and writes it into balance_state.json.
On the next bot restart the corrected value is restored, eliminating
ghost money added by previous restart-resets.

Usage:
  docker exec tradingbot_local python3 fix_balance.py
"""

import json
from pathlib import Path

DATA = Path(__file__).parent / "data"


def load_jsonl(path):
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


def load_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else {}


pnl_state       = load_json(DATA / "pnl_state.json")
bot_status      = load_json(DATA / "bot_status.json")
purchase_prices = load_json(DATA / "purchase_prices_paper.json")
main_trades     = load_jsonl(DATA / "trade_events_paper.jsonl")
scalper_trades  = load_jsonl(DATA / "scalper_trades.jsonl")
scalper_pos     = load_json(DATA / "scalper_positions.json")
bal_state_path  = DATA / "balance_state.json"

initial = float(pnl_state.get("start_eur", 500.0))

# Closed P&L from main bot
sell_types = ("SELL","CLOSE","STOP_LOSS","TAKE_PROFIT","SHORT_CLOSE","SELL_SHORT","CLOSE_SHORT")
main_pnl = sum(float(t.get("pnl_eur", 0)) for t in main_trades if t.get("type") in sell_types)

# Closed P&L from scalper
scalp_pnl = sum(float(t.get("pnl_eur", 0)) for t in scalper_trades)

# Open long position entry costs
long_cost = 0.0
for pair, meta in (purchase_prices or {}).items():
    if isinstance(meta, dict):
        qty   = float(meta.get("qty", 0))
        entry = float(meta.get("entry_price_eur", 0))
    else:
        qty, entry = 0.0, float(meta or 0)
    long_cost += qty * entry

# Open scalper allocation
scalp_alloc = sum(
    float(v.get("qty", 0)) * float(v.get("entry", 0))
    for v in (scalper_pos or {}).values()
)

# Open short proceeds in cash
open_shorts = bot_status.get("open_shorts", {})
short_proceeds = sum(
    float(sv.get("qty", 0)) * float(sv.get("entry", 0))
    for sv in open_shorts.values()
)

correct_cash = initial + main_pnl + scalp_pnl - long_cost - scalp_alloc + short_proceeds
reported_cash = float(bot_status.get("balance_eur", 0))
ghost_money   = reported_cash - correct_cash

print("=" * 60)
print("BALANCE CORRECTION")
print("=" * 60)
print(f"  Initial (pnl_state)    : €{initial:.4f}")
print(f"  Main closed P&L        : €{main_pnl:+.4f}")
print(f"  Scalper closed P&L     : €{scalp_pnl:+.4f}")
print(f"  Open long costs        : €{-long_cost:.4f}")
print(f"  Scalper open alloc     : €{-scalp_alloc:.4f}")
print(f"  Short proceeds in cash : €{short_proceeds:+.4f}")
print(f"  = CORRECT cash         : €{correct_cash:.4f}")
print(f"  Reported cash now      : €{reported_cash:.4f}")
print(f"  Ghost money to remove  : €{ghost_money:.4f}")
print()

# Load existing balance_state and patch paper_balance_eur
try:
    existing = json.loads(bal_state_path.read_text()) if bal_state_path.exists() else {}
except Exception:
    existing = {}

existing["paper_balance_eur"] = round(correct_cash, 4)

# Keep peak_balance and initial_balance_eur as they are, but recalculate a sane peak
# True portfolio = correct_cash + open_longs_at_entry + scalper_alloc (conservative estimate)
true_portfolio_est = correct_cash + long_cost + scalp_alloc
existing["peak_balance"] = round(max(
    float(existing.get("peak_balance", initial)),
    true_portfolio_est
), 4)

bal_state_path.write_text(json.dumps(existing, indent=2))

print(f"  balance_state.json updated:")
print(f"    paper_balance_eur  = €{existing['paper_balance_eur']:.4f}")
print(f"    peak_balance       = €{existing['peak_balance']:.4f}")
print(f"    initial_balance_eur= €{existing.get('initial_balance_eur', initial):.4f}")
print()
print("  Restart the bot to apply:")
print("  docker compose up --build -d")
print("=" * 60)

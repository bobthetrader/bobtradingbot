#!/usr/bin/env python3
"""Balance reconciliation — run on the server to verify the reported balance
is consistent with the actual trade history.

Usage (on the server):
  docker exec tradingbot_local python reconcile.py
"""

import json
from pathlib import Path

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


# ── Load all data ──────────────────────────────────────────────────────────

pnl_state       = load_json(DATA / "pnl_state.json")
bot_status      = load_json(DATA / "bot_status.json")
purchase_prices = load_json(DATA / "purchase_prices_paper.json")
main_trades     = load_jsonl(DATA / "trade_events_paper.jsonl")
scalper_trades  = load_jsonl(DATA / "scalper_trades.jsonl")
scalper_pos     = load_json(DATA / "scalper_positions.json")

initial_balance = float(pnl_state.get("start_eur", 0.0))
created_at      = pnl_state.get("created_at", "unknown")[:10]

# Detect bot restarts: each restart resets _paper_balance_eur to initial_balance.
# Count approximate restarts by looking for large upward jumps in bal_after that
# are close to initial_balance (not explainable by a single trade).
_balance_resets = 0
_reset_boost    = 0.0
_prev_bal = None
for t in main_trades:
    bal = float(t.get("balance_eur", 0))
    if bal <= 0:
        continue
    if _prev_bal is not None:
        jump = bal - _prev_bal
        # A jump UP to roughly initial_balance (+/- 30%) that can't be a single trade
        if jump > 50 and abs(bal - initial_balance) < initial_balance * 0.5:
            _balance_resets += 1
            _reset_boost += jump
    _prev_bal = bal

print("=" * 65)
print("BALANCE RECONCILIATION")
print("=" * 65)
print(f"  P&L baseline start : €{initial_balance:.4f}  (since {created_at})")
if _balance_resets:
    print(f"  *** {_balance_resets} paper-balance restart(s) detected ***")
    print(f"      Each restart resets paper_eur to {initial_balance:.0f}.")
    print(f"      Approximate total boost from resets: €{_reset_boost:.2f}")
    print(f"      Deploy 'persist paper balance' fix to prevent this.")
print()

# ── Main bot closed trades ─────────────────────────────────────────────────

sell_trades  = [t for t in main_trades
                if t.get("type") in ("SELL","CLOSE","STOP_LOSS","TAKE_PROFIT",
                                     "SHORT_CLOSE","SELL_SHORT","CLOSE_SHORT")]
buy_trades   = [t for t in main_trades
                if t.get("type") in ("BUY","OPEN","SHORT_OPEN","BUY_SHORT")]

main_pnl     = sum(float(t.get("pnl_eur", 0)) for t in sell_trades)
pair_pnl: dict = {}
for t in sell_trades:
    p = t.get("pair", "?")
    pair_pnl[p] = pair_pnl.get(p, 0.0) + float(t.get("pnl_eur", 0))

print(f"  Main bot trades    : {len(buy_trades)} opens, {len(sell_trades)} closes")
print(f"  Main bot closed P&L: €{main_pnl:+.4f}")
for pair, pnl in sorted(pair_pnl.items(), key=lambda x: x[1]):
    print(f"    {pair:15s}: €{pnl:+.4f}")
print()

# ── Scalper closed trades ──────────────────────────────────────────────────

scalp_pnl  = sum(float(t.get("pnl_eur", 0)) for t in scalper_trades)
scalp_wins = sum(1 for t in scalper_trades if float(t.get("pnl_eur", 0)) > 0)
scalp_loss = sum(1 for t in scalper_trades if float(t.get("pnl_eur", 0)) <= 0)
scalp_to   = sum(1 for t in scalper_trades if t.get("reason") == "TIMEOUT")

scalp_pair: dict = {}
for t in scalper_trades:
    p = t.get("pair", "?")
    if p not in scalp_pair:
        scalp_pair[p] = {"pnl": 0.0, "w": 0, "l": 0}
    pnl = float(t.get("pnl_eur", 0))
    scalp_pair[p]["pnl"] += pnl
    if pnl > 0:
        scalp_pair[p]["w"] += 1
    else:
        scalp_pair[p]["l"] += 1

print(f"  Scalper trades     : {len(scalper_trades)} total | "
      f"{scalp_wins}W/{scalp_loss}L | {scalp_to} timeouts")
print(f"  Scalper closed P&L : €{scalp_pnl:+.4f}")
for pair, d in sorted(scalp_pair.items(), key=lambda x: x[1]["pnl"]):
    tot = d["w"] + d["l"]
    wr  = round(d["w"] / tot * 100) if tot else 0
    print(f"    {pair:15s}: €{d['pnl']:+.4f}  ({d['w']}W/{d['l']}L  {wr}%)")
print()

# ── Open long positions (main bot) ─────────────────────────────────────────

long_open_cost = 0.0
print("  Open long positions (main bot):")
if purchase_prices:
    for pair, meta in purchase_prices.items():
        if isinstance(meta, dict):
            qty   = float(meta.get("qty", 0))
            entry = float(meta.get("entry_price_eur", 0))
        else:
            qty, entry = 0.0, float(meta or 0)
        cost = qty * entry
        long_open_cost += cost
        if qty > 0:
            print(f"    {pair:15s}: qty={qty:.8f}  entry=€{entry:.4f}  cost=€{cost:.4f}")
    if long_open_cost == 0:
        print("    (none)")
else:
    print("    (none)")
print(f"  Long open cost (entry): €{long_open_cost:.4f}")
print()

# ── Open SHORT positions ───────────────────────────────────────────────────
# In paper mode, opening a short ADDS proceeds (qty × entry) to cash.
# This is a LIABILITY — the bot must buy these back later.
# We must subtract this from cash to get true net position.

open_shorts  = bot_status.get("open_shorts", {})
short_proceeds  = 0.0   # cash currently inflated by these
short_unrealised= 0.0   # unrealised P&L at current prices

print("  Open short positions:")
if open_shorts:
    for pair, sv in open_shorts.items():
        qty      = float(sv.get("qty", 0))
        entry    = float(sv.get("entry", 0))
        current  = float(sv.get("current", 0))
        pnl_eur  = float(sv.get("pnl_eur", 0))
        proceeds = qty * entry
        short_proceeds   += proceeds
        short_unrealised += pnl_eur
        pnl_col = "+" if pnl_eur >= 0 else ""
        print(f"    {pair:15s}: qty={qty:.4f}  entry=€{entry:.4f}  "
              f"now=€{current:.4f}  proceeds=€{proceeds:.4f}  "
              f"unrealised=€{pnl_eur:+.4f}")
else:
    print("    (none)")
print(f"  Short proceeds in cash : €{short_proceeds:.4f}  (liability — will be paid on close)")
print(f"  Short unrealised P&L   : €{short_unrealised:+.4f}")
print()

# ── Scalper open positions ─────────────────────────────────────────────────

scalp_open_alloc = 0.0
if scalper_pos:
    print("  Open scalper positions:")
    for pair, sv in scalper_pos.items():
        qty   = float(sv.get("qty", 0))
        entry = float(sv.get("entry", 0))
        cost  = qty * entry
        scalp_open_alloc += cost
        print(f"    {pair:15s}: qty={qty:.8f}  entry=€{entry:.4f}  alloc≈€{10:.2f}")
    print(f"  Scalper open alloc     : €{scalp_open_alloc:.4f}  "
          f"(€10 per position × {len(scalper_pos)})")
else:
    print("  Open scalper positions : (none)")
print()

# ── Reconciliation ─────────────────────────────────────────────────────────

# How the paper cash balance is built up:
#   start
#   + closed main P&L (buys deduct cash, sells add cash; net = pnl)
#   + closed scalper P&L (adjust_paper_balance; net = pnl)
#   - open long position costs (cash spent buying, not yet returned)
#   - open scalper allocations (€10 × n, not yet returned)
#   + open short proceeds (cash received from selling short, must repay)

expected_cash = (initial_balance
                 + main_pnl
                 + scalp_pnl
                 - long_open_cost
                 - scalp_open_alloc
                 + short_proceeds)

# True portfolio net worth — what you'd have if you closed EVERYTHING now:
#   cash (net of short liability) + long positions at market + scalper at entry + short unrealised
reported_cash      = float(bot_status.get("balance_eur", 0))
reported_portfolio = float(bot_status.get("portfolio_value", reported_cash))
reported_pnl       = float(bot_status.get("adjusted_pnl", 0))

# Long positions at current market (from bot_status open_positions)
open_positions   = bot_status.get("open_positions", {})
long_market_value = sum(
    float(v.get("qty", 0)) * float(v.get("current", v.get("entry", 0)))
    for v in open_positions.values()
)

true_net_worth = (reported_cash
                  - short_proceeds           # remove short liability from cash
                  + long_market_value        # long positions at current prices
                  + scalp_open_alloc         # scalper allocations (€10 each)
                  + short_unrealised)        # short unrealised P&L

cash_diff = reported_cash - expected_cash

print("=" * 65)
print("RECONCILIATION SUMMARY")
print("=" * 65)
print(f"  Initial balance          : €{initial_balance:.4f}")
print(f"  + Closed main P&L        : €{main_pnl:+.4f}  ({len(sell_trades)} trades)")
print(f"  + Closed scalper P&L     : €{scalp_pnl:+.4f}  ({len(scalper_trades)} trades)")
print(f"  - Open long costs        : €{-long_open_cost:.4f}")
print(f"  - Scalper open alloc     : €{-scalp_open_alloc:.4f}")
print(f"  + Short proceeds in cash : €{short_proceeds:+.4f}  (liability)")
print(f"  = Expected cash          : €{expected_cash:.4f}")
print(f"  Reported cash (EUR)      : €{reported_cash:.4f}")
cash_status = "OK" if abs(cash_diff) <= 0.05 else ("WARN" if abs(cash_diff) <= 2.0 else "MISMATCH")
print(f"  Cash diff                : €{cash_diff:+.4f}  [{cash_status}]")
print()
print(f"  ── True net worth (close everything now) ──────────────────")
print(f"  Cash (ex short liability): €{reported_cash - short_proceeds:.4f}")
print(f"  Long positions @ market  : €{long_market_value:.4f}")
print(f"  Scalper open @ entry     : €{scalp_open_alloc:.4f}")
print(f"  Short unrealised P&L     : €{short_unrealised:+.4f}")
print(f"  = TRUE NET WORTH         : €{true_net_worth:.4f}")
print()
print(f"  ── What the dashboard shows ───────────────────────────────")
print(f"  Reported portfolio_value : €{reported_portfolio:.4f}  ← includes short proceeds")
print(f"  Reported adjusted_pnl    : €{reported_pnl:+.4f}  ← vs initial cash {initial_balance:.2f}")
print(f"  (portfolio_value overstates by ~€{short_proceeds:.2f} due to open short proceeds)")
print()
print(f"  ── Real performance ───────────────────────────────────────")
total_closed_pnl = main_pnl + scalp_pnl
print(f"  Closed trade P&L only    : €{total_closed_pnl:+.4f}  "
      f"({total_closed_pnl/initial_balance*100:+.3f}% of starting capital)")
print(f"  Unrealised (longs)       : €{long_market_value - long_open_cost:+.4f}")
print(f"  Unrealised (shorts)      : €{short_unrealised:+.4f}")
print(f"  True total P&L           : €{true_net_worth - initial_balance:+.4f}  "
      f"({(true_net_worth - initial_balance)/initial_balance*100:+.3f}%)")
print()

if cash_status == "MISMATCH":
    print("  *** CASH MISMATCH — last 5 main trades:")
    for t in main_trades[-5:]:
        print(f"    [{t.get('ts','')[:19]}] {t.get('type','?'):12s} "
              f"{t.get('pair','?'):12s} pnl=€{float(t.get('pnl_eur',0)):+.4f}  "
              f"bal=€{float(t.get('balance_eur',0)):.4f}")
else:
    print("  *** Cash balance consistent with trade history. ***")
print("=" * 65)

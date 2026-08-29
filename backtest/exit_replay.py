#!/usr/bin/env python3
"""
Exit-geometry replay - what would a different exit policy have earned on the
trades the bot ACTUALLY took?

The daily journal report (journal_analysis.py) tells you how the trades you took
turned out. It cannot tell you how they WOULD have turned out under different
exits, because the journal only records the exit that happened. This tool fills
that gap: it rebuilds each trade's real price path from 5-minute candles and
replays alternative stop / take-profit / trailing / time-stop rules over it.

That distinction produced the 2026-08-29 exit rebuild. The journal said "we lose
money". The replay said why: gross price P&L was +3.59 EUR while fees were
-21.05 EUR, and the BREAK_EVEN exit was clipping a third of the book at +0.07 EUR
a trade. Entries were never the binding constraint.

Candles come from Binance's public klines endpoint, not Kraken: Kraken's OHLC
endpoint returns at most 720 candles and cannot page backwards, which is well
under a month at 5m resolution. Binance is used only as a price ORACLE - each
path is rescaled to the actual Kraken fill price, so the basis between venues
cancels and only the shape of the move matters.

Usage:
    python backtest/exit_replay.py                  # replay against the live config
    python backtest/exit_replay.py --journal FILE   # a different journal
    python backtest/exit_replay.py --no-cache       # force a candle refetch

Pure stdlib. Candles are cached under backtest/data/candles/ so repeat runs are
offline.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(HERE, "data", "candles")
DEFAULT_JOURNAL = os.path.join(HERE, "data", "trade_events_paper.jsonl")

# Kraken pair -> Binance symbol. EUR quote where it exists (closer to our book),
# USDT otherwise; either way only the percentage shape of the move is used.
SYMBOLS = {
    "XBTEUR": "BTCEUR", "XXBTZEUR": "BTCEUR",
    "ETHEUR": "ETHEUR", "XETHZEUR": "ETHEUR",
    "XRPEUR": "XRPEUR", "XXRPZEUR": "XRPEUR",
    "ADAEUR": "ADAEUR", "SOLEUR": "SOLEUR", "LTCEUR": "LTCEUR",
    "LINKEUR": "LINKEUR", "DOTEUR": "DOTEUR",
    "SUIEUR": "SUIUSDT", "ZECEUR": "ZECUSDT", "AAVEEUR": "AAVEUSDT",
    "PUMPEUR": "PUMPUSDT", "WLDEUR": "WLDUSDT", "HYPEEUR": "HYPEUSDT",
    "TAOEUR": "TAOUSDT", "AVAXEUR": "AVAXUSDT", "DOGEEUR": "DOGEUSDT",
}
MS_5M = 300_000


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #
def load_journal(path):
    """Return (sells, entry features, entry notionals). utf-8-sig survives a PS 5.1 BOM."""
    rows = []
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    sells = [r for r in rows if r.get("type") == "SELL"]
    feats, notional = {}, {}
    for b in rows:
        if b.get("type") != "BUY":
            continue
        key = (b["pair"], round(b.get("volume", 0), 10))
        feats[key] = (b.get("extra") or {}).get("features", {})
        notional[key] = b.get("volume", 0) * b.get("price", 0)
    return sells, feats, notional


def effective_fee_pct(sells, notional):
    """Round-trip fee the engine actually charged, derived from gross vs net P&L."""
    seen = []
    for s in sells:
        key = (s["pair"], round(s.get("volume", 0), 10))
        n = notional.get(key)
        gross = (s.get("extra") or {}).get("pnl_pct")
        if n and gross is not None:
            seen.append(gross - s.get("pnl_eur", 0) / n * 100)
    return statistics.median(seen) if seen else 0.52


# --------------------------------------------------------------------------- #
# candles
# --------------------------------------------------------------------------- #
def fetch_candles(pair, start_ms, end_ms, use_cache=True):
    sym = SYMBOLS.get(pair)
    if not sym:
        return None
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "%s_%d_%d.json" % (pair, start_ms // 86400000, end_ms // 86400000))
    if use_cache and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    out, cur = [], start_ms
    while cur < end_ms:
        url = ("https://api.binance.com/api/v3/klines"
               "?symbol=%s&interval=5m&startTime=%d&endTime=%d&limit=1000" % (sym, cur, end_ms))
        batch = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    batch = json.load(resp)
                break
            except Exception as exc:                       # noqa: BLE001
                if attempt == 3:
                    print("  ! %s (%s) fetch failed: %s" % (pair, sym, exc), file=sys.stderr)
                    return None
                time.sleep(2)
        if not batch:
            break
        out.extend([k[0], float(k[2]), float(k[3]), float(k[4])] for k in batch)
        if len(batch) < 1000:
            break
        cur = batch[-1][0] + MS_5M
        time.sleep(0.25)

    if out:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh)
    return out or None


def build_trades(sells, feats, notional, use_cache=True, forward_hours=72):
    """Attach a forward price path (rescaled to the Kraken fill) to every closed trade."""
    spans = defaultdict(lambda: [float("inf"), 0])
    for s in sells:
        entry_ms = (s.get("extra") or {}).get("entry_ts", 0) * 1000
        if not entry_ms:
            continue
        span = spans[s["pair"]]
        span[0] = min(span[0], entry_ms - MS_5M)
        span[1] = max(span[1], entry_ms + forward_hours * 3600 * 1000)

    candles = {}
    for pair, (lo, hi) in spans.items():
        if lo == float("inf"):
            continue
        got = fetch_candles(pair, int(lo), int(hi), use_cache)
        if got:
            candles[pair] = got

    trades, skipped = [], Counter()
    for s in sells:
        extra = s.get("extra") or {}
        pair, entry = s["pair"], extra.get("entry_price")
        entry_ms = extra.get("entry_ts", 0) * 1000
        series = candles.get(pair)
        if not (series and entry and entry_ms):
            skipped[pair] += 1
            continue
        idx = 0
        while idx < len(series) - 1 and series[idx + 1][0] <= entry_ms:
            idx += 1
        if series[idx][0] > entry_ms + 2 * MS_5M or idx >= len(series) - 12:
            skipped[pair] += 1
            continue
        scale = entry / series[idx][3]                     # rescale oracle -> Kraken fill
        path = [(c[1] * scale, c[2] * scale, c[3] * scale)
                for c in series[idx + 1: idx + 1 + forward_hours * 12]]
        if len(path) < 12:
            skipped[pair] += 1
            continue
        key = (pair, round(s.get("volume", 0), 10))
        trades.append({
            "pair": pair, "entry": entry, "path": path, "ts": entry_ms,
            "notional": notional.get(key) or s.get("volume", 0) * entry,
            "actual_eur": s.get("pnl_eur", 0), "actual_pct": extra.get("pnl_pct", 0),
            "reason": s.get("reason"), "feat": feats.get(key, {}),
        })
    if skipped:
        print("  (no usable candles for %d trades: %s)"
              % (sum(skipped.values()),
                 ", ".join("%s x%d" % (k, v) for k, v in skipped.most_common(5))))
    return trades


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #
def simulate(trade, stop, tp, trail_min, trail_dist, hours,
             break_even_trigger=None, break_even_offset=0.7):
    """
    Mirror the bot's long-exit ladder over a real price path.

    Within a 5m candle the true high/low ordering is unknown, so adverse levels
    are always tested BEFORE favourable ones. The replay is therefore pessimistic
    by construction - treat its numbers as a floor, not a forecast.
    """
    entry, peak = trade["entry"], 0.0
    bars = int(hours * 12)
    for high, low, close in trade["path"][:bars]:
        low_pct = (low / entry - 1) * 100
        high_pct = (high / entry - 1) * 100
        if break_even_trigger and peak >= break_even_trigger and low_pct <= break_even_offset:
            return break_even_offset, "BREAK_EVEN"
        if trail_dist and peak >= trail_min + trail_dist and low_pct <= peak - trail_dist:
            return max(peak - trail_dist, trail_min), "TRAILING_STOP"
        if low_pct <= -stop:
            return -stop, "STOP_LOSS"
        if tp and high_pct >= tp:
            return tp, "TAKE_PROFIT"
        peak = max(peak, high_pct)
    last = trade["path"][min(bars, len(trade["path"])) - 1]
    return (last[2] / entry - 1) * 100, "TIME_STOP"


def evaluate(trades, fee, **policy):
    per, reasons = [], Counter()
    for t in trades:
        gross, reason = simulate(t, **policy)
        per.append((gross - fee) / 100 * t["notional"])
        reasons[reason] += 1
    ordered = sorted(per)
    return {
        "net": sum(per),
        "win_rate": sum(1 for p in per if p > 0) / len(per) * 100 if per else 0.0,
        "median": statistics.median(per) if per else 0.0,
        "net_ex_top3": sum(ordered[:-3]) if len(per) > 3 else 0.0,
        "reasons": reasons,
        "per_trade": per,
    }


def policy_from_config(cfg):
    """The exit policy the bot is running right now, as the replay understands it."""
    rm = cfg.get("risk_management", {})
    stop = max(float(rm.get("stop_loss_percent", 1.5)),
               float(rm.get("min_stop_loss_percent", 0)))
    tp = float(rm.get("take_profit_percent", 0)) or None
    be = (float(rm.get("break_even_trigger_percent", 0))
          if rm.get("enable_break_even", True) else None)
    return {
        "stop": stop,
        "tp": tp,
        "trail_min": float(rm.get("trailing_stop_min_gain_pct", 0.8)),
        "trail_dist": float(rm.get("trailing_stop_percent", 1.5)),
        "hours": float(rm.get("time_stop_hours", 24)),
        "break_even_trigger": be,
        "break_even_offset": float(rm.get("break_even_offset_pct", 0.7)),
    }


def load_config(path):
    try:
        import tomllib
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except Exception as exc:                               # noqa: BLE001
        print("  ! could not read %s: %s" % (path, exc))
        return {}


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--journal", default=DEFAULT_JOURNAL)
    ap.add_argument("--config", default=os.path.join(ROOT, "config.paper.toml"))
    ap.add_argument("--no-cache", action="store_true", help="refetch candles")
    args = ap.parse_args()

    if not os.path.exists(args.journal):
        print("journal not found: %s" % args.journal)
        return 1

    sells, feats, notional = load_journal(args.journal)
    if not sells:
        print("no closed trades in journal")
        return 1
    fee = effective_fee_pct(sells, notional)

    print("\nEXIT REPLAY - %d closed trades, effective round-trip fee %.3f%%"
          % (len(sells), fee))
    print("fetching candles...")
    trades = build_trades(sells, feats, notional, use_cache=not args.no_cache)
    if not trades:
        print("no trades could be reconstructed")
        return 1
    print("reconstructed %d/%d trades\n" % (len(trades), len(sells)))

    # ---- where the money actually went ------------------------------------
    gross = sum(t["actual_pct"] / 100 * t["notional"] for t in trades)
    fees_paid = sum(fee / 100 * t["notional"] for t in trades)
    turnover = sum(t["notional"] for t in trades)
    moves = [t["actual_pct"] for t in trades]
    print("STRUCTURAL ECONOMICS (as traded)")
    print("  gross price P&L   %+9.2f EUR" % gross)
    print("  fees              %+9.2f EUR" % -fees_paid)
    print("  net               %+9.2f EUR" % sum(t["actual_eur"] for t in trades))
    print("  turnover          %9.0f EUR" % turnover)
    print("  median gross move %+.3f%% vs fee %.3f%% -> %.0f%% of trades can cover their own fees\n"
          % (statistics.median(moves), fee,
             sum(1 for m in moves if m > fee) / len(moves) * 100))

    # ---- exits, as they happened ------------------------------------------
    by_reason = defaultdict(list)
    for t in trades:
        by_reason[t["reason"]].append(t["actual_eur"])
    print("ACTUAL EXITS")
    print("  %-18s%4s%10s%9s" % ("reason", "n", "net EUR", "avg"))
    for reason, vals in sorted(by_reason.items(), key=lambda kv: -sum(kv[1])):
        print("  %-18s%4d%10.2f%9.3f"
              % (reason, len(vals), sum(vals), sum(vals) / len(vals)))

    # ---- live policy vs alternatives ---------------------------------------
    cfg = load_config(args.config)
    live = policy_from_config(cfg)
    print("\nLIVE CONFIG POLICY: stop %s%% tp %s%% trail>=%s/-%s %sh break_even %s"
          % (live["stop"], live["tp"], live["trail_min"], live["trail_dist"],
             live["hours"], live["break_even_trigger"]))

    candidates = {
        "LIVE CONFIG": live,
        "old geometry (pre 2026-08-29)": dict(stop=1.5, tp=2.0, trail_min=0.8,
                                              trail_dist=1.5, hours=24,
                                              break_even_trigger=1.0),
        "no take-profit cap": dict(live, tp=None),
        "stop 2%": dict(live, stop=2.0),
        "stop 4%": dict(live, stop=4.0),
        "24h time stop": dict(live, hours=24),
        "break-even re-armed": dict(live, break_even_trigger=1.0),
    }
    print("\nPOLICY COMPARISON  (median = typical trade; ex-top3 = survives without the outliers?)")
    print("  %-34s%10s%7s%9s%10s" % ("policy", "net EUR", "WR", "median", "ex-top3"))
    for label, pol in candidates.items():
        r = evaluate(trades, fee, **pol)
        print("  %-34s%+10.2f%6.1f%%%+9.2f%+10.2f"
              % (label, r["net"], r["win_rate"], r["median"], r["net_ex_top3"]))

    # ---- entry buckets under one fixed exit --------------------------------
    print("\nENTRY QUALITY under the LIVE exit policy (n>=4 buckets)")

    def bucket(name, keyfn):
        groups = defaultdict(list)
        for t in trades:
            try:
                k = keyfn(t["feat"])
            except Exception:                              # noqa: BLE001
                continue
            if k is not None:
                groups[k].append(t)
        rows = []
        for k, group in groups.items():
            if len(group) < 4:
                continue
            r = evaluate(group, fee, **live)
            rows.append((r["net"], str(k), len(group), r["win_rate"], r["median"]))
        if not rows:
            return
        print("  -- %s" % name)
        for net, k, n, wr, med in sorted(rows, reverse=True):
            print("     %-20s%4d%6.1f%%%+9.2f  median %+.2f" % (k, n, wr, net, med))

    bucket("strategy", lambda f: f.get("strategy"))
    bucket("trade desk", lambda f: "agent" if f.get("agent_decided") else "rules")
    bucket("ichimoku vs cloud", lambda f: f.get("ichi_vs_cloud"))
    bucket("smart action", lambda f: f.get("smart_action"))
    bucket("1h RSI", lambda f: ("<50" if f["rsi_1h"] < 50
                                else "50-60" if f["rsi_1h"] < 60 else ">=60"))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

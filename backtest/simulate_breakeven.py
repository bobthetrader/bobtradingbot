#!/usr/bin/env python3
"""
Breakeven stop simulation.

For each STOP_LOSS and TIMEOUT trade in the historical log, fetches 1-min
OHLCV from the Kraken public API and checks whether price ever reached the
fee-recovery level (entry + round_trip_fee%) during the hold window.

If it did, the trade would have exited as BREAKEVEN at ~0% net P&L rather
than at the actual SL/timeout exit price.

Usage:
    python backtest\\simulate_breakeven.py              # sync from Docker first
    python backtest\\simulate_breakeven.py --no-sync    # use cached trade data
    python backtest\\simulate_breakeven.py --round-trip 0.80  # override fee %
"""

import argparse
import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

HERE         = Path(__file__).parent
DATA_DIR     = HERE / "data"
DOCKER_VOLUME = "tradingbot_tradingbot_data"
KRAKEN_OHLC  = "https://api.kraken.com/0/public/OHLC"

# Default round-trip fee at base tier (< $10k 30-day volume)
DEFAULT_ROUND_TRIP = 0.80


# ── Helpers ───────────────────────────────────────────────────────────────────

def sync_trades():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Syncing scalper_trades.jsonl from Docker volume {DOCKER_VOLUME}…")
    r = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{DOCKER_VOLUME}:/data",
         "alpine", "cat", "/data/scalper_trades.jsonl"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0 or not r.stdout.strip():
        print("Docker sync failed — using cached data if available")
        return
    dest = DATA_DIR / "scalper_trades.jsonl"
    dest.write_text(r.stdout, encoding="utf-8")
    count = sum(1 for l in r.stdout.splitlines() if l.strip())
    print(f"  Synced {count} trade records")


def load_trades(path: Path) -> list:
    trades = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return trades


def trade_window(t: dict) -> tuple[float, float]:
    """Return (open_unix, close_unix) for a trade."""
    held_min = float(t.get("held_min", 0))
    # close timestamp
    ts_raw = t.get("ts") or t.get("timestamp")
    if isinstance(ts_raw, str):
        close_ts = datetime.fromisoformat(
            ts_raw.replace("Z", "+00:00")).timestamp()
    else:
        close_ts = float(ts_raw or 0)

    # open timestamp — prefer explicit field, fall back to close - held
    open_raw = t.get("open_ts")
    if open_raw:
        if isinstance(open_raw, str):
            open_ts = datetime.fromisoformat(
                open_raw.replace("Z", "+00:00")).timestamp()
        else:
            open_ts = float(open_raw)
    else:
        open_ts = close_ts - held_min * 60

    return open_ts, close_ts


def fetch_ohlcv_range(pair: str, since: float, until: float) -> list:
    """
    Fetch 1-min OHLCV bars from Kraken covering [since, until].
    Returns list of (time, high) tuples.
    Paginates if needed (each call covers up to 720 bars = 12 h).
    """
    bars = []
    cursor = int(since)
    until_i = int(until) + 60  # +1 bar to ensure we cover the close minute

    for _ in range(10):  # safety cap on pagination
        try:
            r = requests.get(
                KRAKEN_OHLC,
                params={"pair": pair, "interval": 1, "since": cursor},
                timeout=15,
            )
            data = r.json()
        except Exception as exc:
            print(f"    API error for {pair}: {exc}")
            break

        if data.get("error"):
            # Pair might use a different name in the API — skip silently
            break

        result = data.get("result", {})
        raw = result.get(pair) or next(
            (v for k, v in result.items() if k != "last"), None
        )
        if not raw:
            break

        for bar in raw:
            bar_ts = int(bar[0])
            if bar_ts > until_i:
                break
            if bar_ts >= int(since):
                bars.append((bar_ts, float(bar[2])))  # (time, high)

        last_ts = int(raw[-1][0])
        if last_ts >= until_i or last_ts <= cursor:
            break
        cursor = last_ts
        time.sleep(0.25)  # ~4 req/s — within Kraken public limits

    return bars


# ── Core simulation ───────────────────────────────────────────────────────────

def simulate(trades: list, round_trip_pct: float) -> dict:
    # Only simulate SL and TIMEOUT — TP is unaffected (always passed through BE)
    candidates = [t for t in trades if t.get("reason") in ("STOP_LOSS", "TIMEOUT")]
    total = len(trades)
    sl_count = sum(1 for t in trades if t.get("reason") == "STOP_LOSS")
    to_count = sum(1 for t in trades if t.get("reason") == "TIMEOUT")
    tp_count = sum(1 for t in trades if t.get("reason") == "TAKE_PROFIT")

    print(f"\nTrades: {total} total  |  {sl_count} STOP_LOSS  |  {to_count} TIMEOUT  |  {tp_count} TAKE_PROFIT")
    print(f"Breakeven activation threshold: +{round_trip_pct:.2f}% (fee recovery)\n")

    # Group by pair to minimise API calls
    by_pair: dict[str, list] = defaultdict(list)
    for t in candidates:
        pair = t.get("pair", "")
        if pair:
            by_pair[pair].append(t)

    # Per-trade result: True = would have hit BE floor and reversed
    converted: list[dict] = []  # trades that become BREAKEVEN
    not_converted: list[dict] = []
    failed_pairs: set = set()

    total_pairs = len(by_pair)
    for pi, (pair, pair_trades) in enumerate(sorted(by_pair.items()), 1):
        print(f"  [{pi}/{total_pairs}] {pair}: {len(pair_trades)} trades", end="", flush=True)

        # Sort trades by open time
        pair_trades_sorted = sorted(pair_trades, key=lambda t: trade_window(t)[0])

        # Fetch OHLCV in as few calls as possible
        # Each Kraken call covers up to 720 min. Batch trades within 720-min windows.
        windows_covered: dict[int, float] = {}  # bar_ts → high

        first_open, _ = trade_window(pair_trades_sorted[0])
        _, last_close = trade_window(pair_trades_sorted[-1])

        bars = fetch_ohlcv_range(pair, first_open - 60, last_close + 60)
        if not bars:
            failed_pairs.add(pair)
            print(f" [no OHLCV data]")
            for t in pair_trades:
                not_converted.append(t)
            continue

        bar_lookup: dict[int, float] = {ts: high for ts, high in bars}

        pair_converted = 0
        for t in pair_trades:
            entry = float(t.get("entry") or t.get("entry_price") or t.get("buy_price") or 0)
            if not entry:
                not_converted.append(t)
                continue

            be_target = entry * (1 + round_trip_pct / 100)
            open_ts, close_ts = trade_window(t)

            # Check if any bar's HIGH in the hold window >= be_target.
            # Bar timestamps are bar-open (on-the-minute), so include the bar that
            # started up to 60s before our open_ts to avoid off-by-one misses.
            hit_be = any(
                high >= be_target
                for ts, high in bar_lookup.items()
                if (open_ts - 60) <= ts <= close_ts
            )

            if hit_be:
                converted.append(t)
                pair_converted += 1
            else:
                not_converted.append(t)

        print(f" {pair_converted} would convert")

        time.sleep(0.1)

    # ── Results ───────────────────────────────────────────────────────────────
    be_from_sl = [t for t in converted if t.get("reason") == "STOP_LOSS"]
    be_from_to = [t for t in converted if t.get("reason") == "TIMEOUT"]

    avg_sl_pnl = (sum(t.get("pnl_pct", 0) for t in trades if t.get("reason") == "STOP_LOSS")
                  / sl_count) if sl_count else 0
    avg_to_pnl = (sum(t.get("pnl_pct", 0) for t in trades if t.get("reason") == "TIMEOUT")
                  / to_count) if to_count else 0
    avg_tp_pnl = (sum(t.get("pnl_pct", 0) for t in trades if t.get("reason") == "TAKE_PROFIT")
                  / tp_count) if tp_count else 0

    # BE exit P&L ≈ round_trip% price gain - round_trip% fees = 0%
    be_pnl_pct = 0.0

    # Old total P&L sum across all trades
    old_total = sum(t.get("pnl_pct", 0) for t in trades)
    # New total: converted trades change from their old P&L to be_pnl_pct
    gain_per_converted_sl = be_pnl_pct - avg_sl_pnl
    gain_per_converted_to = be_pnl_pct - avg_to_pnl  # TIMEOUT conversions: use their actual P&L
    # For individual converted TIMEOUT trades, use actual pnl_pct (not avg)
    new_total = old_total + sum(
        be_pnl_pct - t.get("pnl_pct", 0) for t in converted
    )

    old_wr = sum(1 for t in trades if t.get("pnl_pct", 0) > 0) / total * 100
    # After conversion: converted SL trades → BE at 0% (not a win), converted TIMEOUT negatives → 0%
    new_wins = (
        sum(1 for t in trades if t.get("pnl_pct", 0) > 0 and t not in converted)
        + sum(1 for t in converted if be_pnl_pct > 0)  # BE trades: 0% = not a win
    )
    new_wr = new_wins / total * 100

    # SL rate change
    new_sl_count = sl_count - len(be_from_sl)
    new_be_count = len(converted)

    return {
        "total_trades":        total,
        "round_trip_pct":      round_trip_pct,
        "sl_converted":        len(be_from_sl),
        "timeout_converted":   len(be_from_to),
        "total_converted":     len(converted),
        "failed_pairs":        sorted(failed_pairs),
        "sl_rate_before":      round(sl_count / total * 100, 1),
        "sl_rate_after":       round(new_sl_count / total * 100, 1),
        "be_rate":             round(new_be_count / total * 100, 1),
        "old_win_rate":        round(old_wr, 1),
        "new_win_rate":        round(new_wr, 1),
        "old_avg_pnl_per_trade": round(old_total / total, 4),
        "new_avg_pnl_per_trade": round(new_total / total, 4),
        "old_total_pnl_pct":   round(old_total, 2),
        "new_total_pnl_pct":   round(new_total, 2),
        "pnl_improvement_pct": round(new_total - old_total, 2),
        "avg_sl_pnl":          round(avg_sl_pnl, 4),
        "avg_tp_pnl":          round(avg_tp_pnl, 4),
        "avg_to_pnl":          round(avg_to_pnl, 4),
    }


def print_report(r: dict):
    print("\n" + "=" * 60)
    print("  BREAKEVEN STOP SIMULATION RESULTS")
    print("=" * 60)
    print(f"  Trades analysed:       {r['total_trades']}")
    print(f"  Round-trip fee used:   {r['round_trip_pct']}%  (activation threshold)")
    print()
    print(f"  Converted STOP_LOSS -> BREAKEVEN: {r['sl_converted']}"
          f"  ({r['sl_converted']/r['total_trades']*100:.1f}% of all trades)")
    print(f"  Converted TIMEOUT   -> BREAKEVEN: {r['timeout_converted']}"
          f"  ({r['timeout_converted']/r['total_trades']*100:.1f}% of all trades)")
    print(f"  Total converted:       {r['total_converted']}")
    print()
    print(f"  Exit reason breakdown:")
    print(f"    STOP_LOSS rate:   {r['sl_rate_before']}%  ->  {r['sl_rate_after']}%")
    print(f"    BREAKEVEN rate:   0.0%  ->  {r['be_rate']}%")
    print()
    print(f"  Win rate:          {r['old_win_rate']}%  ->  {r['new_win_rate']}%")
    print(f"    (BREAKEVEN trades count as 0% P&L, not a win)")
    print()
    print(f"  Avg P&L per trade: {r['old_avg_pnl_per_trade']:+.4f}%  ->  {r['new_avg_pnl_per_trade']:+.4f}%")
    print(f"  Total P&L sum:     {r['old_total_pnl_pct']:+.2f}%  ->  {r['new_total_pnl_pct']:+.2f}%")
    print(f"  P&L improvement:   {r['pnl_improvement_pct']:+.2f}% across {r['total_trades']} trades")
    print()
    if r["failed_pairs"]:
        print(f"  No OHLCV data for: {r['failed_pairs']}")
    print("=" * 60)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sync",    action="store_true", help="Skip Docker sync")
    ap.add_argument("--data",       default=None, help="Path to scalper_trades.jsonl")
    ap.add_argument("--round-trip", type=float, default=None,
                    help=f"Round-trip fee %% (default: read from kraken_fees.json or {DEFAULT_ROUND_TRIP})")
    args = ap.parse_args()

    data_path = Path(args.data) if args.data else DATA_DIR / "scalper_trades.jsonl"
    if not args.no_sync and not args.data:
        sync_trades()

    if not data_path.exists():
        print(f"No trade data at {data_path}")
        sys.exit(1)

    trades = load_trades(data_path)
    print(f"Loaded {len(trades)} trades from {data_path}")

    # Determine round-trip fee
    if args.round_trip is not None:
        round_trip = args.round_trip
        print(f"Using round-trip fee: {round_trip}% (from --round-trip flag)")
    else:
        fees_path = DATA_DIR / "kraken_fees.json"
        if fees_path.exists():
            try:
                fees = json.loads(fees_path.read_text(encoding="utf-8"))
                tiers = fees.get("taker_tiers", [])
                if tiers:
                    taker = float(tiers[0][1]) / 100  # first tier rate
                    round_trip = round(taker * 2, 4)
                    print(f"Using round-trip fee: {round_trip}% (from kraken_fees.json)")
                else:
                    round_trip = DEFAULT_ROUND_TRIP
            except Exception:
                round_trip = DEFAULT_ROUND_TRIP
        else:
            round_trip = DEFAULT_ROUND_TRIP
        if round_trip == DEFAULT_ROUND_TRIP:
            print(f"Using round-trip fee: {round_trip}% (base-tier default)")

    results = simulate(trades, round_trip)
    print_report(results)

    out = DATA_DIR / "breakeven_simulation.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nResults saved to {out}")


if __name__ == "__main__":
    main()

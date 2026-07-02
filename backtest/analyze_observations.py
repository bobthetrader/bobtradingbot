#!/usr/bin/env python3
"""
Signal-excursion analyzer.

Reads the observations logged by core/scalper_probe.py (data/scalper_observations.jsonl)
and answers two questions the closed-trade log cannot:

  1. SIGNAL EDGE — after our entries, does price move up more than down?
     Reports MFE (max favorable) vs MAE (max adverse) distributions. If MAE
     dominates MFE, no geometry saves the signal.

  2. GEOMETRY — for a grid of TP/SL, simulates FIRST-TOUCH on each observation's
     recorded 1-min path (did +TP or -SL hit first?) and reports net-of-fee win
     rate, average P&L and total EV. Picks the net-EV-optimal geometry.

Usage:
    py backtest\\analyze_observations.py
    py backtest\\analyze_observations.py --data data\\scalper_observations.jsonl
    py backtest\\analyze_observations.py --round-trip 0.70 --min-vwap-dev 0.05
"""

import argparse
import json
import statistics
from pathlib import Path

HERE = Path(__file__).parent
DEFAULT_PATHS = [
    HERE / "data" / "scalper_observations.jsonl",
    HERE.parent / "data" / "scalper_observations.jsonl",
]

TP_GRID = [2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0]
SL_GRID = [0.5, 1.0, 1.5, 2.0, 3.0]


def load(path: Path) -> list:
    rows = []
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def pctile(vals, p):
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = (len(vals) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def first_touch(obs: dict, tp: float, sl: float, rt: float):
    """Walk the recorded path; return net-of-fee pnl% for a TP/SL bracket.

    Conservative on same-bar ambiguity: if a bar touches both TP and SL, assume
    SL hit first (worst case). Timeout = exit at the last bar close.
    """
    entry = obs["entry"]
    if not entry:
        return None
    tp_lvl = entry * (1 + tp / 100)
    sl_lvl = entry * (1 - sl / 100)
    path = obs.get("path") or []
    if not path:
        return None
    last_close = entry
    for bar in path:
        hi, lo, close = bar[0], bar[1], (bar[2] if len(bar) > 2 else bar[1])
        last_close = close
        hit_tp = hi >= tp_lvl
        hit_sl = lo <= sl_lvl
        if hit_tp and hit_sl:
            return -sl - rt            # ambiguous -> assume stop first
        if hit_tp:
            return tp - rt
        if hit_sl:
            return -sl - rt
    return (last_close - entry) / entry * 100 - rt   # timeout at last close


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=None)
    ap.add_argument("--round-trip", type=float, default=0.70,
                    help="round-trip fee %% subtracted from every simulated exit")
    ap.add_argument("--min-vwap-dev", type=float, default=None,
                    help="only keep observations whose entry vwap_dev >= this")
    args = ap.parse_args()

    path = Path(args.data) if args.data else next((p for p in DEFAULT_PATHS if p.exists()), None)
    if not path or not path.exists():
        print("No observations file found. Looked in:")
        for p in DEFAULT_PATHS:
            print(f"  {p}")
        print("Run the probe ([scalper] probe_enabled=true) to collect data first.")
        return

    rows = load(path)
    if args.min_vwap_dev is not None:
        rows = [r for r in rows
                if (r.get("entry_signals", {}).get("vwap_dev") or -999) >= args.min_vwap_dev]

    rt = args.round_trip
    print(f"Loaded {len(rows)} observations from {path}")
    print(f"Round-trip fee = {rt}%" +
          (f"  |  filter: vwap_dev >= {args.min_vwap_dev}" if args.min_vwap_dev is not None else ""))
    if not rows:
        return

    # ── 1. Signal edge: MFE vs MAE ──────────────────────────────────────────────
    mfe = [r.get("mfe_pct", 0) for r in rows]
    mae = [r.get("mae_pct", 0) for r in rows]
    print("\n=== SIGNAL EDGE (excursion after entry) ===")
    print(f"  MFE (max favorable): median {statistics.median(mfe):+.2f}%  "
          f"p25 {pctile(mfe,25):+.2f}%  p75 {pctile(mfe,75):+.2f}%  p90 {pctile(mfe,90):+.2f}%  max {max(mfe):+.2f}%")
    print(f"  MAE (max adverse) : median {statistics.median(mae):+.2f}%  "
          f"p25 {pctile(mae,25):+.2f}%  p75 {pctile(mae,75):+.2f}%  p10 {pctile(mae,10):+.2f}%  min {min(mae):+.2f}%")
    edge = statistics.median(mfe) + statistics.median(mae)  # mae is negative
    verdict = ("favorable — MFE > |MAE|" if edge > 0 else
               "NO EDGE — adverse move dominates, geometry can't save it")
    print(f"  median MFE + median MAE = {edge:+.2f}%  ->  {verdict}")

    # ── 2. Geometry grid: first-touch net EV ────────────────────────────────────
    print("\n=== TP/SL FIRST-TOUCH NET EV (per observation, net of fee) ===")
    header = "  SL\\TP " + "".join(f"{tp:>9.1f}" for tp in TP_GRID)
    print(header)
    best = None
    for sl in SL_GRID:
        cells = []
        for tp in TP_GRID:
            pnls = [first_touch(o, tp, sl, rt) for o in rows]
            pnls = [p for p in pnls if p is not None]
            if not pnls:
                cells.append("     —")
                continue
            avg = statistics.mean(pnls)
            cells.append(f"{avg:>+8.2f}%")
            if best is None or avg > best[0]:
                best = (avg, tp, sl, pnls)
        print(f"  {sl:>4.1f} " + "".join(f"{c:>9}" for c in cells))
    print("  (cell = average net-of-fee P&L% per trade for that TP/SL bracket)")

    if best:
        avg, tp, sl, pnls = best
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / len(pnls) * 100
        tot = sum(pnls)
        print(f"\nBest net-EV geometry: TP={tp}% / SL={sl}%")
        print(f"  net WR {wr:.1f}%  |  avg {avg:+.3f}%/trade  |  total {tot:+.1f}% over {len(pnls)} obs")
        if avg <= 0:
            print("  NOTE: best cell is still <=0 net — no tested geometry is profitable on this data yet.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Scalper trade outcome analyser / backtester.

Pulls real trade data from the local Docker volume (which mirrors the server)
and produces a self-contained HTML report covering:
  - Overview stats and exit reasons
  - Entry RSI / VWAP / score analysis
  - Exit signal analysis (RSI, VWAP, OB at close)
  - Grid search: which entry thresholds produced the best win rate
  - Time-of-day and day-of-week breakdown
  - Per-pair performance
  - AI param regime comparison (which rsi_buy / score_thresh worked best)
  - Concurrent position analysis

Usage:
    python backtest\\scalper_backtest.py              # sync from Docker + analyse
    python backtest\\scalper_backtest.py --no-sync    # use cached local data
    python backtest\\scalper_backtest.py --data FILE  # use a specific JSONL file

Output: backtest\\reports\\scalper_YYYY-MM-DD.html
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import pandas as pd
    import numpy as np
except ImportError:
    sys.exit("Run:  pip install pandas numpy")

# ── Config ────────────────────────────────────────────────────────────────────

HERE          = Path(__file__).parent
DATA_DIR      = HERE / "data"
REPORT_DIR    = HERE / "reports"
DOCKER_VOLUME = "tradingbot_tradingbot_data"
SYNC_FILES    = ["scalper_trades.jsonl", "scalper_ai_adjustments.jsonl",
                 "scalper_ai_state.json"]

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

RSI_BUY_GRID    = [25, 27, 28, 29, 30, 31, 32, 33, 35]
SCORE_GRID      = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
MIN_TRADES_GRID = 5   # minimum trades in a grid cell to show a result


# ── Docker sync ───────────────────────────────────────────────────────────────

def sync_from_docker():
    """Copy trade files from the local Docker volume into backtest/data/."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Syncing from Docker volume: {DOCKER_VOLUME}")
    synced = 0
    for fname in SYNC_FILES:
        result = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{DOCKER_VOLUME}:/data",
             "alpine", "cat", f"/data/{fname}"],
            capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode == 0 and result.stdout.strip():
            dest = DATA_DIR / fname
            dest.write_text(result.stdout, encoding="utf-8")
            lines = len([l for l in result.stdout.splitlines() if l.strip()])
            print(f"  {fname}: {lines} records")
            synced += 1
        else:
            print(f"  {fname}: not found or empty (skipping)")
    if synced == 0:
        sys.exit("No files synced — is Docker running? Is the bot container started?")


# ── Data loading ──────────────────────────────────────────────────────────────

def load_trades(path: Path) -> pd.DataFrame:
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass

    if not records:
        sys.exit(f"No trades found in {path}")

    df = pd.DataFrame(records)

    # Parse timestamps
    for col in ("open_ts", "ts"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")

    # Back-fill entry_hour / weekday from open_ts for older records without these fields
    if "open_ts" in df.columns:
        mask = df.get("entry_hour_utc", pd.Series(dtype=float)).isna()
        if mask.any() or "entry_hour_utc" not in df.columns:
            df["entry_hour_utc"] = df["open_ts"].dt.hour
        mask = df.get("entry_weekday", pd.Series(dtype=float)).isna()
        if mask.any() or "entry_weekday" not in df.columns:
            df["entry_weekday"] = df["open_ts"].dt.weekday
    elif "ts" in df.columns:
        # Older records: ts is exit time — entry time derived from held_min
        df["entry_hour_utc"] = (df["ts"] - pd.to_timedelta(df.get("held_min", 0), unit="m")).dt.hour
        df["entry_weekday"]  = (df["ts"] - pd.to_timedelta(df.get("held_min", 0), unit="m")).dt.weekday

    df["win"] = df["pnl_eur"] > 0

    for col in ["entry_rsi", "entry_vwap_dev", "entry_score", "entry_ob_imbalance",
                "exit_rsi", "exit_vwap_dev", "exit_ob_imbalance",
                "pnl_pct", "pnl_eur", "held_min",
                "param_rsi_buy", "param_rsi_sell", "param_score_thresh", "param_vwap_thresh",
                "entry_hour_utc", "entry_weekday", "concurrent_positions",
                "btc_bear_at_entry"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


# ── Stats helpers ─────────────────────────────────────────────────────────────

def wr(series: pd.Series) -> float:
    return round(series.mean() * 100, 1) if len(series) else 0.0

def sharpe(pnl: pd.Series) -> str:
    if len(pnl) < 2 or pnl.std() == 0:
        return "—"
    return f"{pnl.mean() / pnl.std():.2f}"

def col_wr(v: float) -> str:
    return "#00c851" if v >= 55 else ("#ffbb33" if v >= 45 else "#ff4444")

def col_pnl(v: float) -> str:
    return "#00c851" if v >= 0 else "#ff4444"


# ── HTML helpers ──────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; }
body { background:#0d1117; color:#c9d1d9; font-family:system-ui,sans-serif;
       padding:24px; margin:0; }
h1 { color:#e6edf3; border-bottom:1px solid #21262d; padding-bottom:12px; }
h2 { color:#e6edf3; margin-top:36px; font-size:18px; }
h3 { color:#8b949e; font-size:14px; margin:20px 0 8px; }
table { border-collapse:collapse; width:100%; margin-bottom:20px; font-size:13px; }
th { background:#161b22; color:#8b949e; text-align:left; padding:8px 12px;
     border-bottom:2px solid #21262d; white-space:nowrap; }
td { padding:6px 12px; border-bottom:1px solid #21262d; }
tr:hover td { background:#161b22; }
.cards { display:flex; flex-wrap:wrap; gap:12px; margin-bottom:24px; }
.card { background:#161b22; border:1px solid #21262d; border-radius:8px;
        padding:16px 20px; min-width:140px; }
.stat { font-size:26px; font-weight:bold; margin:4px 0; }
.label { font-size:11px; color:#8b949e; }
.note { color:#8b949e; font-size:12px; margin-bottom:12px; }
.section { margin-bottom:8px; }
"""

def card(label: str, value: str, colour: str = "#e6edf3") -> str:
    return (f'<div class="card"><div class="label">{label}</div>'
            f'<div class="stat" style="color:{colour}">{value}</div></div>')

def tbl(headers: list, rows: list) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = ""
    for row in rows:
        body += "<tr>"
        for cell in row:
            if isinstance(cell, tuple):
                val, style = cell
                body += f'<td style="{style}">{val}</td>'
            else:
                body += f"<td>{cell}</td>"
        body += "</tr>"
    return f"<table><tr>{head}</tr>{body}</table>"


# ── Report sections ───────────────────────────────────────────────────────────

def s_overview(df: pd.DataFrame) -> str:
    n         = len(df)
    wins      = int(df["win"].sum())
    wr_all    = wr(df["win"])
    total_pnl = df["pnl_eur"].sum()
    avg_held  = df["held_min"].mean()

    date_range = "—"
    if "open_ts" in df.columns and df["open_ts"].notna().any():
        first = df["open_ts"].min().strftime("%Y-%m-%d")
        last  = df["open_ts"].max().strftime("%Y-%m-%d")
        date_range = f"{first} → {last}"

    cards = (
        card("Trades", str(n))
        + card("Win rate", f"{wr_all}%", col_wr(wr_all))
        + card("W / L", f"{wins} / {n - wins}")
        + card("Total P&L", f"€{total_pnl:+.2f}", col_pnl(total_pnl))
        + card("Avg hold", f"{avg_held:.1f}m")
        + card("Data range", date_range)
    )

    reason_rows = []
    for reason, g in df.groupby("reason"):
        wr2 = wr(g["win"])
        reason_rows.append([
            reason, len(g),
            (f"{wr2}%", f"color:{col_wr(wr2)};font-weight:bold"),
            (f"{g['pnl_pct'].mean():+.3f}%", f"color:{col_pnl(g['pnl_pct'].mean())}"),
            f"{g['held_min'].mean():.1f}m",
            sharpe(g["pnl_pct"]),
        ])

    return (
        f'<h2>Overview</h2><div class="cards">{cards}</div>'
        f"<h3>Exit Reasons</h3>"
        + tbl(["Reason", "Count", "Win Rate", "Avg P&L %", "Avg Hold", "Sharpe"],
              reason_rows)
    )


def s_time(df: pd.DataFrame) -> str:
    if "entry_hour_utc" not in df.columns or df["entry_hour_utc"].isna().all():
        return "<h2>Time of Day</h2><p class='note'>No data yet.</p>"

    hour_rows = []
    for h in range(24):
        g = df[df["entry_hour_utc"] == h]
        if len(g) < 2:
            continue
        wr2 = wr(g["win"])
        avg_pnl = g["pnl_pct"].mean()
        bar_width = min(int(wr2 * 0.8), 80)
        bar = (f'<div style="display:inline-block;width:{bar_width}px;height:8px;'
               f'background:{col_wr(wr2)};border-radius:2px;vertical-align:middle;'
               f'margin-right:6px"></div>')
        hour_rows.append([
            f"{h:02d}:00 UTC",
            len(g),
            (f"{bar}{wr2}%", f"color:{col_wr(wr2)};font-weight:bold"),
            (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
            f"{g['held_min'].mean():.1f}m",
        ])

    day_rows = []
    if "entry_weekday" in df.columns and df["entry_weekday"].notna().any():
        for d in range(7):
            g = df[df["entry_weekday"] == d]
            if len(g) < 2:
                continue
            wr2 = wr(g["win"])
            avg_pnl = g["pnl_pct"].mean()
            day_rows.append([
                DAYS[d], len(g),
                (f"{wr2}%", f"color:{col_wr(wr2)};font-weight:bold"),
                (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
            ])

    out  = "<h2>Time of Day (UTC)</h2>"
    out += "<p class='note'>EU open ~07:00 UTC &nbsp;·&nbsp; US open ~13:30 UTC &nbsp;·&nbsp; Asia ~00:00–08:00 UTC</p>"
    out += tbl(["Hour", "Trades", "Win Rate", "Avg P&L %", "Avg Hold"], hour_rows)

    if day_rows:
        out += "<h2>Day of Week</h2>"
        out += tbl(["Day", "Trades", "Win Rate", "Avg P&L %"], day_rows)

    return out


def s_entry_signals(df: pd.DataFrame) -> str:
    out = "<h2>Entry Signal Analysis</h2>"

    # RSI buckets
    if "entry_rsi" in df.columns and df["entry_rsi"].notna().any():
        rsi_buckets = [(0, 25, "Extreme (<25)"),
                       (25, 27, "25–27"),
                       (27, 28, "27–28"),
                       (28, 29, "28–29"),
                       (29, 30, "29–30"),
                       (30, 32, "30–32"),
                       (32, 35, "32–35"),
                       (35, 100, "35+ (loose)")]
        rows = []
        for lo, hi, label in rsi_buckets:
            g = df[(df["entry_rsi"] >= lo) & (df["entry_rsi"] < hi)]
            if len(g) < 2:
                continue
            wr2 = wr(g["win"])
            avg_pnl = g["pnl_pct"].mean()
            rows.append([
                label, len(g),
                (f"{wr2}%", f"color:{col_wr(wr2)};font-weight:bold"),
                (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
                sharpe(g["pnl_pct"]),
            ])
        out += "<h3>RSI at Entry</h3>"
        out += tbl(["RSI range", "Trades", "Win Rate", "Avg P&L %", "Sharpe"], rows)

    # Score breakdown
    if "entry_score" in df.columns and df["entry_score"].notna().any():
        rows = []
        for s in sorted(df["entry_score"].dropna().unique()):
            g = df[df["entry_score"] == s]
            wr2 = wr(g["win"])
            avg_pnl = g["pnl_pct"].mean()
            rows.append([
                str(s), len(g),
                (f"{wr2}%", f"color:{col_wr(wr2)};font-weight:bold"),
                (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
            ])
        out += "<h3>Combined Entry Score</h3>"
        out += tbl(["Score", "Trades", "Win Rate", "Avg P&L %"], rows)

    # VWAP deviation
    if "entry_vwap_dev" in df.columns and df["entry_vwap_dev"].notna().any():
        vwap_buckets = [(-20, -0.5, "Below -0.5% (strong)"),
                        (-0.5, -0.3, "-0.5% to -0.3%"),
                        (-0.3, -0.1, "-0.3% to -0.1%"),
                        (-0.1, 0.0,  "-0.1% to 0% (barely below)"),
                        (0.0,  20,   "Above VWAP")]
        rows = []
        for lo, hi, label in vwap_buckets:
            g = df[(df["entry_vwap_dev"] >= lo) & (df["entry_vwap_dev"] < hi)]
            if len(g) < 2:
                continue
            wr2 = wr(g["win"])
            avg_pnl = g["pnl_pct"].mean()
            rows.append([
                label, len(g),
                (f"{wr2}%", f"color:{col_wr(wr2)};font-weight:bold"),
                (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
            ])
        out += "<h3>VWAP Deviation at Entry (%)</h3>"
        out += tbl(["VWAP dev", "Trades", "Win Rate", "Avg P&L %"], rows)

    # OB imbalance at entry
    if "entry_ob_imbalance" in df.columns and df["entry_ob_imbalance"].notna().any():
        ob_buckets = [(0.3, 1.0,  "Strong bid >+0.30"),
                      (0.2, 0.3,  "+0.20 to +0.30"),
                      (0.1, 0.2,  "+0.10 to +0.20"),
                      (-0.1, 0.1, "Neutral (±0.10)"),
                      (-1.0, -0.1,"Ask dominant (<-0.10)")]
        rows = []
        for lo, hi, label in ob_buckets:
            g = df[(df["entry_ob_imbalance"] >= lo) & (df["entry_ob_imbalance"] < hi)]
            if len(g) < 2:
                continue
            wr2 = wr(g["win"])
            avg_pnl = g["pnl_pct"].mean()
            rows.append([
                label, len(g),
                (f"{wr2}%", f"color:{col_wr(wr2)};font-weight:bold"),
                (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
            ])
        out += "<h3>Order Book Imbalance at Entry</h3>"
        out += tbl(["OB imbalance", "Trades", "Win Rate", "Avg P&L %"], rows)

    return out


def s_exit_signals(df: pd.DataFrame) -> str:
    out = "<h2>Exit Signal Analysis</h2>"
    out += "<p class='note'>What conditions looked like when each trade closed — helps tune TP/SL thresholds.</p>"

    # Exit RSI by reason
    if "exit_rsi" in df.columns and df["exit_rsi"].notna().any():
        rows = []
        for reason, g in df.groupby("reason"):
            ex = g["exit_rsi"].dropna()
            if len(ex) < 2:
                continue
            rows.append([
                reason, len(g),
                f"{ex.mean():.1f}",
                f"{ex.median():.1f}",
                f"{ex.min():.1f} – {ex.max():.1f}",
            ])
        out += "<h3>Exit RSI by Close Reason</h3>"
        out += tbl(["Reason", "Count", "Avg Exit RSI", "Median", "Range"], rows)

    # Exit VWAP dev by reason
    if "exit_vwap_dev" in df.columns and df["exit_vwap_dev"].notna().any():
        rows = []
        for reason, g in df.groupby("reason"):
            ev = g["exit_vwap_dev"].dropna()
            if len(ev) < 2:
                continue
            avg = ev.mean()
            rows.append([
                reason, len(g),
                (f"{avg:+.3f}%", f"color:{col_pnl(avg)}"),
            ])
        out += "<h3>Exit VWAP Deviation by Close Reason</h3>"
        out += "<p class='note'>Positive = price above VWAP at close. TP trades should typically be above.</p>"
        out += tbl(["Reason", "Count", "Avg Exit VWAP dev"], rows)

    # Exit OB imbalance: stop-loss vs take-profit
    if "exit_ob_imbalance" in df.columns and df["exit_ob_imbalance"].notna().any():
        rows = []
        for reason, g in df.groupby("reason"):
            eo = g["exit_ob_imbalance"].dropna()
            if len(eo) < 2:
                continue
            avg = eo.mean()
            rows.append([
                reason, len(g),
                (f"{avg:+.3f}", f"color:{col_pnl(avg)}"),
            ])
        out += "<h3>Order Book Imbalance at Close</h3>"
        out += "<p class='note'>Negative = selling pressure. Stop losses with strong negative OB indicate momentum exits, not noise.</p>"
        out += tbl(["Reason", "Count", "Avg OB imbalance"], rows)

    # RSI journey: entry → exit for wins vs losses
    if "entry_rsi" in df.columns and "exit_rsi" in df.columns:
        mask = df["entry_rsi"].notna() & df["exit_rsi"].notna()
        if mask.sum() >= 5:
            wins_df   = df[mask & df["win"]]
            losses_df = df[mask & ~df["win"]]
            rows = [
                ["Wins",   len(wins_df),
                 f"{wins_df['entry_rsi'].mean():.1f}" if len(wins_df) else "—",
                 f"{wins_df['exit_rsi'].mean():.1f}"  if len(wins_df) else "—",
                 (f"+{(wins_df['exit_rsi'] - wins_df['entry_rsi']).mean():.1f}",
                  "color:#00c851") if len(wins_df) else ("—", "")],
                ["Losses", len(losses_df),
                 f"{losses_df['entry_rsi'].mean():.1f}" if len(losses_df) else "—",
                 f"{losses_df['exit_rsi'].mean():.1f}"  if len(losses_df) else "—",
                 (f"{(losses_df['exit_rsi'] - losses_df['entry_rsi']).mean():+.1f}",
                  "color:#ff4444") if len(losses_df) else ("—", "")],
            ]
            out += "<h3>RSI Journey: Entry → Exit (Wins vs Losses)</h3>"
            out += tbl(["Outcome", "Count", "Avg Entry RSI", "Avg Exit RSI", "RSI change"], rows)

    return out


def s_grid(df: pd.DataFrame) -> str:
    if ("entry_rsi" not in df.columns or df["entry_rsi"].isna().all() or
            "entry_score" not in df.columns or df["entry_score"].isna().all()):
        return ("<h2>Parameter Grid Search</h2>"
                "<p class='note'>Need entry_rsi and entry_score fields — deploy latest bot version first.</p>")

    out = "<h2>Parameter Grid Search</h2>"
    out += ("<p class='note'>Filters real trade outcomes by entry threshold combinations. "
            "Shows what win rate would have been if only trades meeting each criterion were taken. "
            f"Cells with &lt;{MIN_TRADES_GRID} trades show —. "
            "Stricter filters find the best-performing subset of existing trades.</p>")

    headers = ["RSI ≤ ↓  /  Score ≥ →"] + [str(s) for s in SCORE_GRID]
    rows = []
    for rsi in RSI_BUY_GRID:
        row = [str(rsi)]
        for score in SCORE_GRID:
            g = df[(df["entry_rsi"] <= rsi) & (df["entry_score"] >= score)]
            n = len(g)
            if n < MIN_TRADES_GRID:
                row.append(("—", "color:#3d444d;text-align:center"))
            else:
                wr2 = wr(g["win"])
                c   = col_wr(wr2)
                row.append(
                    (f'<b style="color:{c}">{wr2}%</b>'
                     f'<span style="color:#8b949e;font-size:10px"> n={n}</span>', "")
                )
        rows.append(row)

    out += tbl(headers, rows)

    # Highlight best cell (min 10 trades)
    best_wr, best_n, best_rsi, best_score = 0.0, 0, 0, 0
    for rsi in RSI_BUY_GRID:
        for score in SCORE_GRID:
            g = df[(df["entry_rsi"] <= rsi) & (df["entry_score"] >= score)]
            if len(g) >= 10:
                wr2 = wr(g["win"])
                if wr2 > best_wr:
                    best_wr, best_n, best_rsi, best_score = wr2, len(g), rsi, score

    if best_rsi:
        out += (f'<p style="color:#00c851;font-weight:bold">Best combination (≥10 trades): '
                f'RSI ≤ {best_rsi} + Score ≥ {best_score} → '
                f'{best_wr}% win rate over {best_n} trades</p>')

    return out


def s_pairs(df: pd.DataFrame) -> str:
    rows = []
    for pair, g in df.groupby("pair"):
        wr2      = wr(g["win"])
        tot_pnl  = g["pnl_eur"].sum()
        avg_pnl  = g["pnl_pct"].mean()
        rows.append((wr2, [
            pair, len(g),
            (f"{wr2}%", f"color:{col_wr(wr2)};font-weight:bold"),
            (f"€{tot_pnl:+.4f}", f"color:{col_pnl(tot_pnl)}"),
            (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
            f"{g['held_min'].mean():.1f}m",
            sharpe(g["pnl_pct"]),
        ]))
    rows.sort(key=lambda x: x[0], reverse=True)

    return ("<h2>Pair Performance</h2>"
            + tbl(["Pair", "Trades", "Win Rate", "Total P&L", "Avg P&L %", "Avg Hold", "Sharpe"],
                  [r for _, r in rows]))


def s_param_regimes(df: pd.DataFrame) -> str:
    if "param_rsi_buy" not in df.columns or df["param_rsi_buy"].isna().all():
        return ("<h2>AI Parameter Regimes</h2>"
                "<p class='note'>param_rsi_buy not in trade records yet — deploy latest bot version.</p>")

    out = "<h2>AI Parameter Regimes</h2>"
    out += "<p class='note'>Win rates grouped by the RSI buy threshold that was active when each trade was taken. Shows which AI-tuned params actually worked.</p>"

    rows = []
    for rsi_buy in sorted(df["param_rsi_buy"].dropna().unique()):
        g = df[df["param_rsi_buy"] == rsi_buy]
        wr2     = wr(g["win"])
        tot_pnl = g["pnl_eur"].sum()
        score   = g["param_score_thresh"].mode()[0] if "param_score_thresh" in g.columns and g["param_score_thresh"].notna().any() else "—"
        rows.append([
            str(rsi_buy), str(score), len(g),
            (f"{wr2}%", f"color:{col_wr(wr2)};font-weight:bold"),
            (f"€{tot_pnl:+.4f}", f"color:{col_pnl(tot_pnl)}"),
            sharpe(g["pnl_pct"]),
        ])

    out += tbl(["RSI buy threshold", "Score thresh (mode)", "Trades", "Win Rate", "Total P&L", "Sharpe"], rows)

    # Bear market split
    if "btc_bear_at_entry" in df.columns and df["btc_bear_at_entry"].notna().any():
        bear  = df[df["btc_bear_at_entry"] == 1]
        bull  = df[df["btc_bear_at_entry"] == 0]
        rows2 = []
        if len(bull) >= 2:
            rows2.append(["BTC Bull / Neutral", len(bull),
                          (f"{wr(bull['win'])}%", f"color:{col_wr(wr(bull['win']))};font-weight:bold"),
                          (f"€{bull['pnl_eur'].sum():+.4f}", f"color:{col_pnl(bull['pnl_eur'].sum())}")])
        if len(bear) >= 2:
            rows2.append(["BTC Bear", len(bear),
                          (f"{wr(bear['win'])}%", f"color:{col_wr(wr(bear['win']))};font-weight:bold"),
                          (f"€{bear['pnl_eur'].sum():+.4f}", f"color:{col_pnl(bear['pnl_eur'].sum())}")])
        if rows2:
            out += "<h3>BTC Market Regime at Entry</h3>"
            out += tbl(["Market", "Trades", "Win Rate", "Total P&L"], rows2)

    return out


def s_concurrent(df: pd.DataFrame) -> str:
    if "concurrent_positions" not in df.columns or df["concurrent_positions"].isna().all():
        return ""

    rows = []
    for n_pos, g in df.groupby("concurrent_positions"):
        wr2 = wr(g["win"])
        rows.append([
            int(n_pos), len(g),
            (f"{wr2}%", f"color:{col_wr(wr2)};font-weight:bold"),
            (f"{g['pnl_pct'].mean():+.3f}%", f"color:{col_pnl(g['pnl_pct'].mean())}"),
        ])

    return ("<h2>Concurrent Positions at Entry</h2>"
            "<p class='note'>0 = only position open at the time. Does portfolio concentration affect performance?</p>"
            + tbl(["Other open positions", "Trades", "Win Rate", "Avg P&L %"], rows))


# ── Report assembly ───────────────────────────────────────────────────────────

def generate_report(df: pd.DataFrame, out_path: Path):
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body  = (s_overview(df) + s_time(df) + s_entry_signals(df)
             + s_exit_signals(df) + s_grid(df)
             + s_pairs(df) + s_param_regimes(df) + s_concurrent(df))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Scalper Backtest — {now}</title>
<style>{CSS}</style>
</head>
<body>
<h1>Scalper Trade Analysis
  <span style="font-size:14px;color:#8b949e;font-weight:normal;margin-left:16px">
    {len(df)} trades &nbsp;·&nbsp; {now}
  </span>
</h1>
{body}
</body>
</html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"\nReport saved: {out_path}")
    print(f"Open in browser: file:///{out_path.as_posix()}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scalper trade outcome analyser")
    parser.add_argument("--data",     default=None,
                        help="Path to scalper_trades.jsonl (default: backtest/data/)")
    parser.add_argument("--no-sync",  action="store_true",
                        help="Skip Docker sync, use cached data")
    parser.add_argument("--out",      default=None,
                        help="Output directory (default: backtest/reports/)")
    args = parser.parse_args()

    if args.data:
        data_path = Path(args.data)
    else:
        data_path = DATA_DIR / "scalper_trades.jsonl"
        if not args.no_sync:
            sync_from_docker()

    if not data_path.exists():
        sys.exit(f"Data file not found: {data_path}\n"
                 "Run without --no-sync to pull from Docker, or pass --data <path>")

    out_dir  = Path(args.out) if args.out else REPORT_DIR
    out_path = out_dir / f"scalper_{datetime.now().strftime('%Y-%m-%d_%H%M')}.html"

    print(f"Loading: {data_path}")
    df = load_trades(data_path)
    print(f"Loaded {len(df)} trades")

    generate_report(df, out_path)


if __name__ == "__main__":
    main()

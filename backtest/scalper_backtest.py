#!/usr/bin/env python3
"""
Scalper trade outcome analyser / backtester.

Pulls real trade data from the local Docker volume (which mirrors the server)
and produces a self-contained HTML report covering:
  - Overview stats, Kelly sizing, and exit reasons
  - Entry RSI / VWAP / score analysis with Wilson CIs
  - Exit signal analysis (RSI, VWAP, OB at close)
  - Grid search with Wilson CIs and Benjamini-Hochberg correction
  - Time-of-day and day-of-week breakdown
  - Per-pair performance with Bayesian credible intervals
  - AI param regime comparison
  - Concurrent position analysis

Usage:
    python backtest\\scalper_backtest.py              # sync from Docker + analyse
    python backtest\\scalper_backtest.py --no-sync    # use cached local data
    python backtest\\scalper_backtest.py --data FILE  # use a specific JSONL file

Output: backtest\\reports\\scalper_YYYY-MM-DD_HHMM.html
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
    from scipy import stats
except ImportError:
    sys.exit("Run:  pip install pandas numpy scipy")

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
MIN_TRADES_GRID = 5     # minimum trades in a grid cell to show a result
BH_ALPHA        = 0.05  # false discovery rate for Benjamini-Hochberg
RECS_FILENAME   = "backtest_recommendations.json"


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

    for col in ("open_ts", "ts"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")

    # Back-fill entry_hour / weekday from open_ts for older records
    if "open_ts" in df.columns:
        mask = df.get("entry_hour_utc", pd.Series(dtype=float)).isna()
        if mask.any() or "entry_hour_utc" not in df.columns:
            df["entry_hour_utc"] = df["open_ts"].dt.hour
        mask = df.get("entry_weekday", pd.Series(dtype=float)).isna()
        if mask.any() or "entry_weekday" not in df.columns:
            df["entry_weekday"] = df["open_ts"].dt.weekday
    elif "ts" in df.columns:
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


# ── Statistical helpers ───────────────────────────────────────────────────────

def wilson_ci(wins: pd.Series, confidence: float = 0.95) -> tuple:
    """
    Wilson score CI for a binary win rate.
    More accurate than normal approximation for small samples (n < 30).
    Returns (point_wr%, lo%, hi%).
    """
    n = len(wins)
    if n == 0:
        return (0.0, 0.0, 0.0)
    k      = int(wins.sum())
    p      = k / n
    z      = stats.norm.ppf(1 - (1 - confidence) / 2)   # 1.96 for 95%
    denom  = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    margin = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    lo = max(0.0, centre - margin)
    hi = min(1.0, centre + margin)
    return (round(p * 100, 1), round(lo * 100, 1), round(hi * 100, 1))


def bayesian_ci(wins: pd.Series, confidence: float = 0.95) -> tuple:
    """
    Bayesian Beta-Binomial credible interval for win rate.
    Prior: Beta(1,1) = uniform — no prior bias. Posterior: Beta(k+1, n-k+1).
    Returns (mean_wr%, cred_lo%, cred_hi%, prob_above_50%).
    prob_above_50 is the posterior probability that the true win rate exceeds 50%.
    """
    n      = len(wins)
    k      = int(wins.sum())
    alpha  = k + 1
    beta_p = n - k + 1
    tail   = (1 - confidence) / 2
    lo     = stats.beta.ppf(tail,     alpha, beta_p)
    hi     = stats.beta.ppf(1 - tail, alpha, beta_p)
    mean   = alpha / (alpha + beta_p)
    p_above = 1 - stats.beta.cdf(0.5, alpha, beta_p)
    return (
        round(mean * 100, 1),
        round(lo * 100, 1),
        round(hi * 100, 1),
        round(p_above * 100, 1),
    )


def kelly_fraction(win_rate_pct: float, avg_win_pct: float, avg_loss_pct: float) -> float:
    """
    Kelly criterion: optimal fraction of bankroll to risk per trade.
    f* = (p*b - q) / b  where b = avg_win / avg_loss (payoff ratio).
    Returns full-Kelly fraction (use half in practice to reduce variance).
    """
    if avg_loss_pct <= 0 or win_rate_pct <= 0:
        return 0.0
    p = win_rate_pct / 100
    q = 1 - p
    b = avg_win_pct / avg_loss_pct
    f = (p * b - q) / b
    return round(f, 4)


def bh_correct(p_values: list, alpha: float = BH_ALPHA) -> list:
    """
    Benjamini-Hochberg FDR correction for multiple comparisons.
    Controls the false discovery rate across all grid cells.
    Returns a bool list: True = survives BH correction at given alpha.
    """
    m = len(p_values)
    if m == 0:
        return []
    order    = sorted(range(m), key=lambda i: p_values[i])
    sig      = [False] * m
    last_sig = -1
    for rank, orig_idx in enumerate(order):
        if p_values[orig_idx] <= (rank + 1) / m * alpha:
            last_sig = rank
    for rank in range(last_sig + 1):
        sig[order[rank]] = True
    return sig


def _binomial_pval(wins: pd.Series) -> float:
    """One-sided binomial test p-value: P(WR > 50%) under H0."""
    n = len(wins)
    k = int(wins.sum())
    return stats.binomtest(k, n, 0.5, alternative="greater").pvalue


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


def ci_html(point_wr: float, lo: float, hi: float, n: int,
            prob_above_50: float = None, bh_sig: bool = None) -> tuple:
    """
    Return (html, td_style) for a win rate cell.
    Shows Wilson CI, optional Bayesian P(>50%), optional BH significance marker.

    Colour:
      green  — Wilson lower bound >= 50% (reliably profitable)
      yellow — point estimate >= 50% but CI crosses 50%
      red    — point estimate < 50%
    """
    if n < MIN_TRADES_GRID:
        return ("—", "color:#3d444d;text-align:center")

    if lo >= 50:
        c = "#00c851"
    elif point_wr >= 50:
        c = "#ffbb33"
    else:
        c = "#ff4444"

    ci_width = hi - lo
    ci_col   = "#3d444d" if ci_width > 35 else "#8b949e"

    sig_badge = ""
    if bh_sig is True:
        sig_badge = '<span style="color:#00c851;font-size:10px"> ★</span>'
    elif bh_sig is False and point_wr >= 50:
        sig_badge = '<span style="color:#3d444d;font-size:10px"> ✗</span>'

    prob_html = ""
    if prob_above_50 is not None:
        pc = "#00c851" if prob_above_50 >= 75 else ("#ffbb33" if prob_above_50 >= 55 else "#ff4444")
        prob_html = (f'<br><span style="color:{pc};font-size:10px">'
                     f'P(&gt;50%)={prob_above_50:.0f}%</span>')

    html = (f'<b style="color:{c}">{point_wr:.1f}%</b>'
            f'<span style="color:{ci_col};font-size:10px"> [{lo:.0f}–{hi:.0f}]</span>'
            f'<span style="color:#3d444d;font-size:10px"> n={n}</span>'
            f'{sig_badge}{prob_html}')
    return (html, "")


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
.sublabel { font-size:10px; color:#3d444d; margin-top:2px; }
.note { color:#8b949e; font-size:12px; margin-bottom:12px; }
.section { margin-bottom:8px; }
"""

def card(label: str, value: str, colour: str = "#e6edf3", sublabel: str = "") -> str:
    sub = f'<div class="sublabel">{sublabel}</div>' if sublabel else ""
    return (f'<div class="card"><div class="label">{label}</div>'
            f'<div class="stat" style="color:{colour}">{value}</div>{sub}</div>')

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
    wins_n    = int(df["win"].sum())
    total_pnl = df["pnl_eur"].sum()
    avg_held  = df["held_min"].mean()

    date_range = "—"
    if "open_ts" in df.columns and df["open_ts"].notna().any():
        first      = df["open_ts"].min().strftime("%Y-%m-%d")
        last       = df["open_ts"].max().strftime("%Y-%m-%d")
        date_range = f"{first} → {last}"

    # Wilson CI for overall win rate
    pt_wr, w_lo, w_hi = wilson_ci(df["win"])
    wr_label = f"{pt_wr:.1f}% [{w_lo:.0f}–{w_hi:.0f}]"

    # Bayesian overall
    _, b_lo, b_hi, p_above = bayesian_ci(df["win"])
    bayes_col = "#00c851" if p_above >= 75 else ("#ffbb33" if p_above >= 50 else "#ff4444")

    # Kelly sizing
    wins_df   = df[df["win"]]
    losses_df = df[~df["win"]]
    avg_win   = wins_df["pnl_pct"].mean()           if len(wins_df)   > 0 else 0.0
    avg_loss  = abs(losses_df["pnl_pct"].mean())    if len(losses_df) > 0 else 0.0
    kf        = kelly_fraction(pt_wr, avg_win, avg_loss)
    half_kf   = kf / 2
    kf_col    = "#00c851" if kf > 0 else "#ff4444"
    kf_label  = f"{kf*100:.1f}%" if kf > 0 else "Negative"

    ev = df["pnl_pct"].mean()

    cards = (
        card("Trades", str(n))
        + card("Win rate [95% Wilson]", wr_label, col_wr(pt_wr))
        + card("P(true WR > 50%)", f"{p_above:.0f}%", bayes_col, sublabel="Bayesian posterior")
        + card("W / L", f"{wins_n} / {n - wins_n}")
        + card("Total P&L", f"€{total_pnl:+.2f}", col_pnl(total_pnl))
        + card("Avg hold", f"{avg_held:.1f}m")
        + card("Avg P&L / trade", f"{ev:+.3f}%", col_pnl(ev))
        + card("Data range", date_range)
    )

    kelly_html = ""
    if avg_loss > 0:
        kelly_html = (
            f'<div class="card" style="min-width:300px">'
            f'<div class="label">Kelly Criterion</div>'
            f'<div class="stat" style="color:{kf_col}">{kf_label}</div>'
            f'<div class="sublabel">Full Kelly (theoretical max). '
            f'Half-Kelly = <b>{half_kf*100:.1f}%</b> (recommended).<br>'
            f'Payoff ratio: avg win {avg_win:.3f}% / avg loss {avg_loss:.3f}%'
            f' = {avg_win/avg_loss:.2f}x</div>'
            f'</div>'
        )

    reason_rows = []
    for reason, g in df.groupby("reason"):
        pt, lo, hi       = wilson_ci(g["win"])
        _, _, _, p_ab    = bayesian_ci(g["win"])
        avg_pnl          = g["pnl_pct"].mean()
        reason_rows.append([
            reason, len(g),
            ci_html(pt, lo, hi, len(g), prob_above_50=p_ab),
            (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
            f"{g['held_min'].mean():.1f}m",
            sharpe(g["pnl_pct"]),
        ])

    return (
        f'<h2>Overview</h2><div class="cards">{cards}{kelly_html}</div>'
        f"<h3>Exit Reasons</h3>"
        + tbl(["Reason", "Count", "Win Rate [95% CI] / P(>50%)", "Avg P&L %", "Avg Hold", "Sharpe"],
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
        avg_pnl          = g["pnl_pct"].mean()
        pt, lo, hi       = wilson_ci(g["win"])
        _, _, _, p_above = bayesian_ci(g["win"])
        bar_width = min(int(pt * 0.8), 80)
        bar = (f'<div style="display:inline-block;width:{bar_width}px;height:8px;'
               f'background:{col_wr(pt)};border-radius:2px;vertical-align:middle;'
               f'margin-right:6px"></div>')
        html, style = ci_html(pt, lo, hi, len(g), prob_above_50=p_above)
        hour_rows.append([
            f"{h:02d}:00 UTC", len(g),
            (f"{bar}{html}", style),
            (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
            f"{g['held_min'].mean():.1f}m",
        ])

    day_rows = []
    if "entry_weekday" in df.columns and df["entry_weekday"].notna().any():
        for d in range(7):
            g = df[df["entry_weekday"] == d]
            if len(g) < 2:
                continue
            pt, lo, hi       = wilson_ci(g["win"])
            _, _, _, p_above = bayesian_ci(g["win"])
            avg_pnl          = g["pnl_pct"].mean()
            day_rows.append([
                DAYS[d], len(g),
                ci_html(pt, lo, hi, len(g), prob_above_50=p_above),
                (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
            ])

    out  = "<h2>Time of Day (UTC)</h2>"
    out += "<p class='note'>EU open ~07:00 UTC &nbsp;·&nbsp; US open ~13:30 UTC &nbsp;·&nbsp; Asia ~00:00–08:00 UTC</p>"
    out += tbl(["Hour", "Trades", "Win Rate [95% CI] / P(>50%)", "Avg P&L %", "Avg Hold"], hour_rows)

    if day_rows:
        out += "<h2>Day of Week</h2>"
        out += tbl(["Day", "Trades", "Win Rate [95% CI] / P(>50%)", "Avg P&L %"], day_rows)

    return out


def s_entry_signals(df: pd.DataFrame) -> str:
    out = "<h2>Entry Signal Analysis</h2>"

    if "entry_rsi" in df.columns and df["entry_rsi"].notna().any():
        rsi_buckets = [(0, 25,   "Extreme (<25)"),
                       (25, 27,  "25–27"),
                       (27, 28,  "27–28"),
                       (28, 29,  "28–29"),
                       (29, 30,  "29–30"),
                       (30, 32,  "30–32"),
                       (32, 35,  "32–35"),
                       (35, 100, "35+ (loose)")]
        rows = []
        for lo_r, hi_r, label in rsi_buckets:
            g = df[(df["entry_rsi"] >= lo_r) & (df["entry_rsi"] < hi_r)]
            if len(g) < 2:
                continue
            pt, lo, hi       = wilson_ci(g["win"])
            _, _, _, p_above = bayesian_ci(g["win"])
            avg_pnl          = g["pnl_pct"].mean()
            rows.append([
                label, len(g),
                ci_html(pt, lo, hi, len(g), prob_above_50=p_above),
                (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
                sharpe(g["pnl_pct"]),
            ])
        out += "<h3>RSI at Entry</h3>"
        out += tbl(["RSI range", "Trades", "Win Rate [95% CI] / P(>50%)", "Avg P&L %", "Sharpe"], rows)

    if "entry_score" in df.columns and df["entry_score"].notna().any():
        rows = []
        for s in sorted(df["entry_score"].dropna().unique()):
            g = df[df["entry_score"] == s]
            pt, lo, hi       = wilson_ci(g["win"])
            _, _, _, p_above = bayesian_ci(g["win"])
            avg_pnl          = g["pnl_pct"].mean()
            rows.append([
                str(s), len(g),
                ci_html(pt, lo, hi, len(g), prob_above_50=p_above),
                (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
            ])
        out += "<h3>Combined Entry Score</h3>"
        out += tbl(["Score", "Trades", "Win Rate [95% CI] / P(>50%)", "Avg P&L %"], rows)

    if "entry_vwap_dev" in df.columns and df["entry_vwap_dev"].notna().any():
        vwap_buckets = [(-20, -0.5, "Below -0.5% (strong)"),
                        (-0.5, -0.3, "-0.5% to -0.3%"),
                        (-0.3, -0.1, "-0.3% to -0.1%"),
                        (-0.1,  0.0, "-0.1% to 0% (barely below)"),
                        (0.0,   20,  "Above VWAP")]
        rows = []
        for lo_v, hi_v, label in vwap_buckets:
            g = df[(df["entry_vwap_dev"] >= lo_v) & (df["entry_vwap_dev"] < hi_v)]
            if len(g) < 2:
                continue
            pt, lo, hi       = wilson_ci(g["win"])
            _, _, _, p_above = bayesian_ci(g["win"])
            avg_pnl          = g["pnl_pct"].mean()
            rows.append([
                label, len(g),
                ci_html(pt, lo, hi, len(g), prob_above_50=p_above),
                (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
            ])
        out += "<h3>VWAP Deviation at Entry (%)</h3>"
        out += tbl(["VWAP dev", "Trades", "Win Rate [95% CI] / P(>50%)", "Avg P&L %"], rows)

    if "entry_ob_imbalance" in df.columns and df["entry_ob_imbalance"].notna().any():
        ob_buckets = [(0.3,  1.0,  "Strong bid >+0.30"),
                      (0.2,  0.3,  "+0.20 to +0.30"),
                      (0.1,  0.2,  "+0.10 to +0.20"),
                      (-0.1, 0.1,  "Neutral (±0.10)"),
                      (-1.0, -0.1, "Ask dominant (<-0.10)")]
        rows = []
        for lo_o, hi_o, label in ob_buckets:
            g = df[(df["entry_ob_imbalance"] >= lo_o) & (df["entry_ob_imbalance"] < hi_o)]
            if len(g) < 2:
                continue
            pt, lo, hi       = wilson_ci(g["win"])
            _, _, _, p_above = bayesian_ci(g["win"])
            avg_pnl          = g["pnl_pct"].mean()
            rows.append([
                label, len(g),
                ci_html(pt, lo, hi, len(g), prob_above_50=p_above),
                (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
            ])
        out += "<h3>Order Book Imbalance at Entry</h3>"
        out += tbl(["OB imbalance", "Trades", "Win Rate [95% CI] / P(>50%)", "Avg P&L %"], rows)

    return out


def s_exit_signals(df: pd.DataFrame) -> str:
    out = "<h2>Exit Signal Analysis</h2>"
    out += "<p class='note'>What conditions looked like when each trade closed — helps tune TP/SL thresholds.</p>"

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

    if "exit_vwap_dev" in df.columns and df["exit_vwap_dev"].notna().any():
        rows = []
        for reason, g in df.groupby("reason"):
            ev = g["exit_vwap_dev"].dropna()
            if len(ev) < 2:
                continue
            avg = ev.mean()
            rows.append([reason, len(g), (f"{avg:+.3f}%", f"color:{col_pnl(avg)}")])
        out += "<h3>Exit VWAP Deviation by Close Reason</h3>"
        out += "<p class='note'>Positive = price above VWAP at close. TP trades should typically be above.</p>"
        out += tbl(["Reason", "Count", "Avg Exit VWAP dev"], rows)

    if "exit_ob_imbalance" in df.columns and df["exit_ob_imbalance"].notna().any():
        rows = []
        for reason, g in df.groupby("reason"):
            eo = g["exit_ob_imbalance"].dropna()
            if len(eo) < 2:
                continue
            avg = eo.mean()
            rows.append([reason, len(g), (f"{avg:+.3f}", f"color:{col_pnl(avg)}")])
        out += "<h3>Order Book Imbalance at Close</h3>"
        out += "<p class='note'>Negative = selling pressure. Stop losses with strong negative OB indicate momentum exits, not noise.</p>"
        out += tbl(["Reason", "Count", "Avg OB imbalance"], rows)

    if "entry_rsi" in df.columns and "exit_rsi" in df.columns:
        mask = df["entry_rsi"].notna() & df["exit_rsi"].notna()
        if mask.sum() >= 5:
            wins_df   = df[mask & df["win"]]
            losses_df = df[mask & ~df["win"]]
            rows = [
                ["Wins",   len(wins_df),
                 f"{wins_df['entry_rsi'].mean():.1f}"  if len(wins_df)   else "—",
                 f"{wins_df['exit_rsi'].mean():.1f}"   if len(wins_df)   else "—",
                 (f"+{(wins_df['exit_rsi'] - wins_df['entry_rsi']).mean():.1f}",
                  "color:#00c851") if len(wins_df)   else ("—", "")],
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
    out += (f'<p class="note">Filters real trade outcomes by entry threshold combinations. '
            f'Wilson 95% CI · Bayesian P(&gt;50%) · '
            f'<b>★</b> = survives Benjamini-Hochberg FDR correction '
            f'(controls false discoveries across all tested combinations at {BH_ALPHA*100:.0f}%). '
            f'Cells with &lt;{MIN_TRADES_GRID} trades show —.</p>')

    print("  Computing Wilson CIs + BH correction for grid search...")

    # First pass: collect stats for all cells
    cells = {}
    for rsi in RSI_BUY_GRID:
        for score in SCORE_GRID:
            g = df[(df["entry_rsi"] <= rsi) & (df["entry_score"] >= score)]
            n = len(g)
            if n < MIN_TRADES_GRID:
                cells[(rsi, score)] = None
                continue
            pt, lo, hi       = wilson_ci(g["win"])
            _, _, _, p_above = bayesian_ci(g["win"])
            p_val            = _binomial_pval(g["win"])
            cells[(rsi, score)] = (pt, lo, hi, n, p_above, p_val)

    # BH correction across all eligible cells
    eligible  = [k for k, v in cells.items() if v is not None]
    p_vals    = [cells[k][5] for k in eligible]
    bh_flags  = bh_correct(p_vals) if p_vals else []
    bh_lookup = {k: bh_flags[i] for i, k in enumerate(eligible)}

    # Render table
    headers = ["RSI ≤ ↓  /  Score ≥ →"] + [str(s) for s in SCORE_GRID]
    rows    = []
    best_lo, best_pt, best_n, best_rsi, best_score = 0.0, 0.0, 0, 0, 0
    for rsi in RSI_BUY_GRID:
        row = [str(rsi)]
        for score in SCORE_GRID:
            cell = cells.get((rsi, score))
            if cell is None:
                row.append(("—", "color:#3d444d;text-align:center"))
            else:
                pt, lo, hi, n, p_above, _ = cell
                bh_sig = bh_lookup.get((rsi, score), False)
                row.append(ci_html(pt, lo, hi, n, prob_above_50=p_above, bh_sig=bh_sig))
                if lo > best_lo and n >= 10:
                    best_lo, best_pt, best_n = lo, pt, n
                    best_rsi, best_score     = rsi, score
        rows.append(row)

    out += tbl(headers, rows)
    out += ("<p class='note'>"
            "<b>Reading:</b> <b>47.8%</b> [43–53] n=45 → Wilson point estimate 47.8%, "
            "95% CI 43%–53%, 45 trades. "
            "<span style='color:#00c851'>Green</span> = CI lower bound ≥ 50%. "
            "<span style='color:#ffbb33'>Amber</span> = looks profitable but CI crosses 50%. "
            "<b>★</b> = significant after BH FDR correction. "
            "<b>✗</b> = looks profitable but does not survive BH (likely noise).</p>")

    if best_rsi:
        out += (f'<p style="color:#00c851;font-weight:bold">'
                f'Most reliable (highest Wilson lower bound, ≥10 trades): '
                f'RSI ≤ {best_rsi} + Score ≥ {best_score} → '
                f'{best_pt:.1f}% win rate, CI lower bound {best_lo:.1f}%, n={best_n}'
                f'</p>')

    return out


def s_pairs(df: pd.DataFrame) -> str:
    rows = []
    for pair, g in df.groupby("pair"):
        tot_pnl          = g["pnl_eur"].sum()
        avg_pnl          = g["pnl_pct"].mean()
        pt, lo, hi       = wilson_ci(g["win"])
        _, _, _, p_above = bayesian_ci(g["win"])
        rows.append((p_above, [
            pair, len(g),
            ci_html(pt, lo, hi, len(g), prob_above_50=p_above),
            (f"€{tot_pnl:+.4f}", f"color:{col_pnl(tot_pnl)}"),
            (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
            f"{g['held_min'].mean():.1f}m",
            sharpe(g["pnl_pct"]),
        ]))
    rows.sort(key=lambda x: x[0], reverse=True)

    return ("<h2>Pair Performance</h2>"
            "<p class='note'>Sorted by Bayesian P(true WR &gt; 50%) — most likely genuinely profitable pairs first.</p>"
            + tbl(["Pair", "Trades", "Win Rate [95% CI] / P(>50%)", "Total P&L", "Avg P&L %", "Avg Hold", "Sharpe"],
                  [r for _, r in rows]))


def s_param_regimes(df: pd.DataFrame) -> str:
    if "param_rsi_buy" not in df.columns or df["param_rsi_buy"].isna().all():
        return ("<h2>AI Parameter Regimes</h2>"
                "<p class='note'>param_rsi_buy not in trade records yet — deploy latest bot version.</p>")

    out = "<h2>AI Parameter Regimes</h2>"
    out += "<p class='note'>Win rates grouped by the RSI buy threshold active when each trade was taken.</p>"

    rows = []
    for rsi_buy in sorted(df["param_rsi_buy"].dropna().unique()):
        g              = df[df["param_rsi_buy"] == rsi_buy]
        pt, lo, hi     = wilson_ci(g["win"])
        _, _, _, p_ab  = bayesian_ci(g["win"])
        tot_pnl        = g["pnl_eur"].sum()
        score          = (g["param_score_thresh"].mode()[0]
                          if "param_score_thresh" in g.columns and g["param_score_thresh"].notna().any()
                          else "—")
        rows.append([
            str(rsi_buy), str(score), len(g),
            ci_html(pt, lo, hi, len(g), prob_above_50=p_ab),
            (f"€{tot_pnl:+.4f}", f"color:{col_pnl(tot_pnl)}"),
            sharpe(g["pnl_pct"]),
        ])

    out += tbl(["RSI buy threshold", "Score thresh (mode)", "Trades",
                "Win Rate [95% CI] / P(>50%)", "Total P&L", "Sharpe"], rows)

    if "btc_bear_at_entry" in df.columns and df["btc_bear_at_entry"].notna().any():
        bear  = df[df["btc_bear_at_entry"] == 1]
        bull  = df[df["btc_bear_at_entry"] == 0]
        rows2 = []
        for label, g in [("BTC Bull / Neutral", bull), ("BTC Bear", bear)]:
            if len(g) < 2:
                continue
            pt, lo, hi     = wilson_ci(g["win"])
            _, _, _, p_ab  = bayesian_ci(g["win"])
            rows2.append([
                label, len(g),
                ci_html(pt, lo, hi, len(g), prob_above_50=p_ab),
                (f"€{g['pnl_eur'].sum():+.4f}", f"color:{col_pnl(g['pnl_eur'].sum())}"),
            ])
        if rows2:
            out += "<h3>BTC Market Regime at Entry</h3>"
            out += tbl(["Market", "Trades", "Win Rate [95% CI] / P(>50%)", "Total P&L"], rows2)

    return out


def s_concurrent(df: pd.DataFrame) -> str:
    if "concurrent_positions" not in df.columns or df["concurrent_positions"].isna().all():
        return ""

    rows = []
    for n_pos, g in df.groupby("concurrent_positions"):
        pt, lo, hi     = wilson_ci(g["win"])
        _, _, _, p_ab  = bayesian_ci(g["win"])
        avg_pnl        = g["pnl_pct"].mean()
        rows.append([
            int(n_pos), len(g),
            ci_html(pt, lo, hi, len(g), prob_above_50=p_ab),
            (f"{avg_pnl:+.3f}%", f"color:{col_pnl(avg_pnl)}"),
        ])

    return ("<h2>Concurrent Positions at Entry</h2>"
            "<p class='note'>0 = only position open at the time. Does portfolio concentration affect performance?</p>"
            + tbl(["Other open positions", "Trades", "Win Rate [95% CI] / P(>50%)", "Avg P&L %"], rows))


# ── AI feedback ──────────────────────────────────────────────────────────────

def compute_recommendations(df: pd.DataFrame) -> dict:
    """Extract top validated parameter combinations for AI feedback loop."""
    if ("entry_rsi" not in df.columns or df["entry_rsi"].isna().all() or
            "entry_score" not in df.columns or df["entry_score"].isna().all()):
        return {}

    combos = []
    for rsi in RSI_BUY_GRID:
        for score in SCORE_GRID:
            g = df[(df["entry_rsi"] <= rsi) & (df["entry_score"] >= score)]
            n = len(g)
            if n < MIN_TRADES_GRID:
                continue
            pt, lo, hi       = wilson_ci(g["win"])
            _, _, _, p_above = bayesian_ci(g["win"])
            p_val            = _binomial_pval(g["win"])
            combos.append({
                "rsi_buy_max":   rsi,
                "score_min":     score,
                "win_rate":      pt,
                "wilson_lo":     lo,
                "wilson_hi":     hi,
                "prob_above_50": p_above,
                "n_trades":      n,
                "p_value":       round(p_val, 6),
            })

    if not combos:
        return {}

    p_vals   = [c["p_value"] for c in combos]
    bh_flags = bh_correct(p_vals)
    for c, sig in zip(combos, bh_flags):
        c["bh_significant"] = sig

    # BH-significant first, then by Wilson lower bound
    combos.sort(key=lambda c: (c["bh_significant"], c["wilson_lo"]), reverse=True)

    return {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "trade_count":      len(df),
        "best":             combos[0],
        "top_combinations": combos[:5],
    }


def push_recommendations_to_docker(recs: dict):
    """Write backtest_recommendations.json into the Docker data volume."""
    content = json.dumps(recs, indent=2)
    result  = subprocess.run(
        ["docker", "run", "--rm", "-i",
         "-v", f"{DOCKER_VOLUME}:/data",
         "alpine", "sh", "-c", f"cat > /data/{RECS_FILENAME}"],
        input=content, text=True, capture_output=True,
    )
    if result.returncode == 0:
        print(f"  Pushed recommendations to Docker volume: /data/{RECS_FILENAME}")
    else:
        print(f"  Warning: could not push to Docker: {result.stderr[:120]}")


# ── Report assembly ───────────────────────────────────────────────────────────

def generate_report(df: pd.DataFrame, out_path: Path):
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = (s_overview(df) + s_time(df) + s_entry_signals(df)
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

    # Write AI feedback file locally and push to Docker volume
    recs = compute_recommendations(df)
    if recs:
        recs_path = DATA_DIR / RECS_FILENAME
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        recs_path.write_text(json.dumps(recs, indent=2), encoding="utf-8")
        best = recs["best"]
        sig = "BH-sig" if best['bh_significant'] else "not-BH-sig"
        print(f"\nBest validated combo: RSI<={best['rsi_buy_max']}, Score>={best['score_min']} "
              f"-> {best['win_rate']}% WR [{best['wilson_lo']}%-{best['wilson_hi']}%], "
              f"P(>50%)={best['prob_above_50']}%, n={best['n_trades']}, {sig}")
        push_recommendations_to_docker(recs)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scalper trade outcome analyser")
    parser.add_argument("--data",    default=None,
                        help="Path to scalper_trades.jsonl (default: backtest/data/)")
    parser.add_argument("--no-sync", action="store_true",
                        help="Skip Docker sync, use cached data")
    parser.add_argument("--out",     default=None,
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
    print(f"Loaded {len(df)} trades  |  stats: Wilson CI + Bayesian Beta + BH correction")

    generate_report(df, out_path)


if __name__ == "__main__":
    main()

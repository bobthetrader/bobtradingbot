"""Daily long/short journal analysis — replaces the retired scalper backtest.

Reads the main bot's trade journal (BUY rows carry entry feature-vectors since
2026-07-04; SELL rows carry NET pnl_eur), pairs entries to exits, and renders
bucketed net-win-rate tables: smart-money action, HL bias, whale score, 1h RSI,
entry hour, pair, strategy, exit reason. Console summary + standalone HTML.

REPORT ONLY: never commits, pushes, or feeds any AI loop.
Run: py -3 backtest/journal_analysis.py [--data PATH] [--html PATH]
"""
from __future__ import annotations
import argparse
import html as _html
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA = os.path.join(BASE, "backtest", "data", "trade_events_paper.jsonl")
DEFAULT_HTML = os.path.join(BASE, "backtest", "journal_report.html")

MIN_SHOW = 5      # hide buckets below this n
MIN_CONF = 20     # mark buckets below this n as low-confidence


def load_trades(path: str):
    """Return closed trades: longs (SELL rows, features joined from the
    preceding BUY on the same pair FIFO) and shorts (SHORT_CLOSE rows,
    short_type/features joined from SHORT_OPEN)."""
    if not os.path.exists(path):
        return []
    open_buys = defaultdict(list)     # pair -> [features dicts] FIFO
    open_shorts = {}                  # pair -> {short_type, features}
    out, bad = [], 0
    # utf-8-sig: the PS 5.1 pull script writes a BOM, which would otherwise
    # make json.loads reject the first journal record
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                bad += 1
                continue
            t = e.get("type")
            pair = e.get("pair")
            extra = e.get("extra") or {}
            if t == "BUY":
                open_buys[pair].append(extra.get("features") or {})
            elif t == "SELL":
                feats = open_buys[pair].pop(0) if open_buys[pair] else {}
                out.append({
                    "side": "long", "pair": pair, "ts": e.get("ts", ""),
                    "pnl_eur": float(e.get("pnl_eur") or 0),
                    "pnl_pct": extra.get("pnl_pct"),
                    "reason": e.get("reason", ""),
                    "hold_minutes": extra.get("hold_minutes"),
                    "features": feats,
                })
            elif t == "SHORT_OPEN":
                open_shorts[pair] = {
                    "short_type": extra.get("short_type"),
                    "features": extra.get("features") or {},
                }
            elif t == "SHORT_CLOSE":
                meta = open_shorts.pop(pair, {})
                out.append({
                    "side": "short", "pair": pair, "ts": e.get("ts", ""),
                    "pnl_eur": float(e.get("pnl_eur") or 0),
                    "pnl_pct": extra.get("pnl_pct"),
                    "reason": e.get("reason", ""),
                    "short_type": meta.get("short_type"),
                    "features": meta.get("features") or {},
                })
    if bad:
        print(f"note: skipped {bad} malformed journal line(s)")
    return out


def bucket_stats(trades, key_fn):
    """bucket label -> {n, wins, net_eur, wr_pct, avg_pct}."""
    agg = defaultdict(lambda: {"n": 0, "wins": 0, "net_eur": 0.0, "_pcts": []})
    for t in trades:
        try:
            k = str(key_fn(t))
        except Exception:
            k = "unknown"
        a = agg[k]
        a["n"] += 1
        a["wins"] += 1 if t["pnl_eur"] > 0 else 0
        a["net_eur"] += t["pnl_eur"]
        if t.get("pnl_pct") is not None:
            a["_pcts"].append(float(t["pnl_pct"]))
    out = {}
    for k, a in agg.items():
        out[k] = {
            "n": a["n"], "wins": a["wins"],
            "net_eur": round(a["net_eur"], 2),
            "wr_pct": round(100.0 * a["wins"] / a["n"], 1) if a["n"] else 0.0,
            "avg_pct": round(sum(a["_pcts"]) / len(a["_pcts"]), 2) if a["_pcts"] else None,
        }
    return out


def _band(v, edges, labels):
    if v is None:
        return "no-data"
    for e, lab in zip(edges, labels):
        if v <= e:
            return lab
    return labels[-1]


def _f(t, key):
    return (t.get("features") or {}).get(key)


def _iso_week(t):
    try:
        d = datetime.fromisoformat((t.get("ts") or "")[:19])
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    except Exception:
        return "unknown"


BUCKETS = [
    ("ISO week", _iso_week),
    ("Smart action", lambda t: _f(t, "smart_action") or "none-recorded"),
    ("HL bias", lambda t: _band(_f(t, "hl_bias"), [-2.5, -1.0, 1.0, 2.5],
                                ["<=-2.5", "-2.5..-1", "-1..+1", "+1..+2.5", ">=+2.5"])),
    ("Whale score", lambda t: _band(_f(t, "whale_score"), [-2.5, -1.0, 1.0, 2.5],
                                    ["<=-2.5", "-2.5..-1", "-1..+1", "+1..+2.5", ">=+2.5"])),
    ("1h RSI at entry", lambda t: _band(_f(t, "rsi_1h"), [40, 50, 60],
                                        ["<40", "40-50", "50-60", ">=60"])),
    ("Entry hour (UTC)", lambda t: _band(_f(t, "hour_utc"), [3, 7, 11, 15, 19],
                                         ["00-03", "04-07", "08-11", "12-15", "16-19", "20-23"])),
    ("Pair", lambda t: t.get("pair") or "?"),
    ("Strategy", lambda t: _f(t, "strategy") or "none-recorded"),
    ("Exit reason", lambda t: t.get("reason") or "?"),
]


def _fmt_table(title, stats):
    lines = [f"\n== {title} =="]
    shown = 0
    for k in sorted(stats, key=lambda k: -stats[k]["n"]):
        s = stats[k]
        if s["n"] < MIN_SHOW:
            continue
        shown += 1
        conf = "" if s["n"] >= MIN_CONF else "  (low confidence)"
        avg = f"  avg {s['avg_pct']:+.2f}%" if s["avg_pct"] is not None else ""
        lines.append(f"  {k:<16} n={s['n']:<4} WR {s['wr_pct']:5.1f}%  "
                     f"net {s['net_eur']:+8.2f} EUR{avg}{conf}")
    if not shown:
        lines.append("  (all buckets below n=5)")
    return "\n".join(lines)


def _html_table(title, stats):
    rows = ""
    for k in sorted(stats, key=lambda k: -stats[k]["n"]):
        s = stats[k]
        if s["n"] < MIN_SHOW:
            continue
        colour = "#00c851" if s["net_eur"] >= 0 else "#ff4444"
        conf = "" if s["n"] >= MIN_CONF else " &#9888;"
        rows += (f"<tr><td>{_html.escape(k)}{conf}</td><td>{s['n']}</td>"
                 f"<td>{s['wr_pct']:.1f}%</td>"
                 f"<td style='color:{colour}'>{s['net_eur']:+.2f}</td></tr>")
    if not rows:
        rows = "<tr><td colspan='4'>all buckets below n=5</td></tr>"
    return (f"<h2>{_html.escape(title)}</h2><table border='0' cellpadding='6' "
            f"style='border-collapse:collapse;font-family:monospace'>"
            f"<tr><th align='left'>bucket</th><th>n</th><th>WR</th><th>net EUR</th></tr>"
            f"{rows}</table>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--html", default=DEFAULT_HTML)
    args = ap.parse_args()

    trades = load_trades(args.data)
    if not trades:
        print(f"no journal data at {args.data} — nothing to analyse")
        return 0

    longs = [t for t in trades if t["side"] == "long"]
    shorts = [t for t in trades if t["side"] == "short"]
    event_shorts = [t for t in shorts if t.get("short_type") == "EVENT"]

    def _summary(name, ts):
        if not ts:
            return f"{name}: none"
        wins = sum(1 for t in ts if t["pnl_eur"] > 0)
        net = sum(t["pnl_eur"] for t in ts)
        return (f"{name}: {len(ts)} closed | WR {100.0*wins/len(ts):.1f}% | "
                f"net {net:+.2f} EUR")

    print("=" * 62)
    print("JOURNAL ANALYSIS -", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    print(_summary("LONGS", longs))
    print(_summary("SHORTS", shorts))
    print(_summary("EVENT SHORTS", event_shorts))
    print("=" * 62)

    html_parts = [f"<h1>Journal analysis - "
                  f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC</h1>"
                  f"<p>{_summary('LONGS', longs)}<br>{_summary('SHORTS', shorts)}<br>"
                  f"{_summary('EVENT SHORTS', event_shorts)}</p>"
                  f"<p>&#9888; = n&lt;{MIN_CONF} (low confidence); buckets n&lt;{MIN_SHOW} hidden</p>"]

    for title, key_fn in BUCKETS:
        stats = bucket_stats(longs, key_fn)
        print(_fmt_table(f"LONGS by {title}", stats))
        html_parts.append(_html_table(f"Longs by {title}", stats))

    if shorts:
        for title, key_fn in (("Short type", lambda t: t.get("short_type") or "legacy"),
                              ("Exit reason", lambda t: t.get("reason") or "?"),
                              ("Pair", lambda t: t.get("pair") or "?")):
            stats = bucket_stats(shorts, key_fn)
            print(_fmt_table(f"SHORTS by {title}", stats))
            html_parts.append(_html_table(f"Shorts by {title}", stats))

    try:
        with open(args.html, "w", encoding="utf-8") as f:
            f.write("<meta charset='utf-8'><body style='background:#0e1117;"
                    "color:#e6edf3;font-family:monospace'>" + "".join(html_parts))
        print(f"\nHTML report: {args.html}")
    except Exception as exc:
        print(f"html write failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Journal Analysis Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the daily scalper backtest with a long/short journal analysis (smart-money, RSI, hour-of-day buckets) that feeds the ~2026-07-20 review.

**Architecture:** New pure-stdlib `backtest/journal_analysis.py` pairs the journal's SELL/SHORT_CLOSE outcomes with entry features and renders bucketed net-WR tables (console + standalone HTML). `scripts/daily_backtest.ps1` pulls `trade_events_paper.jsonl` via a new restricted-SSH `journal` command and runs the analyzer; the scalper pull, backtest, and recommendations push-back are removed. Server gatekeeper/cron get a one-time manual update (exact commands provided).

**Tech Stack:** Python 3 stdlib only (no pandas). PowerShell 5.1 for the scheduled task script (ASCII-only — em-dashes broke PS 5.1 ParseFile before).

## Global Constraints

- Report only: analyzer never commits, pushes, or writes outside `backtest/`.
- Missing/empty journal → exit 0 with "no data" message (scheduled task must not accumulate failures). Malformed lines skipped and counted.
- Buckets n<5 hidden; 5≤n<20 marked "low confidence".
- `pnl_eur` on SELL rows is already NET of fees (since commit 7227c6a) — use as-is.
- Scalper code stays in repo untouched; only the schedule changes.
- `daily_backtest.ps1` must stay ASCII-only (PS 5.1 parse quirk).
- Commits end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `backtest/journal_analysis.py` + fixture test

**Files:**
- Create: `backtest/journal_analysis.py`
- Test: `tests/test_journal_analysis.py`

**Interfaces:**
- Produces: CLI `py -3 backtest/journal_analysis.py [--data PATH] [--html PATH]`; library functions `load_trades(path) -> list[dict]` (closed trades: each has `side` ("long"|"short"), `pair`, `pnl_eur`, `reason`, `ts`, optional `features` dict, optional `hold_minutes`, `short_type`) and `bucket_stats(trades, key_fn) -> dict[str, dict]` (bucket -> {n, wins, net_eur, wr_pct, avg_pct}).

- [ ] **Step 1: Write the failing test** — create `tests/test_journal_analysis.py`:

```python
"""Journal analyzer tests against a synthetic fixture.
Run: py -3 tests/test_journal_analysis.py"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mk_fixture(path):
    rows = [
        # BUY with features -> SELL win (+1.0 net)
        {"ts": "2026-07-10T10:00:00", "type": "BUY", "pair": "ADAEUR", "price": 0.10,
         "reason": "BUY_EXECUTED",
         "extra": {"features": {"rsi_1h": 45.0, "hour_utc": 10, "smart_action": "boost",
                                 "hl_bias": 3.0, "whale_score": 1.0, "strategy": "mean_reversion"}}},
        {"ts": "2026-07-10T12:00:00", "type": "SELL", "pair": "ADAEUR", "price": 0.103,
         "pnl_eur": 1.0, "reason": "TAKE_PROFIT",
         "extra": {"entry_price": 0.10, "pnl_pct": 3.0, "hold_minutes": 120.0}},
        # BUY with features -> SELL loss (-0.5 net)
        {"ts": "2026-07-11T21:00:00", "type": "BUY", "pair": "XRPEUR", "price": 1.0,
         "reason": "BUY_EXECUTED",
         "extra": {"features": {"rsi_1h": 62.0, "hour_utc": 21, "smart_action": "neutral",
                                 "hl_bias": -2.0, "whale_score": -1.0, "strategy": "mean_reversion"}}},
        {"ts": "2026-07-11T23:00:00", "type": "SELL", "pair": "XRPEUR", "price": 0.99,
         "pnl_eur": -0.5, "reason": "STOP_LOSS",
         "extra": {"entry_price": 1.0, "pnl_pct": -1.0, "hold_minutes": 120.0}},
        # Orphan SELL (pre-feature era) -> still counted, no feature buckets
        {"ts": "2026-07-09T05:00:00", "type": "SELL", "pair": "LTCEUR", "price": 39.0,
         "pnl_eur": -0.2, "reason": "TRAILING_STOP", "extra": {}},
        # Short open+close (EVENT)
        {"ts": "2026-07-12T02:00:00", "type": "SHORT_OPEN", "pair": "SOLEUR", "price": 70.0,
         "pnl_eur": 0.0, "reason": "SHORT_OPEN_EXECUTED",
         "extra": {"short_type": "EVENT", "features": {"intelligence_score": -2.4,
                                                        "hl_bias": -3.0, "whale_score": -2.0,
                                                        "hour_utc": 2}}},
        {"ts": "2026-07-12T05:00:00", "type": "SHORT_CLOSE", "pair": "SOLEUR", "price": 68.9,
         "pnl_eur": 0.4, "reason": "SHORT_TAKE_PROFIT", "extra": {}},
        # Malformed line is injected raw in the writer below
    ]
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.write("this is not json\n")


def test_load_and_buckets():
    from backtest.journal_analysis import load_trades, bucket_stats

    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "j.jsonl")
        _mk_fixture(p)
        trades = load_trades(p)

    longs = [t for t in trades if t["side"] == "long"]
    shorts = [t for t in trades if t["side"] == "short"]
    assert len(longs) == 3, longs          # 2 feature-paired + 1 orphan
    assert len(shorts) == 1, shorts
    assert shorts[0].get("short_type") == "EVENT"

    # Feature pairing: the ADA win must carry its BUY features
    ada = next(t for t in longs if t["pair"] == "ADAEUR")
    assert ada["features"]["smart_action"] == "boost"
    assert ada["pnl_eur"] == 1.0

    # Buckets by smart_action
    stats = bucket_stats(longs, lambda t: (t.get("features") or {}).get("smart_action") or "none-recorded")
    assert stats["boost"]["n"] == 1 and stats["boost"]["wins"] == 1
    assert stats["neutral"]["n"] == 1 and stats["neutral"]["wins"] == 0
    assert stats["none-recorded"]["n"] == 1
    assert abs(stats["boost"]["net_eur"] - 1.0) < 1e-9
    print("test_load_and_buckets OK")


def test_empty_and_missing():
    from backtest.journal_analysis import load_trades
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "empty.jsonl")
        open(p, "w").close()
        assert load_trades(p) == []
        assert load_trades(os.path.join(td, "nope.jsonl")) == []
    print("test_empty_and_missing OK")


if __name__ == "__main__":
    test_load_and_buckets()
    test_empty_and_missing()
    print("ALL OK")
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -3 tests/test_journal_analysis.py`
Expected: `ModuleNotFoundError: No module named 'backtest.journal_analysis'` (ensure `backtest/__init__.py` exists; if not, the import will fail differently — create an empty `backtest/__init__.py` only if the import error demands it).

- [ ] **Step 3: Implement `backtest/journal_analysis.py`**

```python
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
from datetime import datetime

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
    with open(path, "r", encoding="utf-8") as f:
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
        conf = "" if s["n"] >= MIN_CONF else " ⚠"
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
    print("JOURNAL ANALYSIS —", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    print(_summary("LONGS", longs))
    print(_summary("SHORTS", shorts))
    print(_summary("EVENT SHORTS", event_shorts))
    print("=" * 62)

    html_parts = [f"<h1>Journal analysis — "
                  f"{datetime.utcnow():%Y-%m-%d %H:%M} UTC</h1>"
                  f"<p>{_summary('LONGS', longs)}<br>{_summary('SHORTS', shorts)}<br>"
                  f"{_summary('EVENT SHORTS', event_shorts)}</p>"
                  f"<p>⚠ = n&lt;{MIN_CONF} (low confidence); buckets n&lt;{MIN_SHOW} hidden</p>"]

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
```

- [ ] **Step 4: Run tests to verify pass**

Run: `py -3 tests/test_journal_analysis.py`
Expected: `test_load_and_buckets OK`, `test_empty_and_missing OK`, `ALL OK`

- [ ] **Step 5: Live dry-run against real data** (uses whatever journal copy exists locally; "no data" exit is also a pass):

Run: `py -3 backtest/journal_analysis.py --data "C:/Users/rober/AppData/Local/Temp/claude/D--Tradingbot/3b2fa3e0-ffec-46ef-a2dd-cd8194157c42/scratchpad/dr2/trade_events_paper.jsonl" --html backtest/journal_report_test.html`
Expected: console summary with LONGS/SHORTS lines + bucket tables; HTML file created. Delete `backtest/journal_report_test.html` afterwards.

- [ ] **Step 6: Commit**

```bash
git add backtest/journal_analysis.py tests/test_journal_analysis.py
git commit -m "feat: long/short journal analyzer (replaces daily scalper backtest)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Rewire `daily_backtest.ps1` + server commands

**Files:**
- Modify: `scripts/daily_backtest.ps1` (whole file — small)
- No repo changes for the server; exact manual commands are printed for the user at the end.

**Interfaces:**
- Consumes: `backtest/journal_analysis.py` CLI (Task 1); restricted-SSH command `journal` (added server-side manually).

- [ ] **Step 1: Replace the body of `scripts/daily_backtest.ps1`** (keep the header comment style, ASCII only):

```powershell
# daily_backtest.ps1 - runs at 9:35am via Windows Task Scheduler
#
# 2026-07-15: switched from the retired scalper backtest to the long/short
# JOURNAL analysis. Pulls trade_events_paper.jsonl via the restricted-key
# "journal" command and runs backtest/journal_analysis.py. REPORT ONLY -
# no recommendations push-back (that fed the retired scalper AI loop).
#
# Server prerequisites (one-time, already installed if this works):
#   - 09:30 cron extracts trade_events_paper.jsonl to /home/botuser/backup/
#   - /home/botuser/bot_auto.sh has a "journal" case streaming that file
#
# SECURITY: uses id_ed25519_botauto - restricted key, fixed commands only.

$SERVER      = "root@178.105.159.157"
$SSH_KEY     = "C:\Users\rober\.ssh\id_ed25519_botauto"
$BOT_DIR     = "D:\Tradingbot"
$DATA_DIR    = "$BOT_DIR\backtest\data"
$LOG_DIR     = "$BOT_DIR\scripts\logs"
$LOG_FILE    = "$LOG_DIR\backtest_$(Get-Date -Format 'yyyy-MM-dd').log"

New-Item -ItemType Directory -Force -Path $LOG_DIR  | Out-Null
New-Item -ItemType Directory -Force -Path $DATA_DIR | Out-Null

function Log($msg) {
    $line = "$(Get-Date -Format 'HH:mm:ss')  $msg"
    Write-Host $line
    Add-Content -Path $LOG_FILE -Value $line -Encoding UTF8
}

Log "=== Daily journal analysis starting ==="

# Step 1: Pull the main-bot trade journal from the server via restricted key
Log "Pulling trade_events_paper.jsonl from server..."
$dest   = "$DATA_DIR\trade_events_paper.jsonl"
$sshOut = & ssh -i $SSH_KEY -o StrictHostKeyChecking=no $SERVER "journal" 2>&1

if ($LASTEXITCODE -ne 0) {
    Log "ERROR: ssh journal failed - $sshOut"
    Log "Check server IP, SSH key, gatekeeper 'journal' case, and the 9:30 cron."
    exit 1
}

$sshOut | Out-File -FilePath $dest -Encoding UTF8
$lines = (Get-Content $dest | Measure-Object -Line).Lines
Log "Downloaded $lines journal records"

# Step 2: Run the journal analyzer (report only)
Log "Running journal analysis..."
$env:PYTHONIOENCODING = "utf-8"
$btOut = & py "$BOT_DIR\backtest\journal_analysis.py" 2>&1
$btOut | ForEach-Object { Log "  $_" }

if ($LASTEXITCODE -ne 0) {
    Log "ERROR: analyzer exited with code $LASTEXITCODE"
    exit 1
}

Log "Report: $BOT_DIR\backtest\journal_report.html"
Log "=== Done ==="
```

- [ ] **Step 2: Verify the script parses under PS 5.1 and is ASCII**

Run: `powershell -NoProfile -Command "$null = [System.Management.Automation.PSParser]::Tokenize((Get-Content -Raw 'scripts/daily_backtest.ps1'), [ref]$null); 'parse OK'"`
Expected: `parse OK`
Run: `py -3 -c "b=open('scripts/daily_backtest.ps1','rb').read(); import sys; sys.exit(0 if max(b)<128 else print('non-ascii byte found'))"`
Expected: silent (exit 0).

- [ ] **Step 3: Commit + push**

```bash
git add scripts/daily_backtest.ps1
git commit -m "chore: daily pipeline analyses the trade journal, retires scalper backtest run

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

- [ ] **Step 4: Print the one-time server commands for the human** (run over their admin SSH; gatekeeper cases match on $1):

```bash
# 1) add the journal extract to the 09:30 cron (append to existing extract cron line's script
#    or add a second line) — extracts the journal from the Docker volume to /home/botuser/backup/
( crontab -l ; echo '30 9 * * * docker run --rm -v bobtradingbot_tradingbot_data:/data -v /home/botuser/backup:/out alpine cp /data/trade_events_paper.jsonl /out/trade_events_paper.jsonl' ) | crontab -

# 2) add the "journal" case to the gatekeeper /home/botuser/bot_auto.sh, next to the existing
#    extract) case:
#      journal)
#          cat /home/botuser/backup/trade_events_paper.jsonl
#          ;;
nano /home/botuser/bot_auto.sh

# 3) prime it once so tomorrow's 09:35 pull works today:
docker run --rm -v bobtradingbot_tradingbot_data:/data -v /home/botuser/backup:/out alpine cp /data/trade_events_paper.jsonl /out/trade_events_paper.jsonl
```

Then verify from the PC: `ssh -i C:\Users\rober\.ssh\id_ed25519_botauto -o StrictHostKeyChecking=no root@178.105.159.157 "journal" | head -c 200` → prints the first journal line as JSON.

- [ ] **Step 5: End-to-end local run** (after the server side is primed):

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/daily_backtest.ps1`
Expected: log shows downloaded record count, analyzer summary tables, `Report: ...journal_report.html`, `=== Done ===`, exit 0.

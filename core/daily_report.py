"""Daily trade activity report — saves a CSV to data/reports/ each morning."""

import csv
import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def _load_trades_last_24h(data_dir: str, paper_mode: bool) -> list:
    cutoff = time.time() - 86400
    trade_file = "trade_events_paper.jsonl" if paper_mode else "trade_events_live.jsonl"
    rows = []

    for fname in (trade_file, "scalper_trades.jsonl"):
        path = os.path.join(data_dir, fname)
        source = "scalper" if fname.startswith("scalper") else "main"
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        ts = float(row.get("timestamp") or row.get("ts") or row.get("time") or 0)
                        if ts >= cutoff:
                            row["_source"] = source
                            rows.append(row)
                    except Exception:
                        pass
        except FileNotFoundError:
            pass

    rows.sort(key=lambda r: float(r.get("timestamp") or r.get("ts") or r.get("time") or 0))
    return rows


def _build_csv(trades: list, paper_mode: bool) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow([
        "Time (UTC)", "Source", "Type", "Pair",
        "Entry Price", "Exit Price", "Volume",
        "P&L EUR", "P&L %", "Reason", "Held (min)"
    ])

    wins = losses = 0
    total_pnl = 0.0

    for t in trades:
        ts = float(t.get("timestamp") or t.get("ts") or t.get("time") or 0)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        pnl_eur = t.get("pnl_eur", "")
        pnl_pct = t.get("pnl_pct", t.get("pnl_percent", ""))
        entry   = t.get("entry_price", t.get("buy_price", ""))
        exit_p  = t.get("exit_price",  t.get("sell_price", ""))
        volume  = t.get("volume", t.get("qty", ""))
        held_min = ""
        entry_ts = t.get("entry_ts", t.get("open_ts", 0))
        if entry_ts and ts:
            held_min = round((ts - float(entry_ts)) / 60, 1)

        if pnl_eur != "":
            pnl_val = float(pnl_eur)
            total_pnl += pnl_val
            wins += 1 if pnl_val >= 0 else 0
            losses += 1 if pnl_val < 0 else 0

        writer.writerow([
            dt,
            t.get("_source", "main"),
            t.get("type", t.get("action", "")),
            t.get("pair", ""),
            f"{float(entry):.6f}"  if entry   != "" else "",
            f"{float(exit_p):.6f}" if exit_p  != "" else "",
            f"{float(volume):.6f}" if volume  != "" else "",
            f"{float(pnl_eur):+.4f}" if pnl_eur != "" else "",
            f"{float(pnl_pct):+.2f}%" if pnl_pct != "" else "",
            t.get("reason", t.get("close_reason", "")),
            held_min,
        ])

    writer.writerow([])
    writer.writerow(["SUMMARY"])
    writer.writerow(["Mode",               "Paper" if paper_mode else "Live"])
    writer.writerow(["Period",             "Last 24 hours"])
    writer.writerow(["Total closed trades", wins + losses])
    writer.writerow(["Wins",               wins])
    writer.writerow(["Losses",             losses])
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    writer.writerow(["Win rate",           f"{win_rate:.1f}%"])
    writer.writerow(["Total P&L (EUR)",    f"{total_pnl:+.4f}"])

    return buf.getvalue()


def save_daily_report(data_dir: str, paper_mode: bool) -> str:
    """Generate and save the 24h CSV report. Returns the file path."""
    trades = _load_trades_last_24h(data_dir, paper_mode)
    csv_content = _build_csv(trades, paper_mode)

    reports_dir = Path(data_dir) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    path = reports_dir / f"trade_report_{date_str}.csv"
    path.write_text(csv_content, encoding="utf-8")
    return str(path)

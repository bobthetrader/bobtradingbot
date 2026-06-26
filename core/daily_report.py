"""Daily trade activity report — generates a CSV and emails it via Gmail SMTP."""

import csv
import io
import json
import os
import smtplib
import time
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


def _load_trades_last_24h(data_dir: str, paper_mode: bool) -> list[dict]:
    """Read all trade events from the last 24h across main bot and scalper."""
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


def _build_csv(trades: list[dict], paper_mode: bool) -> str:
    """Return CSV string of trade activity."""
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
        source  = t.get("_source", "main")
        ttype   = t.get("type", t.get("action", ""))
        pair    = t.get("pair", "")
        entry   = t.get("entry_price", t.get("buy_price", ""))
        exit_p  = t.get("exit_price",  t.get("sell_price", ""))
        volume  = t.get("volume", t.get("qty", ""))
        pnl_eur = t.get("pnl_eur", "")
        pnl_pct = t.get("pnl_pct", t.get("pnl_percent", ""))
        reason  = t.get("reason", t.get("close_reason", ""))

        held_min = ""
        entry_ts = t.get("entry_ts", t.get("open_ts", 0))
        if entry_ts and ts:
            held_min = round((ts - float(entry_ts)) / 60, 1)

        if pnl_eur != "":
            pnl_val = float(pnl_eur)
            total_pnl += pnl_val
            if pnl_val >= 0:
                wins += 1
            else:
                losses += 1

        writer.writerow([
            dt, source, ttype, pair,
            f"{float(entry):.6f}" if entry != "" else "",
            f"{float(exit_p):.6f}" if exit_p != "" else "",
            f"{float(volume):.6f}" if volume != "" else "",
            f"{float(pnl_eur):+.4f}" if pnl_eur != "" else "",
            f"{float(pnl_pct):+.2f}%" if pnl_pct != "" else "",
            reason,
            held_min,
        ])

    # Summary rows
    writer.writerow([])
    writer.writerow(["SUMMARY"])
    mode_label = "Paper" if paper_mode else "Live"
    writer.writerow(["Mode", mode_label])
    writer.writerow(["Period", "Last 24 hours"])
    writer.writerow(["Total closed trades", wins + losses])
    writer.writerow(["Wins", wins])
    writer.writerow(["Losses", losses])
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    writer.writerow(["Win rate", f"{win_rate:.1f}%"])
    writer.writerow(["Total P&L (EUR)", f"{total_pnl:+.4f}"])

    return buf.getvalue()


def send_daily_report(
    data_dir: str,
    paper_mode: bool,
    smtp_user: str,
    smtp_app_password: str,
    report_email: str,
) -> bool:
    """Generate and email the 24h report. Returns True on success."""
    try:
        trades = _load_trades_last_24h(data_dir, paper_mode)
        csv_content = _build_csv(trades, paper_mode)

        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        mode_label = "Paper" if paper_mode else "Live"
        subject = f"Bob Trading Bot — Daily Report {date_str} ({mode_label})"
        filename = f"trade_report_{date_str}.csv"

        closed = sum(1 for t in trades if t.get("pnl_eur") is not None)
        total_pnl = sum(float(t.get("pnl_eur", 0)) for t in trades if t.get("pnl_eur") is not None)
        body = (
            f"Daily trading report for {date_str}.\n\n"
            f"Mode:           {mode_label}\n"
            f"Closed trades:  {closed}\n"
            f"Total P&L:      {total_pnl:+.4f} EUR\n\n"
            f"Full breakdown attached as CSV — open in LibreOffice Calc or import to Google Sheets.\n"
        )

        msg = MIMEMultipart()
        msg["From"]    = smtp_user
        msg["To"]      = report_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        attachment = MIMEBase("application", "octet-stream")
        attachment.set_payload(csv_content.encode("utf-8"))
        encoders.encode_base64(attachment)
        attachment.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        msg.attach(attachment)

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_app_password)
            server.sendmail(smtp_user, report_email, msg.as_string())

        return True

    except Exception as exc:
        raise RuntimeError(f"Daily report email failed: {exc}") from exc

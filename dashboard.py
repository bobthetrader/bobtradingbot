"""
Web Dashboard
==============
A lightweight Flask app that reads the bot's data files and serves
a live status page. Runs in a background thread inside the same
container as the trading bot.

Railway exposes the PORT environment variable — Flask binds to it
so Railway can route HTTP traffic to the dashboard.

Access at: your Railway service public URL (e.g. https://bobtradingbot-xxxx.railway.app)
"""

import json
import os
import threading
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _read_json(filename: str, default=None):
    try:
        path = os.path.join(DATA_DIR, filename)
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _read_jsonl_tail(filename: str, n: int = 20) -> list:
    """Read last N lines from a JSONL file."""
    path = os.path.join(DATA_DIR, filename)
    lines = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        lines.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return lines[-n:]


def _signal_colour(signal: str) -> str:
    return {"BUY": "#00c851", "SELL": "#ff4444", "HOLD": "#aaaaaa"}.get(signal, "#aaaaaa")


def _verdict_colour(verdict: str) -> str:
    return {
        "success":           "#00c851",
        "neutral":           "#ffbb33",
        "failure":           "#ff4444",
        "insufficient_data": "#aaaaaa",
    }.get(verdict, "#aaaaaa")


def _trending_arrow(trending: str) -> str:
    return {"toward_success": "▲", "toward_failure": "▼", "stable": "●"}.get(trending, "●")


def _age(ts_str: str) -> str:
    """Human-readable age of a timestamp."""
    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - ts).total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        return f"{secs // 3600}h ago"
    except Exception:
        return "?"


HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>Bob Trading Bot</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0d1117; color: #e6edf3; font-family: 'Courier New', monospace; font-size: 14px; padding: 16px; }}
    h1 {{ font-size: 18px; color: #58a6ff; margin-bottom: 4px; }}
    .subtitle {{ color: #8b949e; font-size: 12px; margin-bottom: 16px; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }}
    @media(max-width:600px) {{ .grid {{ grid-template-columns: 1fr; }} }}
    .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }}
    .card h2 {{ font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }}
    .big {{ font-size: 28px; font-weight: bold; color: #e6edf3; }}
    .sub {{ font-size: 12px; color: #8b949e; margin-top: 2px; }}
    .pill {{ display:inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; }}
    table {{ width: 100%; border-collapse: collapse; }}
    td, th {{ padding: 6px 8px; text-align: left; border-bottom: 1px solid #21262d; font-size: 13px; }}
    th {{ color: #8b949e; font-weight: normal; font-size: 11px; text-transform: uppercase; }}
    .green {{ color: #00c851; }} .red {{ color: #ff4444; }} .grey {{ color: #8b949e; }}
    .full {{ grid-column: 1 / -1; }}
    .badge {{ font-size: 11px; padding: 1px 6px; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>&#x1F916; Bob Trading Bot</h1>
  <div class="subtitle">Auto-refreshes every 30s &nbsp;&#x2022;&nbsp; Updated: {updated} &nbsp;&#x2022;&nbsp; Loop #{loop} &nbsp;&#x2022;&nbsp; {mode}</div>

  <div class="grid">

    <!-- Balance card -->
    <div class="card">
      <h2>Balance</h2>
      <div class="big">&euro;{balance}</div>
      <div class="sub">Started: &euro;{initial} &nbsp; P&amp;L: <span style="color:{pnl_colour}">{pnl_sign}&euro;{pnl}</span></div>
      <div class="sub" style="margin-top:6px">Trades: {trade_count} &nbsp;&#x2022;&nbsp; Regime: {regime}</div>
    </div>

    <!-- Sharpe card -->
    <div class="card">
      <h2>Sharpe Score</h2>
      <div class="big" style="color:{sharpe_colour}">{sharpe}</div>
      <div class="sub">
        <span class="pill" style="background:{sharpe_colour}22;color:{sharpe_colour}">{verdict}</span>
        &nbsp; {arrow} {trending}
      </div>
      <div class="sub" style="margin-top:6px">Target: &gt;3.0 (success) &nbsp;&#x2022;&nbsp; Exit: &lt;1.0 (failure)</div>
    </div>

    <!-- Signals card -->
    <div class="card">
      <h2>Pair Signals &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">AI Panel: {intel_score:+.1f}</span></h2>
      <table>
        <tr><th>Pair</th><th>Signal</th><th>Score</th></tr>
        {signal_rows}
      </table>
    </div>

    <!-- Open positions card -->
    <div class="card">
      <h2>Open Positions</h2>
      {positions_html}
    </div>

    <!-- AI model panel card -->
    <div class="card full">
      <h2>AI Model Panel &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">Combined: {intel_score:+.1f} / 5</span></h2>
      {model_html}
    </div>

    <!-- Recent trades card -->
    <div class="card full">
      <h2>Recent Trades</h2>
      {trades_html}
    </div>

  </div>

  <div style="color:#30363d;font-size:11px;margin-top:8px">
    Sharpe success &ge;3.0 &nbsp;&#x2022;&nbsp; Optimizer runs every 10 closed trades &nbsp;&#x2022;&nbsp; 4-model AI panel via OpenRouter + OpenAI
  </div>
</body>
</html>"""


def _build_page() -> str:
    status = _read_json("bot_status.json")
    trades = _read_jsonl_tail("trade_events.jsonl", n=15)

    # ── Balance ───────────────────────────────────────────────────────────────
    balance   = status.get("balance_eur", 0.0)
    initial   = status.get("initial_balance", 100.0)
    pnl       = status.get("adjusted_pnl", 0.0)
    pnl_colour= "#00c851" if pnl >= 0 else "#ff4444"
    pnl_sign  = "+" if pnl >= 0 else ""

    # ── Sharpe ────────────────────────────────────────────────────────────────
    sharpe  = status.get("sharpe")
    sharpe_str = f"{sharpe:.3f}" if sharpe is not None else "—"
    verdict = status.get("sharpe_verdict", "insufficient_data")
    trending= status.get("sharpe_trending", "stable")

    # ── Signals ───────────────────────────────────────────────────────────────
    signal_rows = ""
    for pair in status.get("pair_signals", {}):
        sig   = status["pair_signals"].get(pair, "HOLD")
        score = status.get("pair_scores", {}).get(pair, 0.0)
        colour= _signal_colour(sig)
        signal_rows += (
            f'<tr><td>{pair}</td>'
            f'<td><span style="color:{colour};font-weight:bold">{sig}</span></td>'
            f'<td style="color:{colour}">{score:+.2f}</td></tr>'
        )
    if not signal_rows:
        signal_rows = '<tr><td colspan="3" class="grey">Waiting for signals…</td></tr>'

    # ── AI model panel ────────────────────────────────────────────────────────
    model_scores  = status.get("model_scores", {})
    model_outputs = status.get("model_outputs", {})
    _MODEL_META = {
        "hermes":   ("Hermes 3 70B",    "Strategy & positioning",     "#7c3aed"),
        "sonar":    ("Perplexity Sonar", "Live web search",            "#0ea5e9"),
        "deepseek": ("DeepSeek R1",      "Technical pattern reasoning","#f59e0b"),
        "mistral":  ("Mistral 7B",       "Fast sentiment check",       "#10b981"),
        "gpt":      ("GPT-4o-mini",      "General sentiment",          "#6366f1"),
    }
    if model_scores:
        model_rows = ""
        for key, (name, role, colour) in _MODEL_META.items():
            score = model_scores.get(key)
            text  = model_outputs.get(key, "—")
            if score is None:
                score_html = '<span class="grey">—</span>'
                bar_w = 0
                bar_col = "#30363d"
            else:
                bar_w   = int(abs(score) / 5.0 * 100)
                bar_col = "#00c851" if score >= 0 else "#ff4444"
                score_html = f'<span style="color:{bar_col};font-weight:bold">{score:+.1f}</span>'
            short_text = text[:80] + "…" if len(text) > 80 else text
            model_rows += (
                f'<tr>'
                f'<td><span style="color:{colour};font-weight:bold">{name}</span>'
                f'<br><span class="grey" style="font-size:11px">{role}</span></td>'
                f'<td>{score_html}'
                f'<div style="background:#21262d;border-radius:3px;height:4px;margin-top:4px">'
                f'<div style="background:{bar_col};width:{bar_w}%;height:4px;border-radius:3px"></div>'
                f'</div></td>'
                f'<td style="font-size:11px;color:#8b949e">{short_text}</td>'
                f'</tr>'
            )
        model_html = (
            '<table><tr><th>Model</th><th style="width:80px">Score</th><th>Reasoning</th></tr>'
            + model_rows + '</table>'
        )
    else:
        model_html = '<div class="grey" style="padding:8px 0">Waiting for first AI panel refresh (up to 10 min)…</div>'

    # ── Open positions ────────────────────────────────────────────────────────
    positions = {**status.get("open_positions", {})}
    shorts    = status.get("open_shorts", {})
    pos_rows  = ""
    for pair, info in positions.items():
        pos_rows += (
            f'<tr><td>{pair}</td><td class="green">LONG</td>'
            f'<td>{info["qty"]}</td><td>&euro;{info["entry"]}</td></tr>'
        )
    for pair, info in shorts.items():
        pos_rows += (
            f'<tr><td>{pair}</td><td class="red">SHORT</td>'
            f'<td>{info["qty"]}</td><td>&euro;{info["entry"]}</td></tr>'
        )
    if pos_rows:
        positions_html = (
            '<table><tr><th>Pair</th><th>Side</th><th>Qty</th><th>Entry</th></tr>'
            + pos_rows + '</table>'
        )
    else:
        positions_html = '<div class="grey" style="padding:8px 0">No open positions</div>'

    # ── Recent trades ─────────────────────────────────────────────────────────
    if trades:
        trade_rows = ""
        for t in reversed(trades):
            ttype  = t.get("type", "")
            pair   = t.get("pair", "")
            price  = t.get("price", 0.0)
            pnl_t  = t.get("pnl_eur", 0.0)
            ts     = t.get("ts", "")
            colour = "#00c851" if "BUY" in ttype or "OPEN" in ttype else "#ff4444"
            pnl_col= "#00c851" if pnl_t >= 0 else "#ff4444"
            pnl_display = f'<span style="color:{pnl_col}">{pnl_t:+.4f}</span>' if pnl_t != 0 else "—"
            trade_rows += (
                f'<tr>'
                f'<td class="grey">{_age(ts)}</td>'
                f'<td><span style="color:{colour};font-weight:bold">{ttype}</span></td>'
                f'<td>{pair}</td>'
                f'<td>&euro;{price:,.4f}</td>'
                f'<td>{pnl_display}</td>'
                f'</tr>'
            )
        trades_html = (
            '<table><tr><th>When</th><th>Type</th><th>Pair</th><th>Price</th><th>P&amp;L</th></tr>'
            + trade_rows + '</table>'
        )
    else:
        trades_html = '<div class="grey" style="padding:8px 0">No trades yet</div>'

    # ── Render ────────────────────────────────────────────────────────────────
    ts_raw  = status.get("ts", "")
    mode    = "&#x1F4C4; PAPER MODE" if status.get("paper_mode", True) else "&#x1F7E2; LIVE MODE"

    return HTML_TEMPLATE.format(
        updated       = _age(ts_raw) if ts_raw else "starting…",
        loop          = status.get("loop", 0),
        mode          = mode,
        balance       = f"{balance:,.2f}",
        initial       = f"{initial:,.2f}",
        pnl           = f"{abs(pnl):,.4f}",
        pnl_sign      = pnl_sign,
        pnl_colour    = pnl_colour,
        trade_count   = status.get("trade_count", 0),
        regime        = status.get("regime", "—"),
        sharpe        = sharpe_str,
        sharpe_colour = _verdict_colour(verdict),
        verdict       = verdict.replace("_", " "),
        arrow         = _trending_arrow(trending),
        trending      = trending.replace("_", " "),
        intel_score   = status.get("intelligence_score", 0.0),
        signal_rows   = signal_rows,
        model_html    = model_html,
        positions_html= positions_html,
        trades_html   = trades_html,
    )


def start_dashboard(port: int = 8080):
    """Start the Flask dashboard in a background daemon thread."""
    try:
        from flask import Flask, Response
        app = Flask(__name__)

        @app.route("/")
        def index():
            return Response(_build_page(), mimetype="text/html")

        @app.route("/health")
        def health():
            return "ok", 200

        def _run():
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

        t = threading.Thread(target=_run, name="dashboard", daemon=True)
        t.start()
        logger.info("Dashboard started on port %d", port)
    except Exception as exc:
        logger.warning("Dashboard could not start: %s", exc)

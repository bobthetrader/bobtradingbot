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
      <h2>Pair Signals &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">AI Panel: {intel_score:+.1f}</span> &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">Short: {btc_mode}</span></h2>
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

    <!-- Monthly return card -->
    <div class="card full">
      <h2>Monthly Return Target &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">Goal: +3% to +8% per month</span></h2>
      {monthly_html}
    </div>

    <!-- Alpaca card -->
    <div class="card full">
      <h2>Alpaca Correlated Stocks &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">MSTR · COIN · MARA — mirrors BTC/ETH signals</span></h2>
      {alpaca_html}
    </div>

    <!-- Kraken news card -->
    <div class="card full">
      <h2>Kraken News &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">blog.kraken.com — refreshed every 10 min</span></h2>
      {kraken_news_html}
    </div>

    <!-- Optimizer card -->
    <div class="card full">
      <h2>Scientific Method Optimizer &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">Sharpe target: &ge;3.0 success | &lt;1.0 failure</span></h2>
      {optimizer_html}
    </div>

    <!-- New listings card -->
    <div class="card full">
      <h2>New Listings Monitor &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">60 min wait → buy if +2% → sell after 12h</span></h2>
      {listings_html}
    </div>

    <!-- Sharpe.ai derivatives card -->
    <div class="card full">
      <h2>Sharpe.ai Derivatives</h2>
      {sharpe_html}
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

    <!-- Intelligence log card -->
    <div class="card full">
      <h2>AI Panel History &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">last 10 assessments</span></h2>
      {intel_log_html}
    </div>

  </div>

  <div style="color:#30363d;font-size:11px;margin-top:8px">
    Sharpe success &ge;3.0 &nbsp;&#x2022;&nbsp; Optimizer runs every 10 closed trades &nbsp;&#x2022;&nbsp; 4-model AI panel via OpenRouter + OpenAI
    &nbsp;&#x2022;&nbsp; {db_summary}
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
    breakout_ages = status.get("breakout_ages_days", {})
    btc_mode = status.get("short_mode", "HEDGE (3% NAV)")
    for pair in status.get("pair_signals", {}):
        sig    = status["pair_signals"].get(pair, "HOLD")
        score  = status.get("pair_scores", {}).get(pair, 0.0)
        colour = _signal_colour(sig)
        age    = breakout_ages.get(pair)
        age_str = f' <span class="grey" style="font-size:10px">({age:.0f}d breakout)</span>' if age else ""
        signal_rows += (
            f'<tr><td>{pair}{age_str}</td>'
            f'<td><span style="color:{colour};font-weight:bold">{sig}</span></td>'
            f'<td style="color:{colour}">{score:+.2f}</td></tr>'
        )
    if not signal_rows:
        signal_rows = '<tr><td colspan="3" class="grey">Waiting for signals…</td></tr>'

    # ── Monthly return ────────────────────────────────────────────────────────
    monthly_pct   = status.get("monthly_return_pct", 0.0)
    monthly_start = status.get("monthly_start_bal", 0.0)
    target_lo     = status.get("monthly_target_low", 3.0)
    target_hi     = status.get("monthly_target_high", 8.0)

    if monthly_pct >= target_hi:
        m_col   = "#ffd700"   # gold — above target, protecting gains
        m_label = f"+{monthly_pct:.2f}% ✓ TARGET EXCEEDED — position sizing reduced to protect gains"
    elif monthly_pct >= target_lo:
        m_col   = "#00c851"   # green — in target range
        m_label = f"+{monthly_pct:.2f}% ✓ ON TARGET"
    elif monthly_pct >= 0:
        m_col   = "#ffbb33"   # yellow — positive but below target
        m_label = f"+{monthly_pct:.2f}% — building toward {target_lo}% target"
    else:
        m_col   = "#ff4444"   # red — negative
        m_label = f"{monthly_pct:.2f}% — below breakeven, slight aggression enabled"

    # Progress bar: 0% → 8% range, clamp at 100%
    bar_pct  = max(0, min(100, (monthly_pct / target_hi) * 100))
    bar_col  = m_col
    zone_lo  = round((target_lo / target_hi) * 100)   # where 3% sits on bar

    monthly_html = f"""
    <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
      <div style="font-size:32px;font-weight:bold;color:{m_col}">{monthly_pct:+.2f}%</div>
      <div>
        <div style="font-size:13px;color:{m_col};margin-bottom:4px">{m_label}</div>
        <div style="font-size:11px;color:#8b949e">
          Month start: €{monthly_start:.2f} &nbsp;•&nbsp; Target: +{target_lo}% to +{target_hi}%
        </div>
      </div>
    </div>
    <div style="margin-top:12px;background:#21262d;border-radius:6px;height:12px;position:relative">
      <div style="position:absolute;left:{zone_lo}%;top:0;bottom:0;width:2px;background:#30363d"></div>
      <div style="background:#30363d;border-radius:6px;position:absolute;left:{zone_lo}%;right:0;top:0;bottom:0"></div>
      <div style="background:{bar_col};width:{bar_pct:.0f}%;height:12px;border-radius:6px;transition:width 1s"></div>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#8b949e;margin-top:3px">
      <span>0%</span><span style="color:#ffbb33">+{target_lo}% target floor</span><span style="color:#ffd700">+{target_hi}% cap</span>
    </div>"""

    # ── Alpaca ────────────────────────────────────────────────────────────────
    alpaca_data = status.get("alpaca", {})
    if alpaca_data:
        mkt_col  = "#00c851" if alpaca_data.get("market_open") else "#ff4444"
        mkt_label= "OPEN" if alpaca_data.get("market_open") else "CLOSED"
        a_rows   = ""
        for pos in alpaca_data.get("positions", []):
            pl      = pos.get("unrealized_pl", 0)
            pl_pct  = pos.get("unrealized_plpc", 0)
            pl_col  = "#00c851" if pl >= 0 else "#ff4444"
            a_rows += (
                f'<tr>'
                f'<td><b>{pos.get("symbol")}</b></td>'
                f'<td>{pos.get("qty"):.4f} shares</td>'
                f'<td>${pos.get("market_value", 0):.2f}</td>'
                f'<td style="color:{pl_col}">{pl:+.2f} ({pl_pct:+.2f}%)</td>'
                f'</tr>'
            )
        pos_table = (
            '<table><tr><th>Symbol</th><th>Qty</th><th>Value</th><th>P&amp;L</th></tr>'
            + a_rows + '</table>'
        ) if a_rows else '<div class="grey">No open positions — waiting for strong BTC/ETH signal (score &ge;10)</div>'
        alpaca_html = (
            f'<div style="margin-bottom:10px">'
            f'Portfolio: <b>${alpaca_data.get("portfolio_value", 0):,.2f}</b> &nbsp;'
            f'Market: <span style="color:{mkt_col};font-weight:bold">{mkt_label}</span>'
            f'</div>' + pos_table
        )
    else:
        alpaca_html = '<div class="grey" style="padding:8px 0">Add ALPACA_API_KEY, ALPACA_API_SECRET and ALPACA_BASE_URL to Railway Variables to enable.</div>'

    # ── Kraken news ───────────────────────────────────────────────────────────
    headlines = status.get("kraken_headlines", [])
    if headlines:
        news_rows = ""
        for h in headlines:
            is_listing = h.get("is_listing", False)
            ts         = h.get("ts", 0)
            from datetime import datetime, timezone
            try:
                age_h = (datetime.now(timezone.utc).timestamp() - ts) / 3600
                age   = f"{int(age_h)}h ago" if age_h < 24 else f"{int(age_h/24)}d ago"
            except Exception:
                age = ""
            row_style = "background:#1a2420;" if is_listing else ""
            tag = '<span style="color:#00c851;font-size:10px;font-weight:bold"> [LISTING]</span>' if is_listing else ""
            link = h.get("link", "#")
            news_rows += (
                f'<tr style="{row_style}">'
                f'<td style="font-size:11px;color:#8b949e;white-space:nowrap">{age}</td>'
                f'<td><a href="{link}" style="color:#e6edf3;text-decoration:none" target="_blank">'
                f'{h.get("title","")}</a>{tag}</td>'
                f'</tr>'
            )
        kraken_news_html = (
            '<table style="width:100%">'
            '<tr><th style="width:60px">When</th><th>Headline</th></tr>'
            + news_rows + '</table>'
        )
    else:
        kraken_news_html = '<div class="grey" style="padding:8px 0">Fetching Kraken blog headlines — updates every 10 minutes.</div>'

    # ── Optimizer ─────────────────────────────────────────────────────────────
    opt = status.get("optimizer", {})
    n_trades_sharpe = status.get("sharpe_n_trades", 0)
    if opt:
        opt_lines = []
        # Current experiment
        exp = opt.get("current_experiment")
        baseline = opt.get("baseline_sharpe")
        if exp and exp.get("param"):
            opt_lines.append(
                f'<div style="margin-bottom:10px;padding:10px;background:#161b22;border:1px solid #30363d;border-radius:6px">'
                f'<span style="color:#ffbb33;font-weight:bold">ACTIVE EXPERIMENT</span> &nbsp; '
                f'<code>{exp["param"]}</code>: '
                f'<span class="grey">{exp["old_value"]}</span> '
                f'&rarr; <span style="color:#58a6ff;font-weight:bold">{exp["new_value"]}</span> '
                f'({exp.get("direction","?")})'
                f'<span class="grey" style="margin-left:12px;font-size:11px">'
                f'Sharpe at start: {exp.get("sharpe_at_start") or "—"}'
                f'</span></div>'
            )
        elif baseline is not None:
            opt_lines.append(
                f'<div class="grey" style="margin-bottom:10px">Baseline Sharpe: <b>{baseline:.3f}</b> | '
                f'Waiting for next experiment trigger ({n_trades_sharpe} closed trades so far)</div>'
            )

        # History table
        hist = opt.get("history", [])
        if hist:
            rows = ""
            for h in hist:
                pct = h.get("pct_change")
                verdict = h.get("verdict", "?")
                if verdict == "kept":
                    v_col, v_icon = "#00c851", "KEPT"
                else:
                    v_col, v_icon = "#ff4444", "REVERTED"
                pct_str = ""
                if pct is not None:
                    pct_col = "#00c851" if pct > 0 else "#ff4444"
                    pct_str = f'<span style="color:{pct_col};font-weight:bold">{pct:+.1f}%</span>'
                s_before = f'{h["sharpe_before"]:.3f}' if h.get("sharpe_before") is not None else "—"
                s_after  = f'{h["sharpe_after"]:.3f}'  if h.get("sharpe_after")  is not None else "—"
                rows += (
                    f'<tr>'
                    f'<td><code>{h.get("param","?")}</code></td>'
                    f'<td class="grey">{h.get("old","?")} &rarr; {h.get("new","?")}</td>'
                    f'<td>{s_before}</td><td>{s_after}</td>'
                    f'<td>{pct_str}</td>'
                    f'<td style="color:{v_col};font-weight:bold">{v_icon}</td>'
                    f'</tr>'
                )
            opt_lines.append(
                '<table><tr><th>Parameter</th><th>Change</th>'
                '<th>Sharpe Before</th><th>Sharpe After</th>'
                '<th>Impact</th><th>Result</th></tr>'
                + rows + '</table>'
            )
        else:
            opt_lines.append('<div class="grey">No experiments completed yet — runs after every 10 closed trades.</div>')

        optimizer_html = "\n".join(opt_lines)
    else:
        optimizer_html = '<div class="grey" style="padding:8px 0">Optimizer initialising — needs 5+ closed trades to establish Sharpe baseline.</div>'

    # ── New listings ──────────────────────────────────────────────────────────
    new_listings = status.get("new_listings", {})
    if new_listings:
        lst_rows = ""
        for sym, info in new_listings.items():
            bought    = info.get("bought", False)
            pnl_pct   = info.get("pnl_pct", 0.0)
            hrs_left  = info.get("hours_left", 0)
            cur       = info.get("current", 0)
            init      = info.get("initial_price", 0)
            buy_price = info.get("buy_price", 0)
            chg_from_init = round(((cur - init) / init) * 100, 2) if init > 0 else 0.0
            status_label = "HOLDING" if bought else ("WATCHING" if chg_from_init < 2 else "READY TO BUY")
            status_col   = "#00c851" if bought else ("#ffbb33" if chg_from_init >= 2 else "#8b949e")
            pnl_col      = "#00c851" if pnl_pct >= 0 else "#ff4444"
            lst_rows += (
                f'<tr>'
                f'<td><b>{sym}</b><br><span class="grey" style="font-size:10px">{info.get("pair","")}</span></td>'
                f'<td><span style="color:{status_col};font-weight:bold">{status_label}</span></td>'
                f'<td>&euro;{cur:.6f}<br><span class="grey" style="font-size:10px">init: &euro;{init:.6f}</span></td>'
                f'<td style="color:{("#ffbb33" if chg_from_init >= 2 else "#8b949e")}">{chg_from_init:+.2f}%</td>'
                f'<td>{info.get("qty", 0):.6f}</td>'
                f'<td style="color:{pnl_col}">{pnl_pct:+.2f}%</td>'
                f'<td class="grey">{hrs_left:.1f}h left</td>'
                f'</tr>'
            )
        listings_html = (
            '<table><tr>'
            '<th>Coin</th><th>Status</th><th>Price</th>'
            '<th>vs Detection</th><th>Qty Held</th><th>P&amp;L</th><th>Timer</th>'
            '</tr>' + lst_rows + '</table>'
        )
    else:
        listings_html = '<div class="grey" style="padding:8px 0">No new Kraken listings detected in last 24h — checking every 10 minutes via Sharpe.ai</div>'

    # ── Sharpe.ai derivatives ─────────────────────────────────────────────────
    funding       = status.get("sharpe_funding", {})
    insider_sig   = status.get("sharpe_insider", None)
    if funding:
        sharpe_rows = ""
        for coin, f_score in sorted(funding.items()):
            f_col = "#ff4444" if f_score < -1 else ("#00c851" if f_score > 1 else "#ffbb33")
            f_str = f'<span style="color:{f_col};font-weight:bold">{f_score:+.1f}</span>'
            sharpe_rows += f'<tr><td>{coin}</td><td>{f_str}</td></tr>'
        ins_str = ""
        if insider_sig is not None:
            ins_col = "#ff4444" if insider_sig < -1 else ("#00c851" if insider_sig > 0 else "#8b949e")
            ins_str = (
                f'<div style="margin-top:10px;font-size:12px">'
                f'Market insider signal: <span style="color:{ins_col};font-weight:bold">{float(insider_sig):+.1f}</span>'
                f'<span class="grey"> (negative = altcoin short pressure detected)</span></div>'
            )
        sharpe_html = (
            '<table><tr>'
            '<th>Pair</th>'
            '<th>Funding Signal<br><span class="grey" style="font-size:10px">- = longs crowded (bearish) | + = shorts crowded (bullish)</span></th>'
            '</tr>'
            + sharpe_rows + '</table>' + ins_str
        )
    else:
        sharpe_html = '<div class="grey" style="padding:8px 0">Add SHARPE_API_KEY to Railway Variables to enable institutional derivatives data.</div>'

    # ── AI model panel ────────────────────────────────────────────────────────
    model_scores  = status.get("model_scores", {})
    model_outputs = status.get("model_outputs", {})
    _MODEL_META = {
        "hermes":   ("Hermes 3 70B",    "Strategy & positioning",     "#7c3aed"),
        "sonar":    ("Perplexity Sonar", "Live web search",            "#0ea5e9"),
        "deepseek": ("DeepSeek R1",      "Technical pattern reasoning","#f59e0b"),
        "mistral":  ("Llama 3.1 8B",      "Fast sentiment check",       "#10b981"),
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
        pnl_pct = info.get("pnl_pct", 0.0)
        pnl_eur = info.get("pnl_eur", 0.0)
        pnl_col = "#00c851" if pnl_pct >= 0 else "#ff4444"
        cur = info.get("current", 0)
        pos_rows += (
            f'<tr><td>{pair}</td><td class="green">LONG</td>'
            f'<td>{info["qty"]}</td>'
            f'<td>&euro;{info["entry"]}</td>'
            f'<td>&euro;{cur:,.4f}</td>'
            f'<td style="color:{pnl_col}">{pnl_pct:+.2f}% ({pnl_eur:+.4f})</td></tr>'
        )
    for pair, info in shorts.items():
        pos_rows += (
            f'<tr><td>{pair}</td><td class="red">SHORT</td>'
            f'<td>{info["qty"]}</td><td>&euro;{info["entry"]}</td></tr>'
        )
    if pos_rows:
        positions_html = (
            '<table><tr><th>Pair</th><th>Side</th><th>Qty</th><th>Entry</th><th>Current</th><th>P&amp;L</th></tr>'
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

    # ── Intelligence log ──────────────────────────────────────────────────────
    intel_entries = _read_jsonl_tail("intelligence_log.jsonl", n=10)
    if intel_entries:
        _MCOLS = ["hermes", "sonar", "deepseek", "mistral", "gpt"]
        _MCOLOURS = {"hermes":"#7c3aed","sonar":"#0ea5e9","deepseek":"#f59e0b","mistral":"#10b981","gpt":"#6366f1"}
        header_cells = "".join(f"<th>{m}</th>" for m in _MCOLS)
        il_rows = ""
        for entry in reversed(intel_entries):
            ts_str   = entry.get("ts", "")[:16].replace("T", " ")
            combined = entry.get("combined_score", 0.0)
            mscores  = entry.get("model_scores", {})
            outcome  = entry.get("market_outcome", "pending")
            comb_col = "#00c851" if combined >= 1 else ("#ff4444" if combined <= -1 else "#ffbb33")
            out_col  = "#00c851" if "WIN" in outcome else ("#ff4444" if "LOSS" in outcome else "#8b949e")
            score_cells = ""
            for m in _MCOLS:
                s = mscores.get(m)
                if s is None:
                    score_cells += '<td class="grey">—</td>'
                else:
                    col = "#00c851" if s >= 1 else ("#ff4444" if s <= -1 else "#ffbb33")
                    score_cells += f'<td style="color:{col}">{s:+.1f}</td>'
            il_rows += (
                f'<tr>'
                f'<td class="grey" style="font-size:11px">{ts_str}</td>'
                f'<td style="color:{comb_col};font-weight:bold">{combined:+.2f}</td>'
                f'{score_cells}'
                f'<td style="color:{out_col};font-size:11px">{outcome}</td>'
                f'</tr>'
            )
        intel_log_html = (
            f'<table><tr><th>Time</th><th>Combined</th>{header_cells}<th>Outcome</th></tr>'
            + il_rows + '</table>'
        )
    else:
        intel_log_html = '<div class="grey" style="padding:8px 0">No AI panel history yet — first entry appears after the next 10-minute refresh.</div>'

    # ── DB stats ──────────────────────────────────────────────────────────────
    db_stats = status.get("db_stats", {})
    if db_stats:
        oldest = db_stats.get("oldest_trade", "")[:10] or "today"
        db_summary = (
            f"History DB: {db_stats.get('trades', 0)} trades | "
            f"{db_stats.get('ai_panels', 0)} AI panels | "
            f"{db_stats.get('sharpe_snapshots', 0)} Sharpe snapshots | "
            f"since {oldest}"
        )
    else:
        db_summary = "History DB: initialising…"

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
        btc_mode      = btc_mode,
        signal_rows   = signal_rows,
        monthly_html  = monthly_html,
        alpaca_html      = alpaca_html,
        kraken_news_html = kraken_news_html,
        optimizer_html = optimizer_html,
        listings_html = listings_html,
        sharpe_html   = sharpe_html,
        model_html    = model_html,
        positions_html= positions_html,
        trades_html    = trades_html,
        intel_log_html = intel_log_html,
        db_summary     = db_summary,
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

        @app.route("/force-buy")
        def force_buy():
            try:
                path = os.path.join(DATA_DIR, "FORCE_BUY")
                open(path, "w").close()
                return Response(
                    "<html><body style='background:#0d1117;color:#00c851;font-family:monospace;padding:40px'>"
                    "<h2>&#x2705; FORCE_BUY triggered</h2>"
                    "<p>The bot will execute a buy on the next loop (within 60 seconds).</p>"
                    "<p>Watch your Telegram for the BUY notification.</p>"
                    "<a href='/' style='color:#58a6ff'>← Back to dashboard</a>"
                    "</body></html>",
                    mimetype="text/html"
                )
            except Exception as e:
                return Response(f"Error: {e}", status=500)

        @app.route("/clear-state")
        def clear_state():
            try:
                import json as _json
                cleared = []
                for fname in ("purchase_prices_paper.json", "purchase_prices_live.json"):
                    p = os.path.join(DATA_DIR, fname)
                    if os.path.exists(p):
                        with open(p, "w") as f:
                            _json.dump({}, f)
                        cleared.append(fname)
                return Response(
                    "<html><body style='background:#0d1117;color:#ffbb33;font-family:monospace;padding:40px'>"
                    f"<h2>&#x1F9F9; State cleared</h2>"
                    f"<p>Cleared: {', '.join(cleared) if cleared else 'nothing to clear'}</p>"
                    "<p>Restart the Railway service to reload with a clean slate.</p>"
                    "<a href='/' style='color:#58a6ff'>← Back to dashboard</a>"
                    "</body></html>",
                    mimetype="text/html"
                )
            except Exception as e:
                return Response(f"Error: {e}", status=500)

        @app.route("/force-sell")
        def force_sell():
            try:
                path = os.path.join(DATA_DIR, "FORCE_SELL")
                open(path, "w").close()
                return Response(
                    "<html><body style='background:#0d1117;color:#ff4444;font-family:monospace;padding:40px'>"
                    "<h2>&#x2705; FORCE_SELL triggered</h2>"
                    "<p>The bot will close all open positions on the next loop (within 60 seconds).</p>"
                    "<p>Watch your Telegram for the SELL notification.</p>"
                    "<a href='/' style='color:#58a6ff'>← Back to dashboard</a>"
                    "</body></html>",
                    mimetype="text/html"
                )
            except Exception as e:
                return Response(f"Error: {e}", status=500)

        def _run():
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

        t = threading.Thread(target=_run, name="dashboard", daemon=True)
        t.start()
        logger.info("Dashboard started on port %d", port)
    except Exception as exc:
        logger.warning("Dashboard could not start: %s", exc)

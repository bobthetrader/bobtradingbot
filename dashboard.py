"""
Web Dashboard
==============
A lightweight Flask app that reads the bot's data files and serves
a live status page. Runs in a background thread inside the same
container as the trading bot.

Flask binds to the PORT environment variable (default 8080).

Access at: your server domain or IP (e.g. https://bobtradingbot.com)
"""

import json
import os
import time
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


_CLOSE_TYPES = {"SELL","CLOSE","STOP_LOSS","TAKE_PROFIT","SHORT_CLOSE","SELL_SHORT","CLOSE_SHORT"}

def _calc_realized_pnl(paper_mode: bool) -> float:
    """Sum pnl_eur across all closed main-bot and scalper trades."""
    trade_file = "trade_events_paper.jsonl" if paper_mode else "trade_events.jsonl"
    total = 0.0
    for fname in (trade_file, "scalper_trades.jsonl"):
        path = os.path.join(DATA_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        if fname.startswith("scalper") or row.get("type") in _CLOSE_TYPES:
                            total += float(row.get("pnl_eur", 0))
                    except Exception:
                        pass
        except FileNotFoundError:
            pass
    return total


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
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='%230d1117'/><polyline points='3,26 9,18 15,22 21,10 29,14' stroke='%2358a6ff' stroke-width='2.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/><circle cx='29' cy='14' r='2.5' fill='%2300c851'/></svg>">
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0d1117; color: #e6edf3; font-family: 'Courier New', monospace; font-size: 14px; padding: 16px; }}
    h1 {{ font-size: 18px; color: #58a6ff; margin-bottom: 4px; }}
    .subtitle {{ color: #8b949e; font-size: 12px; margin-bottom: 16px; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }}
    @media(max-width:600px) {{ .grid {{ grid-template-columns: 1fr; }} }}
    @media(max-width:900px) {{ .two-panel {{ grid-template-columns: 1fr !important; }} }}
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
{circuit_breaker_banner}
  <div class="grid">

    <!-- Balance card -->
    <div class="card">
      <h2>Balance</h2>
      <div class="big">&euro;{balance}</div>
      <div class="sub">Started: &euro;{initial} &nbsp; P&amp;L: <span style="color:{pnl_colour}">{pnl_sign}&euro;{pnl}</span></div>
      {realized_html}
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
      <div class="sub" style="margin-top:6px">Target: &gt;3.0 (success) &nbsp;&#x2022;&nbsp; Exit: &lt;1.0 (failure) &nbsp;&#x2022;&nbsp; Kelly: {kelly_pct}% (half-Kelly sizing ×{kelly_mult})</div>
    </div>

    <!-- Signals card -->
    <div class="card">
      <h2>Pair Signals &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">AI: {intel_score:+.1f}</span> &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">Regime: {regime_label}</span> &nbsp; <span class="badge" style="background:#00c85122;color:#00c851">TP: {dynamic_tp}%</span> &nbsp; <span class="badge" style="background:#ff444422;color:#ff4444">SL: {dynamic_sl}%</span> &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">Correlated open: {corr_open}</span> &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">Short: {btc_mode}</span></h2>
      <table>
        <tr><th>Pair</th><th>Signal</th><th>Score</th></tr>
        {signal_rows}
      </table>
    </div>

    <!-- Ichimoku + Gaussian card -->
    <div class="card">
      <h2>Ichimoku + Gaussian &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">20/30/60 &bull; 1h candles</span></h2>
      {ichi_html}
    </div>

    <!-- Open positions card -->
    <div class="card">
      <h2>Open Positions</h2>
      {positions_html}
    </div>

    <!-- Scalper open positions card -->
    <div class="card">
      <h2>Scalper Positions &nbsp; {scalper_stats}</h2>
      {scalper_positions_html}
    </div>

    <!-- Monthly return card -->
    <div class="card full">
      <h2>Monthly Return Target &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">Goal: +3% to +8% per month</span></h2>
      {monthly_html}
    </div>

  </div><!-- end top grid -->

  <!-- ── 2-panel layout: analysis left, news right ─────────────────────── -->
  <div style="display:grid;grid-template-columns:3fr 2fr;gap:12px;margin-bottom:12px;align-items:stretch">

    <!-- LEFT PANEL: analysis cards stacked -->
    <div style="display:flex;flex-direction:column;gap:12px">

      <div class="card">
        <h2>Social Sentiment &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">Reddit · CoinGecko — activity leads price 1-6h</span></h2>
        {lunar_html}
      </div>

      <div class="card">
        <h2>On-Chain Data &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">Blockchain.info · CoinMetrics · Alchemy</span></h2>
        {onchain_html}
      </div>

      <div class="card">
        <h2>New Listings Monitor &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">15 min wait → buy if +0.8% → smart exit</span></h2>
        {listings_html}
      </div>

      <div class="card">
        <h2>Scientific Method Optimizer &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">Sharpe &ge;3.0 success | &lt;1.0 failure</span></h2>
        {optimizer_html}
      </div>

    </div><!-- end left panel -->

    <!-- RIGHT PANEL: Kraken news feed -->
    <div class="card" style="display:flex;flex-direction:column;overflow:hidden">
      <h2 style="flex-shrink:0">Kraken News &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">blog.kraken.com</span></h2>
      <div style="overflow-y:auto;flex:1">{kraken_news_html}</div>
    </div>

  </div><!-- end 2-panel -->

  <div class="grid">

    <!-- Alpaca card -->
    <div class="card full">
      <h2>Alpaca Correlated Stocks &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">MSTR · COIN · MARA — mirrors BTC/ETH signals</span></h2>
      {alpaca_html}
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

    <!-- Scalper card -->
    <div class="card full">
      <h2>Scalper &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">BTC/ETH &bull; 30s loop &bull; TP 0.7% / SL 0.35%</span> &nbsp; {scalper_stats}</h2>
      {scalper_html}
    </div>

    <!-- Scalper AI Tuner card -->
    <div class="card full">
      <h2>Scalper AI Tuner &nbsp; <span class="badge" style="background:#21262d;color:#8b949e">free OpenRouter models &bull; tunes every 25 trades</span> &nbsp; {scalper_ai_badge}</h2>
      {scalper_ai_html}
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

  <div class="card full" style="margin-top:12px;background:#0d1117;border-color:#21262d">
    <div style="display:flex;flex-wrap:wrap;gap:24px;font-size:12px;color:#8b949e">
      <div><span style="color:#58a6ff">Sharpe</span> &nbsp; success &ge;3.0 &nbsp;&#x2022;&nbsp; failure &lt;1.0 &nbsp;&#x2022;&nbsp; optimizer every 10 closed trades</div>
      <div><span style="color:#58a6ff">AI Panel</span> &nbsp; 5 models via OpenRouter &nbsp;&#x2022;&nbsp; Hermes &nbsp;&#x2022;&nbsp; Sonar &nbsp;&#x2022;&nbsp; DeepSeek &nbsp;&#x2022;&nbsp; Llama &nbsp;&#x2022;&nbsp; GPT-4o-mini</div>
      <div><span style="color:#58a6ff">Data</span> &nbsp; Sharpe.ai &nbsp;&#x2022;&nbsp; Alchemy &nbsp;&#x2022;&nbsp; CoinMetrics &nbsp;&#x2022;&nbsp; CoinGecko &nbsp;&#x2022;&nbsp; Blockchain.info &nbsp;&#x2022;&nbsp; Kraken RSS</div>
      <div><span style="color:#58a6ff">Exchanges</span> &nbsp; Kraken (7 pairs) &nbsp;&#x2022;&nbsp; Alpaca (MSTR &nbsp;&#x2022;&nbsp; COIN &nbsp;&#x2022;&nbsp; MARA)</div>
      <div><span style="color:#58a6ff">History</span> &nbsp; {db_summary}</div>
    </div>
  </div>
</body>
</html>"""


def _build_page() -> str:
    status = _read_json("bot_status.json")
    trades = _read_jsonl_tail("trade_events.jsonl", n=15)

    # ── Balance ───────────────────────────────────────────────────────────────
    # Use portfolio_value (cash + open positions) for accurate display
    balance      = status.get("portfolio_value", status.get("balance_eur", 0.0))
    initial      = status.get("initial_balance", 100.0)
    pnl          = status.get("adjusted_pnl", 0.0)
    pnl_colour   = "#00c851" if pnl >= 0 else "#ff4444"
    pnl_sign     = "+" if pnl >= 0 else ""
    paper_mode   = status.get("paper_mode", True)
    realized_pnl = _calc_realized_pnl(paper_mode)
    unrealized   = pnl - realized_pnl
    _rc          = "#00c851" if realized_pnl >= 0 else "#ff4444"
    _uc          = "#00c851" if unrealized >= 0 else "#ff4444"
    realized_html = (
        f'<div class="sub" style="margin-top:4px;font-size:11px">'
        f'Realized: <span style="color:{_rc}">{("+" if realized_pnl>=0 else "")}'
        f'&euro;{abs(realized_pnl):.4f}</span>'
        f'&nbsp;&nbsp;&#x2502;&nbsp;&nbsp;'
        f'Unrealized: <span style="color:{_uc}">{("+" if unrealized>=0 else "")}'
        f'&euro;{abs(unrealized):.4f}</span>'
        f'</div>'
    )

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

    # ── Social Sentiment (CoinGecko trending + Fear & Greed) ──────────────────
    lc = status.get("lunarcrush", {})
    if lc:
        combined     = lc.get("combined", 0)
        c_col        = "#00c851" if combined >= 1 else ("#ff4444" if combined <= -1 else "#ffbb33")
        fg_val       = lc.get("fear_greed")
        fg_str       = f"{fg_val}/100" if fg_val is not None else "n/a"
        fg_col       = "#00c851" if fg_val and fg_val <= 30 else ("#ff4444" if fg_val and fg_val >= 70 else "#ffbb33")
        trending_now = lc.get("trending_now", [])
        lc_rows      = ""
        for pair, cd in lc.get("coins", {}).items():
            sig      = cd.get("signal", 0)
            ch24     = cd.get("change_24h", 0)
            is_trend = cd.get("is_trending", False)
            reddit   = cd.get("reddit_count", 0)
            sig_col  = "#00c851" if sig >= 1 else ("#ff4444" if sig <= -1 else "#8b949e")
            ch_col   = "#00c851" if ch24 >= 0 else "#ff4444"
            tr_col   = "#00c851" if ch24 >= 0 else "#ff4444"
            if is_trend:
                ch_arrow = "&#x25B2;" if ch24 >= 0 else "&#x25BC;"
                tr_badge = f' <span style="color:{tr_col};font-size:10px;font-weight:bold">TRENDING</span>'
            else:
                ch_arrow = "&gt;"
                tr_col   = "#8b949e"
                tr_badge = ""
            reddit_col  = "#58a6ff" if reddit >= 10 else ("#8b949e" if reddit >= 3 else "#30363d")
            reddit_html = f'<span style="color:{reddit_col}">{reddit}</span>'
            lc_rows += (
                f'<tr><td><span style="color:{tr_col}">{ch_arrow}</span> {cd.get("symbol","")}{tr_badge}</td>'
                f'<td style="color:{ch_col}">{ch24:+.1f}%</td>'
                f'<td>{reddit_html}</td>'
                f'<td style="color:{sig_col};font-weight:bold">{sig:+.1f}</td></tr>'
            )
        trending_str = (
            f'<div style="margin-top:8px;font-size:11px;color:#8b949e">'
            f'Trending now: {", ".join(trending_now[:10])}</div>'
        ) if trending_now else ""
        lunar_html = (
            f'<div style="display:flex;gap:20px;margin-bottom:10px;flex-wrap:wrap">'
            f'<div>Social signal: <span style="color:{c_col};font-size:20px;font-weight:bold">{combined:+.2f}</span></div>'
            f'<div>Fear &amp; Greed: <span style="color:{fg_col};font-weight:bold">{fg_str}</span></div>'
            f'</div>'
            f'<table><tr><th>Coin</th><th>24h</th><th>Reddit</th><th>Signal</th></tr>'
            + lc_rows + f'</table>{trending_str}'
        )
    else:
        lunar_html = '<div class="grey" style="padding:8px 0">Social sentiment loading — CoinGecko trending + Fear &amp; Greed (free, no API key).</div>'

    # ── On-chain ──────────────────────────────────────────────────────────────
    oc = status.get("onchain", {})
    if oc:
        combined = oc.get("combined", 0)
        c_col    = "#00c851" if combined >= 1 else ("#ff4444" if combined <= -1 else "#ffbb33")
        rows = [
            ("BTC Transactions (24h)", f'{oc.get("btc_tx_24h", 0):,}', "#e6edf3"),
            ("BTC Mempool Size",       f'{oc.get("btc_mempool", 0):,} txs', "#e6edf3"),
            ("BTC Network Signal",     f'{oc.get("btc_score", 0):+.1f}',
             "#00c851" if oc.get("btc_score", 0) >= 0 else "#ff4444"),
            ("ETH Gas (fast)",         f'{oc.get("eth_gas", 0):.2f} gwei', "#e6edf3"),
            ("ETH Gas Signal",         f'{oc.get("eth_signal", 0):+.1f}',
             "#00c851" if oc.get("eth_signal", 0) >= 0 else "#ff4444"),
        ]
        if oc.get("btc_flow_signal") is not None:
            rows.append(("BTC Exchange Flow Signal",
                         f'{oc["btc_flow_signal"]:+.1f} (Glassnode)',
                         "#00c851" if oc["btc_flow_signal"] >= 0 else "#ff4444"))
        tbl = "".join(
            f'<tr><td class="grey">{r[0]}</td>'
            f'<td style="color:{r[2]};font-weight:bold">{r[1]}</td></tr>'
            for r in rows
        )
        onchain_html = (
            f'<div style="margin-bottom:10px">Combined on-chain signal: '
            f'<span style="color:{c_col};font-size:20px;font-weight:bold">{combined:+.2f}</span>'
            f'<span class="grey" style="margin-left:8px;font-size:11px">'
            f'positive=bullish network activity | negative=bearish</span></div>'
            f'<table>{tbl}</table>'
            f'<div class="grey" style="font-size:11px;margin-top:6px">'
            f'Exchange flows via CoinMetrics (free) · Add ETHERSCAN_API_KEY for ETH gas data</div>'
        )
    else:
        onchain_html = '<div class="grey" style="padding:8px 0">On-chain data loading — updates every 5 loops.</div>'

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
        alpaca_html = '<div class="grey" style="padding:8px 0">Alpaca not configured — set ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_BASE_URL in .env to enable.</div>'

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
            bt_sharpe    = exp.get("backtest_sharpe")
            helped       = exp.get("pairs_helped", [])
            hurt         = exp.get("pairs_hurt", [])
            bt_detail    = ""
            if bt_sharpe:
                bt_detail = f" | backtest: {bt_sharpe:.3f}"
                if helped: bt_detail += f" | helped: {', '.join(helped)}"
                if hurt:   bt_detail += f" | hurt: {', '.join(hurt)}"
            bt_str = bt_detail
            opt_lines.append(
                f'<div style="margin-bottom:10px;padding:10px;background:#161b22;border:1px solid #30363d;border-radius:6px">'
                f'<span style="color:#ffbb33;font-weight:bold">ACTIVE EXPERIMENT</span> &nbsp; '
                f'<code>{exp["param"]}</code>: '
                f'<span class="grey">{exp["old_value"]}</span> '
                f'&rarr; <span style="color:#58a6ff;font-weight:bold">{exp["new_value"]}</span> '
                f'({exp.get("direction","?")})'
                f'<span class="grey" style="margin-left:12px;font-size:11px">'
                f'Sharpe at start: {exp.get("sharpe_at_start") or "—"}{bt_str}'
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
            f_col  = "#ff4444" if f_score < -1 else ("#00c851" if f_score > 1 else "#ffbb33")
            bar_w  = int(abs(f_score) / 5.0 * 100)
            f_str  = f'<span style="color:{f_col};font-weight:bold">{f_score:+.1f}</span>'
            bias   = "bearish (longs crowded)" if f_score < -1 else ("bullish (shorts crowded)" if f_score > 1 else "neutral")
            bar    = (f'<div style="background:#21262d;border-radius:3px;height:4px;margin-top:3px">'
                      f'<div style="background:{f_col};width:{bar_w}%;height:4px;border-radius:3px"></div></div>')
            sharpe_rows += (
                f'<tr><td><b>{coin}</b></td>'
                f'<td>{f_str}{bar}</td>'
                f'<td class="grey" style="font-size:11px">{bias}</td></tr>'
            )
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
            '<th>Coin</th>'
            '<th>Funding Signal</th>'
            '<th>Market Bias</th>'
            '</tr>'
            + sharpe_rows + '</table>' + ins_str
        )
    else:
        sharpe_html = '<div class="grey" style="padding:8px 0">Sharpe.ai derivatives data loading — refreshes every 10 minutes.</div>'

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
        tp_price = info.get("tp_price", 0)
        sl_price = info.get("sl_price", 0)
        tp_pct   = info.get("tp_pct", 0)
        sl_pct   = info.get("sl_pct", 0)
        tp_str   = f'&euro;{tp_price:,.4f} (+{tp_pct:.2f}%)' if tp_price else '—'
        sl_str   = f'&euro;{sl_price:,.4f} (-{sl_pct:.2f}%)' if sl_price else '—'
        pos_rows += (
            f'<tr><td>{pair}</td><td class="green">LONG</td>'
            f'<td>{info["qty"]}</td>'
            f'<td>&euro;{info["entry"]}</td>'
            f'<td>&euro;{cur:,.4f}</td>'
            f'<td style="color:{pnl_col}">{pnl_pct:+.2f}% ({pnl_eur:+.4f})</td>'
            f'<td style="color:#00c851;font-size:11px">{tp_str}</td>'
            f'<td style="color:#ff4444;font-size:11px">{sl_str}</td>'
            f'<td><button onclick="manualSell(\'{pair}\')" '
            f'style="background:#ff4444;color:#fff;border:none;padding:4px 10px;'
            f'border-radius:4px;cursor:pointer;font-size:11px">Sell</button></td></tr>'
        )
    for pair, info in shorts.items():
        _s_pnl_pct = info.get("pnl_pct", 0.0)
        _s_pnl_eur = info.get("pnl_eur", 0.0)
        _s_pnl_col = "#00c851" if _s_pnl_pct >= 0 else "#ff4444"
        _s_cur     = info.get("current", 0)
        _s_tp_price = info.get("tp_price", 0)
        _s_sl_price = info.get("sl_price", 0)
        _s_tp_pct   = info.get("tp_pct", 0)
        _s_sl_pct   = info.get("sl_pct", 0)
        _s_tp_str   = f'&euro;{_s_tp_price:,.4f} (+{_s_tp_pct:.2f}%)' if _s_tp_price else '&mdash;'
        _s_sl_str   = f'&euro;{_s_sl_price:,.4f} (-{_s_sl_pct:.2f}%)' if _s_sl_price else '&mdash;'
        pos_rows += (
            f'<tr><td>{pair}</td><td class="red">SHORT</td>'
            f'<td>{info["qty"]}</td>'
            f'<td>&euro;{info["entry"]}</td>'
            f'<td>&euro;{_s_cur:,.4f}</td>'
            f'<td style="color:{_s_pnl_col}">{_s_pnl_pct:+.2f}% ({_s_pnl_eur:+.4f})</td>'
            f'<td style="color:#00c851;font-size:11px">{_s_tp_str}</td>'
            f'<td style="color:#ff4444;font-size:11px">{_s_sl_str}</td>'
            f'<td></td></tr>'
        )
    if pos_rows:
        positions_html = (
            '<table><tr><th>Pair</th><th>Side</th><th>Qty</th><th>Entry</th>'
            '<th>Current</th><th>P&amp;L</th>'
            '<th style="color:#00c851">TP Target</th>'
            '<th style="color:#ff4444">SL Target</th>'
            '<th></th></tr>'
            + pos_rows + '</table>'
            '<script>'
            'function manualSell(pair) {'
            '  if (confirm("Sell " + pair + "?\\n\\nAre you sure? This will market-sell the position immediately.")) {'
            '    if (confirm("Second confirmation: close " + pair + " at market price NOW?")) {'
            '      window.location.href = "/manual-sell/" + pair;'
            '    }'
            '  }'
            '}'
            '</script>'
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

    # ── Scalper card ──────────────────────────────────────────────────────────
    scalper_data = status.get("scalper", {})
    if scalper_data:
        _sc_total   = scalper_data.get("total_trades", 0)
        _sc_wins    = scalper_data.get("wins", 0)
        _sc_losses  = scalper_data.get("losses", 0)
        _sc_wr      = scalper_data.get("win_rate", 0)
        _sc_pnl     = scalper_data.get("total_pnl_eur", 0)
        _sc_pnl_c   = "#00c851" if _sc_pnl >= 0 else "#ff4444"
        _sc_vol     = scalper_data.get("volume_usd", 0)
        _sc_fee     = scalper_data.get("taker_fee_pct", 0.26)
        _sc_dtp     = scalper_data.get("dynamic_tp_pct", 0.58)
        _sc_wl_c    = "#00c851" if _sc_wins >= _sc_losses else "#ff4444"
        scalper_stats = (
            f'<span class="badge" style="background:#21262d;color:#8b949e">{_sc_total} trades</span> &nbsp;'
            f'<span class="badge" style="background:{_sc_wl_c}22;color:{_sc_wl_c}">'
            f'W {_sc_wins} / L {_sc_losses} &bull; {_sc_wr:.0f}%</span> &nbsp;'
            f'<span class="badge" style="background:{_sc_pnl_c}22;color:{_sc_pnl_c}">P&amp;L {_sc_pnl:+.4f} EUR</span> &nbsp;'
            f'<span class="badge" style="background:#21262d;color:#8b949e">Vol ${_sc_vol:,.0f}</span> &nbsp;'
            f'<span class="badge" style="background:#21262d;color:#58a6ff">Fee {_sc_fee:.2f}% &bull; TP {_sc_dtp:.2f}%</span>'
        )
        # Open positions
        _sc_pos = scalper_data.get("positions", {})
        _pos_rows = ""
        for _sp, _sv in _sc_pos.items():
            _held = round((time.time() - _sv.get("ts", time.time())) / 60, 1)
            _pos_rows += (
                f'<tr><td>{_sp}</td><td>{_sv.get("entry", 0):.6f}</td>'
                f'<td>{_sv.get("qty", 0):.8f}</td><td>{_held}m</td>'
                f'<td><span style="color:#58a6ff">score {_sv.get("score", 0):.1f}</span></td></tr>'
            )
        # Recent scalp trades
        _sc_trades = scalper_data.get("recent_trades", [])[-10:]
        _trade_rows = ""
        for _t in reversed(_sc_trades):
            _tc = "#00c851" if _t.get("pnl_eur", 0) >= 0 else "#ff4444"
            _trade_rows += (
                f'<tr><td>{_t.get("ts","")[:19]}</td><td>{_t.get("pair","")}</td>'
                f'<td>{_t.get("entry",0):.6f}</td><td>{_t.get("exit",0):.6f}</td>'
                f'<td style="color:{_tc}">{_t.get("pnl_eur",0):+.4f}</td>'
                f'<td style="color:{_tc}">{_t.get("pnl_pct",0):+.3f}%</td>'
                f'<td>{_t.get("reason","")}</td><td>{_t.get("held_min",0):.1f}m</td></tr>'
            )
        # Compact positions-only html for the top card
        if _pos_rows:
            scalper_positions_html = (
                '<table><tr><th>Pair</th><th>Entry</th><th>Qty</th><th>Held</th><th>Score</th></tr>'
                + _pos_rows + '</table>'
            )
        else:
            scalper_positions_html = '<div class="grey" style="padding:8px 0">No open scalp positions</div>'

        # Pair trend grid
        _pair_scores = scalper_data.get("pair_scores", {})
        _trend_cells = ""
        for _tp in [
            "XBTEUR","XETHZEUR","SOLEUR","XXRPZEUR","LINKEUR","AVAXEUR",
            "ADAEUR","DOTEUR","ATOMEUR","UNIEUR",
            "LTCEUR","BCHEUR","TRXEUR","XMREUR","AAVEEUR","NEAREUR",
            "ALGOEUR","ETCEUR","SHIBEUR","ZECEUR",
            "MKREUR","SNXEUR","OPEUR","ARBEUR","SANDEUR",
            "MANAUER","INJEUR","FTMEUR","GALEUR","APEEUR",
        ]:
            _label = {
                "XBTEUR":"BTC","XETHZEUR":"ETH","XXRPZEUR":"XRP","XMREUR":"XMR",
                "SOLEUR":"SOL","LINKEUR":"LINK","AVAXEUR":"AVAX","ADAEUR":"ADA",
                "DOTEUR":"DOT","ATOMEUR":"ATOM","UNIEUR":"UNI","LTCEUR":"LTC",
                "BCHEUR":"BCH","TRXEUR":"TRX","AAVEEUR":"AAVE","NEAREUR":"NEAR",
                "ALGOEUR":"ALGO","ETCEUR":"ETC","SHIBEUR":"SHIB","ZECEUR":"ZEC",
                "MKREUR":"MKR","SNXEUR":"SNX","OPEUR":"OP","ARBEUR":"ARB",
                "SANDEUR":"SAND","MANAUER":"MANA","INJEUR":"INJ","FTMEUR":"FTM",
                "GALEUR":"GAL","APEEUR":"APE",
            }.get(_tp, _tp)
            _sc = _pair_scores.get(_tp)
            if _sc is None:
                _arrow, _col = "·", "#8b949e"
            elif _sc >= 1.5:
                _arrow, _col = "▲", "#00c851"
            elif _sc <= -1.5:
                _arrow, _col = "▼", "#ff4444"
            elif _sc > 0:
                _arrow, _col = "↑", "#4caf50"
            elif _sc < 0:
                _arrow, _col = "↓", "#f44336"
            else:
                _arrow, _col = "–", "#8b949e"
            _in_pos = "★ " if _tp in _sc_pos else ""
            _trend_cells += (
                f'<span style="display:inline-block;min-width:70px;margin:2px 4px;font-size:11px">'
                f'<span style="color:{_col}">{_arrow}</span> '
                f'<span style="color:#e6edf3">{_in_pos}{_label}</span>'
                f'</span>'
            )
        _trend_grid = (
            '<b style="color:#8b949e;font-size:11px">PAIR TRENDS</b><br>'
            f'<div style="padding:6px 0 10px 0;line-height:2">{_trend_cells}</div>'
        ) if _trend_cells else ""

        scalper_html = _trend_grid
        if _pos_rows:
            scalper_html += (
                '<b style="color:#8b949e;font-size:11px">OPEN POSITIONS</b>'
                '<table><tr><th>Pair</th><th>Entry</th><th>Qty</th><th>Held</th><th>Signal</th></tr>'
                + _pos_rows + '</table><br>'
            )
        if _trade_rows:
            scalper_html += (
                '<b style="color:#8b949e;font-size:11px">RECENT SCALP TRADES</b>'
                '<table><tr><th>Time</th><th>Pair</th><th>Entry</th><th>Exit</th>'
                '<th>P&amp;L EUR</th><th>P&amp;L %</th><th>Reason</th><th>Held</th></tr>'
                + _trade_rows + '</table>'
            )
        if not scalper_html:
            scalper_html = '<div class="grey" style="padding:8px 0">No scalp trades yet — waiting for signal score &ge;1.5</div>'
    else:
        scalper_stats          = '<span class="badge" style="background:#21262d;color:#8b949e">not running</span>'
        scalper_positions_html = '<div class="grey" style="padding:8px 0">Scalper not active</div>'
        scalper_html           = '<div class="grey" style="padding:8px 0">Scalper engine not active (paper mode only)</div>'

    # ── Scalper AI Tuner ──────────────────────────────────────────────────────
    ai_adjustments = _read_jsonl_tail("scalper_ai_adjustments.jsonl", n=10)
    ai_params_raw  = _read_json("scalper_ai_params.json")
    _PARAM_LABELS  = {
        "rsi_buy":      "RSI Buy",
        "rsi_sell":     "RSI Sell",
        "vwap_thresh":  "VWAP Thresh",
        "score_thresh": "Score Thresh",
        "sl_pct":       "Stop Loss %",
    }
    _PARAM_DEFAULTS = {
        "rsi_buy": 35.0, "rsi_sell": 65.0, "vwap_thresh": 0.003,
        "score_thresh": 1.5, "sl_pct": 0.20,
    }

    if ai_adjustments:
        latest = ai_adjustments[-1]
        wr     = latest.get("win_rate", 0)
        wr_col = "#00c851" if wr >= 50 else ("#ffbb33" if wr >= 40 else "#ff4444")
        scalper_ai_badge = (
            f'<span class="badge" style="background:{wr_col}22;color:{wr_col}">'
            f'last WR {wr:.0f}%</span>'
        )

        # Current params vs defaults diff
        param_cells = ""
        for key, label in _PARAM_LABELS.items():
            default = _PARAM_DEFAULTS[key]
            current = float(ai_params_raw.get(key, default)) if ai_params_raw else default
            changed = abs(current - default) > 1e-4
            col     = "#58a6ff" if changed else "#8b949e"
            arrow   = f'<span style="color:#8b949e;font-size:10px"> (def {default})</span>'
            param_cells += (
                f'<td style="color:{col};font-weight:{"bold" if changed else "normal"}'
                f';font-size:12px">{label}<br>'
                f'<span style="font-size:14px">{current}</span>{arrow}</td>'
            )
        blacklist = ai_params_raw.get("pairs_blacklist", []) if ai_params_raw else []
        bl_html = ""
        if blacklist:
            bl_items = "".join(
                f'<span style="background:#ff444422;color:#ff4444;border-radius:4px;'
                f'padding:1px 6px;margin:2px;font-size:11px">{p}</span>'
                for p in blacklist
            )
            bl_html = (
                f'<div style="margin-top:8px;margin-bottom:4px">'
                f'<span style="color:#8b949e;font-size:11px">BLACKLISTED: </span>{bl_items}</div>'
            )

        updated_at = ai_params_raw.get("updated_at", "") if ai_params_raw else ""
        age_str    = _age(updated_at) if updated_at else "never"
        params_bar = (
            f'<div style="margin-bottom:12px">'
            f'<div style="color:#8b949e;font-size:11px;margin-bottom:6px">'
            f'LIVE PARAMS (last updated {age_str})</div>'
            f'<table style="width:100%"><tr>{param_cells}</tr></table>'
            f'{bl_html}</div>'
        )

        # Adjustment history log
        adj_rows = ""
        for adj in reversed(ai_adjustments):
            adj_ts      = adj.get("ts", "")[:16].replace("T", " ")
            adj_wr      = adj.get("win_rate", 0)
            adj_wr_col  = "#00c851" if adj_wr >= 50 else ("#ffbb33" if adj_wr >= 40 else "#ff4444")
            adj_n       = adj.get("trades_analyzed", 0)
            changes     = adj.get("changes", [])
            reasoning   = adj.get("reasoning", "")
            pair_stats  = adj.get("pair_stats", {})

            if not changes:
                change_html = '<span style="color:#8b949e;font-size:11px">no changes</span>'
            else:
                parts = []
                for c in changes:
                    param = c.get("param", "")
                    old   = c.get("old")
                    new   = c.get("new")
                    if param == "pairs_blacklist":
                        for pair in (new or []):
                            ps  = pair_stats.get(pair, {})
                            tot = ps.get("w", 0) + ps.get("l", 0)
                            wr2 = round(ps["w"] / tot * 100) if tot else 0
                            parts.append(
                                f'<span style="color:#ff4444">{pair} {wr2}% WR → blacklisted</span>'
                            )
                    else:
                        label = _PARAM_LABELS.get(param, param)
                        col   = "#58a6ff"
                        parts.append(
                            f'<span style="color:{col}">{label} '
                            f'<span style="color:#8b949e">{old}</span> → '
                            f'<b>{new}</b></span>'
                        )
                change_html = ' &nbsp;|&nbsp; '.join(parts)

            # Worst pairs this round
            worst = sorted(
                [(p, s) for p, s in pair_stats.items()
                 if s.get("w", 0) + s.get("l", 0) >= 3],
                key=lambda x: x[1]["w"] / (x[1]["w"] + x[1]["l"])
            )[:3]
            worst_html = ""
            if worst:
                worst_parts = []
                for p, s in worst:
                    tot = s["w"] + s["l"]
                    wr2 = round(s["w"] / tot * 100) if tot else 0
                    wc  = "#ff4444" if wr2 < 40 else "#ffbb33"
                    worst_parts.append(f'<span style="color:{wc}">{p} {wr2}%</span>')
                worst_html = (
                    f'<div style="font-size:10px;color:#8b949e;margin-top:2px">'
                    f'worst pairs: {" · ".join(worst_parts)}</div>'
                )

            adj_rows += (
                f'<tr style="border-bottom:1px solid #21262d">'
                f'<td style="color:#8b949e;font-size:11px;white-space:nowrap;vertical-align:top">'
                f'{adj_ts}<br>{adj_n} trades</td>'
                f'<td style="color:{adj_wr_col};font-weight:bold;vertical-align:top">{adj_wr:.0f}%</td>'
                f'<td style="vertical-align:top">{change_html}{worst_html}</td>'
                f'<td style="color:#8b949e;font-size:11px;vertical-align:top">{reasoning[:120]}</td>'
                f'</tr>'
            )

        scalper_ai_html = (
            params_bar
            + '<b style="color:#8b949e;font-size:11px">ADJUSTMENT HISTORY</b>'
            + '<table style="width:100%;margin-top:6px">'
            + '<tr><th>Time</th><th>WR</th><th>Changes</th><th>AI Reasoning</th></tr>'
            + adj_rows + '</table>'
        )
    else:
        scalper_ai_badge = '<span class="badge" style="background:#21262d;color:#8b949e">waiting for 25 trades</span>'
        scalper_ai_html  = (
            '<div class="grey" style="padding:8px 0">'
            'AI tuner will activate after 25 scalp trades — it will analyze RSI, VWAP deviation and '
            'order book imbalance at entry to find which signal combinations win vs lose, '
            'then adjust thresholds within safe bounds automatically.'
            '</div>'
        )

    # ── DB stats ──────────────────────────────────────────────────────────────
    db_stats = status.get("db_stats", {})
    if db_stats:
        oldest = (db_stats.get("oldest_trade") or "")[:10] or "today"
        db_summary = (
            f"History DB: {db_stats.get('trades', 0)} trades | "
            f"{db_stats.get('ai_panels', 0)} AI panels | "
            f"{db_stats.get('sharpe_snapshots', 0)} Sharpe snapshots | "
            f"since {oldest}"
        )
    else:
        db_summary = "History DB: initialising…"

    # ── Ichimoku + Gaussian card ──────────────────────────────────────────────
    ichi_data = status.get("ichi", {})
    if ichi_data:
        _ichi_rows = ""
        for _pair, _sig in ichi_data.items():
            _vs  = _sig.get("price_vs_cloud", "unknown")
            _trend = _sig.get("trend", "unknown")
            _gauss = _sig.get("gaussian_buy", False)
            _boost = _sig.get("score_boost", 0)
            _cloud_top = _sig.get("cloud_top", 0)
            _cloud_bot = _sig.get("cloud_bottom", 0)
            if _vs == "above":
                _vs_colour = "#00c851"
                _vs_label  = "ABOVE"
            elif _vs == "below":
                _vs_colour = "#ff4444"
                _vs_label  = "BELOW"
            elif _vs == "inside":
                _vs_colour = "#f0a500"
                _vs_label  = "INSIDE"
            else:
                _vs_colour = "#8b949e"
                _vs_label  = "—"
            _gauss_html = ('<span style="color:#00c851">&#x25CF; BUY</span>'
                           if _gauss else '<span style="color:#8b949e">—</span>')
            _boost_html = (f'<span style="color:#58a6ff">+{_boost:.1f}</span>'
                           if _boost > 0 else "")
            _ichi_rows += (
                f'<tr><td>{_pair}</td>'
                f'<td><span style="color:{_vs_colour};font-weight:bold">{_vs_label}</span></td>'
                f'<td style="color:#8b949e">{_trend}</td>'
                f'<td style="color:#8b949e">{_cloud_bot:.2f} – {_cloud_top:.2f}</td>'
                f'<td>{_gauss_html}</td>'
                f'<td>{_boost_html}</td></tr>'
            )
        ichi_html = (
            '<table><tr><th>Pair</th><th>Cloud</th><th>Trend</th>'
            '<th>Cloud range</th><th>Gaussian</th><th>Boost</th></tr>'
            + _ichi_rows + '</table>'
        )
    else:
        ichi_html = '<div class="grey" style="padding:8px 0">Ichimoku data not yet available — appears after first 1h candle fetch</div>'

    # ── Circuit breaker banner ────────────────────────────────────────────────
    if status.get("circuit_breaker", False):
        _peak = status.get("peak_balance", 0)
        _bal  = status.get("portfolio_value", status.get("balance_eur", 0))
        _dd   = round((_peak - _bal) / _peak * 100, 1) if _peak > 0 else 0
        circuit_breaker_banner = (
            f'  <div style="background:#ff000022;border:1px solid #ff4444;border-radius:8px;'
            f'padding:12px 16px;margin-bottom:12px;color:#ff4444;font-weight:bold;">'
            f'&#x26A0; CIRCUIT BREAKER ACTIVE — Drawdown {_dd}% exceeded limit. '
            f'All positions closed. Buying paused 24h. Peak: &euro;{_peak:,.2f}'
            f'</div>'
        )
    else:
        circuit_breaker_banner = ""

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
        realized_html = realized_html,
        trade_count   = status.get("trade_count", 0),
        regime        = status.get("regime", "—"),
        sharpe        = sharpe_str,
        sharpe_colour = _verdict_colour(verdict),
        verdict       = verdict.replace("_", " "),
        arrow         = _trending_arrow(trending),
        trending      = trending.replace("_", " "),
        kelly_pct     = round((status.get("kelly_fraction", 0.1)) * 100, 1),
        kelly_mult    = status.get("kelly_multiplier", 1.0),
        intel_score   = status.get("intelligence_score", 0.0),
        regime_label  = status.get("regime_strategy", "RANGING"),
        dynamic_tp    = status.get("dynamic_tp_pct", 2.0),
        dynamic_sl    = status.get("dynamic_sl_pct", 0.8),
        corr_open     = status.get("correlated_open", 0),
        btc_mode      = btc_mode,
        signal_rows   = signal_rows,
        monthly_html  = monthly_html,
        lunar_html       = lunar_html,
        onchain_html     = onchain_html,
        alpaca_html      = alpaca_html,
        kraken_news_html = kraken_news_html,
        optimizer_html = optimizer_html,
        listings_html = listings_html,
        sharpe_html   = sharpe_html,
        model_html    = model_html,
        positions_html= positions_html,
        trades_html    = trades_html,
        intel_log_html          = intel_log_html,
        db_summary              = db_summary,
        circuit_breaker_banner  = circuit_breaker_banner,
        scalper_stats           = scalper_stats,
        scalper_html            = scalper_html,
        scalper_positions_html  = scalper_positions_html,
        ichi_html               = ichi_html,
        scalper_ai_badge        = scalper_ai_badge,
        scalper_ai_html         = scalper_ai_html,
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
                    "<p>Restart the bot service to reload with a clean slate.</p>"
                    "<a href='/' style='color:#58a6ff'>← Back to dashboard</a>"
                    "</body></html>",
                    mimetype="text/html"
                )
            except Exception as e:
                return Response(f"Error: {e}", status=500)

        @app.route("/manual-sell/<pair>")
        def manual_sell(pair):
            """Sell a single specific pair — triggered from the Open Positions button."""
            try:
                safe_pair = pair.upper().replace("/", "").replace("..", "")
                path = os.path.join(DATA_DIR, f"FORCE_SELL_{safe_pair}")
                open(path, "w").close()
                return Response(
                    "<html><body style='background:#0d1117;color:#ff4444;font-family:monospace;padding:40px'>"
                    f"<h2>&#x2705; Manual SELL triggered for {safe_pair}</h2>"
                    "<p>The bot will close this position on the next loop (within 60 seconds).</p>"
                    "<p>Watch your Telegram for the SELL notification.</p>"
                    "<a href='/' style='color:#58a6ff'>&#x2190; Back to dashboard</a>"
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

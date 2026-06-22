"""
Market Intelligence Module — Multi-Model Panel
================================================
Runs a panel of specialist AI models in parallel via OpenRouter (+ OpenAI).
Each model has a distinct role so they don't just repeat each other.
Their scores are aggregated into a single `intelligence_score` [-5, +5]
that adjusts the bot's buy gate each loop.

Models
------
  hermes    nousresearch/hermes-3-llama-3.1-70b     Strategy & positioning
  sonar     perplexity/llama-3.1-sonar-small-128k-online  Live web search (real news)
  deepseek  deepseek/deepseek-r1-distill-llama-70b  Technical pattern reasoning
  mistral   mistralai/mistral-7b-instruct            Fast sentiment cross-check
  gpt       gpt-4o-mini (OpenAI)                     General sentiment

Bot performance context is injected into every prompt so models reason
about how *this bot* is actually doing, not just the market in abstract.

Required env vars (set in Railway Variables):
  OPENROUTER_API_KEY   — covers hermes, sonar, deepseek, mistral
  OPENAI_API_KEY       — optional, covers gpt-4o-mini
"""

import json
import os
import re
import time
import logging
import threading
import requests
from typing import Optional

try:
    from core.onchain_data import fetch_all_onchain as _fetch_onchain
    _ONCHAIN_AVAILABLE = True
except ImportError:
    _ONCHAIN_AVAILABLE = False

try:
    from core.sharpe_data import fetch_all as _sharpe_fetch_all
    _SHARPE_AVAILABLE = True
except ImportError:
    try:
        from sharpe_data import fetch_all as _sharpe_fetch_all
        _SHARPE_AVAILABLE = True
    except ImportError:
        _SHARPE_AVAILABLE = False

try:
    from core.history_db import (
        record_ai_panel as _record_ai_panel,
        record_sharpe_snapshot as _record_sharpe_snapshot,
        build_history_context as _build_history_context,
    )
    _HISTORY_DB_AVAILABLE = True
except ImportError:
    try:
        from history_db import (
            record_ai_panel as _record_ai_panel,
            record_sharpe_snapshot as _record_sharpe_snapshot,
            build_history_context as _build_history_context,
        )
        _HISTORY_DB_AVAILABLE = True
    except ImportError:
        _HISTORY_DB_AVAILABLE = False

logger = logging.getLogger(__name__)

_INTEL_LOG = os.path.join(os.path.dirname(__file__), "..", "data", "intelligence_log.jsonl")
_INTEL_LOG_KEEP = 200   # max entries to retain


def _load_recent_intel(n: int = 5) -> list:
    """Return the last N intelligence log entries."""
    entries = []
    try:
        with open(_INTEL_LOG, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.debug("Intel log read failed: %s", exc)
    return entries[-n:]


def _append_intel_log(entry: dict) -> None:
    """Append one result to the intelligence log (plain append, no lock needed — single writer)."""
    try:
        os.makedirs(os.path.dirname(_INTEL_LOG), exist_ok=True)
        with open(_INTEL_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception as exc:
        logger.debug("Intel log write failed: %s", exc)

# ── Model registry ─────────────────────────────────────────────────────────────
# (id, display_name, weight, role, system_prompt)
_OPENROUTER_MODELS = [
    (
        "nousresearch/hermes-3-llama-3.1-70b",
        "hermes",
        1.0,
        "crypto trading strategist",
        (
            "You are Hermes, an expert crypto trading strategist. "
            "A short-term bot (< 2-hour holds, pairs vs EUR) needs your positioning advice. "
            "Consider market context AND the bot's recent performance. "
            "Reply with exactly: 'Score: X. Strategy: <one sentence>.' "
            "where X is an integer -5 (strongly reduce exposure) to +5 (strongly increase exposure)."
        ),
    ),
    (
        "perplexity/sonar",
        "sonar",
        1.5,   # higher weight — has live web access
        "live news analyst",
        (
            "You are a real-time crypto news analyst with live internet access. "
            "Search for the latest breaking news, regulatory updates, and market-moving events "
            "for Bitcoin, Ethereum, Solana, and XRP right now. "
            "Based on what you find, reply with exactly: 'Score: X. News: <one sentence summary>.' "
            "where X is an integer -5 (very negative news) to +5 (very positive news)."
        ),
    ),
    (
        "deepseek/deepseek-r1",
        "deepseek",
        1.0,
        "technical analyst",
        (
            "You are a cryptocurrency technical analyst specialising in short-term price patterns. "
            "Given the market data and bot signal scores provided, reason about likely price direction "
            "for BTC, ETH, SOL, XRP over the next 1-2 hours. "
            "Reply with exactly: 'Score: X. Analysis: <one sentence>.' "
            "where X is an integer -5 (strongly bearish technically) to +5 (strongly bullish technically)."
        ),
    ),
    (
        "meta-llama/llama-3.1-8b-instruct",
        "mistral",
        0.75,
        "sentiment checker",
        (
            "You are a crypto market sentiment checker. "
            "Quickly assess overall market mood from the data provided. "
            "Reply with exactly: 'Score: X. Sentiment: <three words>.' "
            "where X is an integer -5 (extreme fear/bearish) to +5 (extreme greed/bullish)."
        ),
    ),
    (
        "openai/gpt-4o-mini",
        "gpt",
        1.0,
        "general analyst",
        (
            "You are a crypto market analyst. "
            "Given the market context and bot performance data, "
            "reply with exactly: 'Score: X. Reason: <one sentence>.' "
            "where X is an integer -5 (very bearish) to +5 (very bullish)."
        ),
    ),
]

# ── External data endpoints ────────────────────────────────────────────────────
_FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=3"
_COINGECKO_URL  = "https://api.coingecko.com/api/v3/global"
_NEWS_URL = (
    "https://min-api.cryptocompare.com/data/v2/news/"
    "?lang=EN&categories=BTC,ETH,Trading&limit=8"
)

# ── Cache ──────────────────────────────────────────────────────────────────────
_cache: dict = {}
_CACHE_TTL = 600


def _cached_get(url: str, timeout: int = 8) -> Optional[dict]:
    now = time.time()
    entry = _cache.get(url)
    if entry and now - entry["ts"] < _CACHE_TTL:
        return entry["data"]
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "tradingbot/1.0"})
        r.raise_for_status()
        data = r.json()
        _cache[url] = {"data": data, "ts": now}
        return data
    except Exception as exc:
        logger.debug("Market data fetch failed [%s]: %s", url, exc)
        return None


# ── Market context builder ─────────────────────────────────────────────────────

def _build_market_context(pairs: list, bot_context: dict, sharpe_data: dict = None) -> str:
    lines = []

    # Fear & Greed (last 3 days to show trend)
    fg = _cached_get(_FEAR_GREED_URL)
    if fg and fg.get("data"):
        vals = [f"{d.get('value')}/100 ({d.get('value_classification')})" for d in fg["data"][:3]]
        lines.append(f"Fear & Greed (today→2d ago): {' | '.join(vals)}")

    # CoinGecko global
    cg = _cached_get(_COINGECKO_URL)
    if cg and cg.get("data"):
        d = cg["data"]
        btc_dom = round(d.get("market_cap_percentage", {}).get("btc", 0), 1)
        chg_24h = round(d.get("market_cap_change_percentage_24h_usd", 0), 2)
        active  = d.get("active_cryptocurrencies", "?")
        lines.append(f"BTC dominance: {btc_dom}% | Global 24h change: {chg_24h}% | Active coins: {active}")

    # News headlines
    news = _cached_get(_NEWS_URL)
    if news and news.get("Data"):
        lines.append("Recent crypto headlines:")
        for art in news["Data"][:6]:
            lines.append(f"  • {art.get('title', '')}")

    lines.append(f"\nBot pairs: {', '.join(pairs)}")

    # ── On-chain data ─────────────────────────────────────────────────────────
    if _ONCHAIN_AVAILABLE:
        try:
            _oc = _fetch_onchain()
            if _oc.get("available"):
                lines.append("\n--- ON-CHAIN DATA ---")
                btc_net = _oc.get("btc_network", {})
                if btc_net.get("summary"):
                    lines.append(btc_net["summary"])
                eth_gas = _oc.get("eth_gas", {})
                if eth_gas.get("summary"):
                    lines.append(eth_gas["summary"])
                btc_flow = _oc.get("btc_flows", {})
                if btc_flow.get("summary"):
                    lines.append(btc_flow["summary"])
                lines.append(
                    f"On-chain combined signal: {_oc.get('combined_score', 0):+.2f} "
                    f"(positive=bullish activity, negative=bearish)"
                )
        except Exception as _oce:
            logger.debug("On-chain context injection failed: %s", _oce)

    # ── Sharpe.ai institutional data ──────────────────────────────────────────
    if _SHARPE_AVAILABLE:
        try:
            sharpe = sharpe_data if sharpe_data else _sharpe_fetch_all(pairs)
            if sharpe.get("available"):
                lines.append("\n--- SHARPE.AI DERIVATIVES DATA ---")

                # Funding rates
                fd = sharpe.get("funding", {})
                if fd.get("summary"):
                    lines.append(fd["summary"])
                    lines.append(
                        "  Interpretation: positive rate = longs crowded = bearish contrarian signal; "
                        "negative rate = shorts crowded = bullish contrarian signal."
                    )

                # Insider selling — top flagged tokens across market
                ins = sharpe.get("insider", {})
                if ins.get("summary"):
                    lines.append(ins["summary"])

                # Derivatives overview
                deriv = sharpe.get("derivatives", {})
                if deriv.get("total_oi_usd"):
                    oi_b = round(deriv["total_oi_usd"] / 1e9, 1)
                    oi_fund = deriv.get("oi_weighted_funding_rate")
                    lines.append(
                        f"Market-wide perp OI: ${oi_b}B | OI-weighted funding: "
                        f"{oi_fund:.6f}" if oi_fund else f"Market-wide perp OI: ${oi_b}B"
                    )
                    top_oi = deriv.get("top_coins_oi", [])
                    if top_oi:
                        top_str = ", ".join(
                            f"{c.get('coin')} (${round(c.get('open_interest_usd',0)/1e9,1)}B)"
                            for c in top_oi[:3]
                        )
                        lines.append(f"Top OI coins: {top_str}")

                # News from Sharpe.ai feed
                sharpe_news = sharpe.get("news", [])
                if sharpe_news:
                    lines.append("Sharpe.ai news feed:")
                    for h in sharpe_news:
                        lines.append(f"  • {h}")
        except Exception as _se:
            logger.debug("Sharpe.ai context injection failed: %s", _se)

    # ── Persistent history from DB (trades, past AI calls, funding trends) ───
    if _HISTORY_DB_AVAILABLE:
        try:
            history_block = _build_history_context()
            if history_block:
                lines.append(history_block)
        except Exception as _he:
            logger.debug("History context build failed: %s", _he)

    # ── Recent AI panel history (so models can calibrate against past calls) ──
    recent_intel = _load_recent_intel(n=5)
    if recent_intel:
        lines.append("\n--- RECENT AI PANEL HISTORY ---")
        lines.append("(Use this to judge whether past calls were accurate and calibrate accordingly)")
        for entry in recent_intel:
            ts_str  = entry.get("ts", "")[:16].replace("T", " ")
            score   = entry.get("combined_score", 0.0)
            scores  = entry.get("model_scores", {})
            outcome = entry.get("market_outcome", "unknown")
            score_parts = " | ".join(f"{k}:{v:+.1f}" for k, v in scores.items() if v is not None)
            lines.append(f"  {ts_str}  combined={score:+.2f}  [{score_parts}]  outcome={outcome}")

    # ── Bot performance context ────────────────────────────────────────────────
    lines.append("\n--- BOT PERFORMANCE CONTEXT ---")

    sharpe = bot_context.get("sharpe")
    verdict = bot_context.get("sharpe_verdict", "insufficient_data")
    trending = bot_context.get("sharpe_trending", "stable")
    if sharpe is not None:
        lines.append(f"Current Sharpe ratio: {sharpe:.3f} ({verdict}, {trending})")
        lines.append("  (Target: Sharpe >3.0 = success | Sharpe <1.0 = failure)")
    else:
        lines.append("Sharpe ratio: not yet available (need 10+ closed trades)")

    trade_count = bot_context.get("trade_count", 0)
    lines.append(f"Total closed trades: {trade_count}")

    recent = bot_context.get("recent_trades", [])
    if recent:
        lines.append("Last 5 trades:")
        for t in recent[-5:]:
            pnl = t.get("pnl_eur", 0)
            outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "FLAT")
            lines.append(f"  {t.get('type','?')} {t.get('pair','?')} → {outcome} ({pnl:+.4f} EUR)")

    open_pos = bot_context.get("open_positions", {})
    open_shorts = bot_context.get("open_shorts", {})
    if open_pos or open_shorts:
        lines.append("Current open positions:")
        for pair, info in open_pos.items():
            lines.append(f"  LONG  {pair}: {info.get('qty')} @ €{info.get('entry')}")
        for pair, info in open_shorts.items():
            lines.append(f"  SHORT {pair}: {info.get('qty')} @ €{info.get('entry')}")
    else:
        lines.append("No open positions.")

    signals = bot_context.get("pair_signals", {})
    scores  = bot_context.get("pair_scores", {})
    if signals:
        lines.append("Current technical signals:")
        for pair in signals:
            lines.append(f"  {pair}: {signals[pair]} (score {scores.get(pair, 0):+.2f})")

    balance = bot_context.get("balance_eur")
    if balance is not None:
        lines.append(f"Simulated balance: €{balance:.2f}")

    return "\n".join(lines)


# ── Score parser ───────────────────────────────────────────────────────────────

def _parse_score(text: Optional[str]) -> float:
    if not text:
        return 0.0
    m = re.search(r"score[:\s]+([+-]?\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if not m:
        m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*/\s*5", text)
    if not m:
        # last resort: first number in the text
        m = re.search(r"([+-]?\d+(?:\.\d+)?)", text)
    if m:
        return max(-5.0, min(5.0, float(m.group(1))))
    return 0.0


# ── Single model caller ────────────────────────────────────────────────────────

def _call_model(model_id: str, system: str, user: str, api_key: str,
                base_url: str = "https://openrouter.ai/api/v1") -> Optional[str]:
    if not api_key:
        return None
    try:
        import openai
        client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers={"HTTP-Referer": "https://github.com/tradingbot"},
        )
        resp = client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens=200,
            temperature=0.1,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.warning("Model %s failed: %s", model_id, exc)
        return None


# ── Public API ─────────────────────────────────────────────────────────────────

def get_market_intelligence(pairs: list, bot_context: dict = None) -> dict:
    """
    Query all AI models in parallel and return aggregated market intelligence.

    Parameters
    ----------
    pairs       : list of trading pair strings
    bot_context : dict with keys sharpe, sharpe_verdict, sharpe_trending,
                  trade_count, recent_trades, open_positions, open_shorts,
                  pair_signals, pair_scores, balance_eur

    Returns
    -------
    {
      "score":          float,          # weighted combined score -5..+5
      "model_scores":   dict,           # {name: score} per model
      "model_outputs":  dict,           # {name: raw text} per model
      "sources_used":   int,
      "market_context": str,
    }
    """
    if bot_context is None:
        bot_context = {}

    # Fetch Sharpe.ai data first so it's available in context AND return dict
    _sharpe = {}
    if _SHARPE_AVAILABLE:
        try:
            _sharpe = _sharpe_fetch_all(pairs)
        except Exception as _se:
            logger.debug("Sharpe.ai pre-fetch failed: %s", _se)

    context = _build_market_context(pairs, bot_context, sharpe_data=_sharpe)
    or_key  = os.getenv("OPENROUTER_API_KEY", "")

    model_scores:  dict = {}
    model_outputs: dict = {}
    _lock = threading.Lock()

    def _run(model_id, name, system):
        key = or_key
        base = "https://openrouter.ai/api/v1"
        text = _call_model(model_id, system, context, key, base)
        score = _parse_score(text)
        with _lock:
            model_outputs[name] = text or "unavailable"
            model_scores[name]  = score
        logger.debug("Model %s → score=%.1f text=%s", name, score, (text or "")[:80])

    # Launch all OpenRouter models in parallel
    threads = []
    for model_id, name, _weight, _role, system in _OPENROUTER_MODELS:
        t = threading.Thread(target=_run, args=(model_id, name, system), daemon=True)
        threads.append(t)
        t.start()

    # Wait for all (max 20s so we never block the trading loop)
    for t in threads:
        t.join(timeout=20)

    # Weighted average over models that responded
    weight_map = {name: w for _, name, w, _, _ in _OPENROUTER_MODELS}

    total_weight = 0.0
    weighted_sum = 0.0
    for name, score in model_scores.items():
        if model_outputs.get(name) and model_outputs[name] != "unavailable":
            w = weight_map.get(name, 1.0)
            weighted_sum += score * w
            total_weight  += w

    combined = round(weighted_sum / total_weight, 2) if total_weight > 0 else 0.0
    sources  = sum(1 for v in model_outputs.values() if v != "unavailable")

    result = {
        "score":            combined,
        "model_scores":     model_scores,
        "model_outputs":    model_outputs,
        "sources_used":     sources,
        "market_context":   context,
        "sharpe_funding":   _sharpe.get("funding", {}).get("coin_scores", {}),
        "sharpe_insider":   _sharpe.get("insider", {}).get("market_signal", 0),
    }

    logger.info(
        "MarketIntelligence: combined=%.2f sources=%d/5 scores=%s",
        combined, sources,
        {k: f"{v:+.1f}" for k, v in model_scores.items()},
    )

    # ── Write to persistent history DB ────────────────────────────────────────
    if _HISTORY_DB_AVAILABLE:
        try:
            _record_ai_panel(
                ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                scores={**model_scores, "combined": combined},
                texts=model_outputs,
                sharpe=bot_context.get("sharpe"),
            )
            # Write Sharpe snapshot if available
            if _sharpe.get("available"):
                _record_sharpe_snapshot(
                    ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    funding_scores=_sharpe.get("funding", {}).get("coin_scores", {}),
                    insider_signal=float(_sharpe.get("insider", {}).get("market_signal") or 0),
                    total_oi=_sharpe.get("derivatives", {}).get("total_oi_usd"),
                    oi_weighted=_sharpe.get("derivatives", {}).get("oi_weighted_funding_rate"),
                )
        except Exception as _dbe:
            logger.debug("History DB write failed: %s", _dbe)

    # ── Persist to intelligence log ────────────────────────────────────────────
    # market_outcome is filled in retrospectively by the next entry's context
    # (we record what the bot was told, and the next call can note what happened)
    _log_entry = {
        "ts":            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "combined_score":combined,
        "model_scores":  model_scores,
        "model_outputs": {k: v[:200] if v else None for k, v in model_outputs.items()},
        "sources_used":  sources,
        "bot_snapshot": {
            "sharpe":       bot_context.get("sharpe"),
            "trade_count":  bot_context.get("trade_count"),
            "balance_eur":  bot_context.get("balance_eur"),
            "pair_signals": bot_context.get("pair_signals"),
        },
        "market_outcome": "pending",   # updated retrospectively via next entry
    }
    _append_intel_log(_log_entry)

    return result

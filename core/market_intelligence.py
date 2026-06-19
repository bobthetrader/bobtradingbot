"""
Market Intelligence Module
===========================
Pulls live external market data (Fear & Greed index, global market stats,
news headlines) and queries two AI agents:

  - OpenAI GPT-4o-mini  (OPENAI_API_KEY)
  - Nous Hermes via OpenRouter  (OPENROUTER_API_KEY)

Each agent returns a score in [-5, +5]. The average is the
`intelligence_score` fed back into the bot to tighten or loosen buy gates.

Positive score  → market looks bullish  → lowers effective min_buy_score
Negative score  → market looks bearish  → raises effective min_buy_score

Data sources (all free, no API key needed for basic endpoints):
  - https://api.alternative.me/fng/     Fear & Greed index
  - https://api.coingecko.com/api/v3/   Global market caps
  - https://min-api.cryptocompare.com/  Recent news headlines
"""

import os
import re
import time
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

# ── AI model config ────────────────────────────────────────────────────────────
OPENAI_MODEL = "gpt-4o-mini"
HERMES_MODEL = "nousresearch/hermes-3-llama-3.1-70b"

# ── External data endpoints ────────────────────────────────────────────────────
_FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
_COINGECKO_URL  = "https://api.coingecko.com/api/v3/global"
_NEWS_URL       = (
    "https://min-api.cryptocompare.com/data/v2/news/"
    "?lang=EN&categories=BTC,ETH,Trading&limit=8"
)

# ── Simple in-process cache (10 min TTL) ──────────────────────────────────────
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


def _build_market_context(pairs: list) -> str:
    """Assemble a text summary of current market conditions."""
    lines: list[str] = []

    # Fear & Greed
    fg = _cached_get(_FEAR_GREED_URL)
    if fg and fg.get("data"):
        d = fg["data"][0]
        lines.append(
            f"Crypto Fear & Greed: {d.get('value', '?')}/100 "
            f"({d.get('value_classification', '?')})"
        )

    # CoinGecko global
    cg = _cached_get(_COINGECKO_URL)
    if cg and cg.get("data"):
        d = cg["data"]
        btc_dom = round(d.get("market_cap_percentage", {}).get("btc", 0), 1)
        chg_24h = round(d.get("market_cap_change_percentage_24h_usd", 0), 2)
        lines.append(f"BTC dominance: {btc_dom}% | Global 24h cap change: {chg_24h}%")

    # News headlines
    news = _cached_get(_NEWS_URL)
    if news and news.get("Data"):
        lines.append("Recent headlines:")
        for art in news["Data"][:6]:
            lines.append(f"  • {art.get('title', '')}")

    if not lines:
        lines.append("External market data currently unavailable.")

    lines.append(f"\nBot is trading: {', '.join(pairs)}")
    return "\n".join(lines)


def _call_openai(context: str) -> Optional[str]:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None
    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a crypto market sentiment analyst for a short-term algorithmic "
                        "trading bot (hold time < 2 hours). Analyse the market context provided "
                        "and reply with exactly: 'Score: X. Reason: <one sentence>.' "
                        "where X is an integer from -5 (very bearish) to +5 (very bullish)."
                    ),
                },
                {"role": "user", "content": context},
            ],
            max_tokens=120,
            temperature=0.1,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.debug("OpenAI call failed: %s", exc)
        return None


def _call_hermes(context: str) -> Optional[str]:
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return None
    try:
        import openai
        client = openai.OpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers={"HTTP-Referer": "https://github.com/tradingbot"},
        )
        resp = client.chat.completions.create(
            model=HERMES_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Hermes, an expert crypto trading strategist. "
                        "A short-term bot (< 2-hour holds) needs your assessment. "
                        "Given current market conditions, output exactly: "
                        "'Score: X. Strategy: <one sentence>.' "
                        "where X is an integer from -5 (reduce all exposure) to +5 (increase exposure)."
                    ),
                },
                {"role": "user", "content": context},
            ],
            max_tokens=150,
            temperature=0.1,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.debug("Hermes/OpenRouter call failed: %s", exc)
        return None


def _parse_score(text: Optional[str], default: float = 0.0) -> float:
    """Extract a numeric score in [-5, 5] from model output."""
    if not text:
        return default
    m = re.search(r"score[:\s]+([+-]?\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if not m:
        m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*/\s*5", text)
    if not m:
        m = re.search(r"([+-]?\d+(?:\.\d+)?)", text)
    if m:
        return max(-5.0, min(5.0, float(m.group(1))))
    return default


def get_market_intelligence(pairs: list) -> dict:
    """
    Query external data + AI agents and return a combined market intelligence dict.

    Return shape:
      {
        "score":             float,   # -5.0 … +5.0
        "gpt_score":         float,
        "hermes_score":      float,
        "gpt_reasoning":     str,
        "hermes_reasoning":  str,
        "market_context":    str,
        "sources_used":      int,     # 0, 1, or 2
      }
    """
    context = _build_market_context(pairs)
    gpt_text    = _call_openai(context)
    hermes_text = _call_hermes(context)

    gpt_score    = _parse_score(gpt_text)
    hermes_score = _parse_score(hermes_text)

    available = [(gpt_score, gpt_text), (hermes_score, hermes_text)]
    valid = [(s, t) for s, t in available if t is not None]
    combined = round(sum(s for s, _ in valid) / len(valid), 2) if valid else 0.0

    result = {
        "score":            combined,
        "gpt_score":        gpt_score,
        "hermes_score":     hermes_score,
        "gpt_reasoning":    gpt_text    or "unavailable",
        "hermes_reasoning": hermes_text or "unavailable",
        "market_context":   context,
        "sources_used":     len(valid),
    }
    logger.info(
        "MarketIntelligence: combined=%.2f gpt=%.2f hermes=%.2f sources=%d",
        combined, gpt_score, hermes_score, len(valid),
    )
    return result

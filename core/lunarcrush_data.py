"""
LunarCrush Social Sentiment Module
=====================================
Fetches social volume and sentiment data per coin from LunarCrush.

Social data leads price by 1-6 hours in crypto — when Reddit/Twitter
activity spikes alongside bullish sentiment, price often follows.

Key metrics:
  social_volume     Total social posts/mentions in last 24h
  social_score      Composite engagement score (volume × influence)
  sentiment         % of posts that are bullish (0-100)
  social_dominance  % of total crypto social volume this coin holds
  bullish_mentions  Raw count of bullish posts

Signal logic:
  High social_volume + high sentiment (>60%)  → social bullish signal
  High social_volume + low sentiment  (<40%)  → social bearish signal
  Very high spike in social_volume            → watch for breakout
  Low social_volume                           → no signal, ignore

Requires LUNARCRUSH_API_KEY in Railway Variables / .env
Free tier: 10 requests/min, plenty for our 10-min refresh cycle.
"""

import os
import time
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

_BASE_URL  = "https://lunarcrush.com/api4/public"
_CACHE: dict = {}
_CACHE_TTL = 540   # 9 minutes

# Map Kraken pair names to LunarCrush coin slugs
_PAIR_TO_SLUG = {
    "XBTEUR":   "bitcoin",
    "XXBTZEUR": "bitcoin",
    "XETHZEUR": "ethereum",
    "ETHEUR":   "ethereum",
    "SOLEUR":   "solana",
    "XXRPZEUR": "ripple",
    "XRPEUR":   "ripple",
    "ADAEUR":   "cardano",
    "DOTEUR":   "polkadot",
    "LINKEUR":  "chainlink",
}


def _api_key() -> str:
    return os.getenv("LUNARCRUSH_API_KEY", "")


def _get(path: str, timeout: int = 10) -> Optional[dict]:
    key = _api_key()
    if not key:
        return None
    cache_key = path
    now = time.time()
    if cache_key in _CACHE and now - _CACHE[cache_key]["ts"] < _CACHE_TTL:
        return _CACHE[cache_key]["data"]
    try:
        r = requests.get(
            f"{_BASE_URL}{path}",
            headers={"Authorization": f"Bearer {key}", "User-Agent": "tradingbot/1.0"},
            timeout=timeout,
        )
        if r.status_code == 200:
            data = r.json()
            _CACHE[cache_key] = {"data": data, "ts": now}
            return data
        logger.debug("LunarCrush %s → HTTP %d: %s", path, r.status_code, r.text[:100])
    except Exception as exc:
        logger.debug("LunarCrush fetch failed [%s]: %s", path, exc)
    return None


def _sentiment_signal(sentiment_pct: float, social_volume: int) -> float:
    """
    Convert sentiment % and volume into a [-5, +5] signal score.
    Volume matters: high sentiment on low volume is noise.
    """
    if social_volume < 500:
        return 0.0   # too quiet to be meaningful

    vol_boost = min(2.0, social_volume / 10000)   # caps at 2× for very high volume

    if sentiment_pct >= 70:
        return min(5.0, 3.0 * vol_boost)
    if sentiment_pct >= 55:
        return min(3.0, 1.5 * vol_boost)
    if sentiment_pct >= 45:
        return 0.0   # neutral
    if sentiment_pct >= 30:
        return max(-3.0, -1.5 * vol_boost)
    return max(-5.0, -3.0 * vol_boost)


def get_coin_sentiment(slug: str) -> dict:
    """
    Fetch social sentiment for a single coin from LunarCrush.
    Returns structured dict with signal score.
    """
    data = _get(f"/coins/{slug}/v1")
    if not data:
        return {}

    # LunarCrush v4 response structure
    coin = data.get("data") or data
    if isinstance(coin, list):
        coin = coin[0] if coin else {}

    social_volume    = int(coin.get("social_volume_24h")  or coin.get("social_volume")    or 0)
    social_score     = float(coin.get("social_score")      or 0)
    sentiment        = float(coin.get("sentiment")          or coin.get("social_sentiment") or 50)
    social_dominance = float(coin.get("social_dominance")  or 0)
    alt_rank         = int(coin.get("alt_rank")             or 0)

    # Normalise sentiment to 0-100 if it comes as 0-1
    if 0 < sentiment <= 1:
        sentiment *= 100

    signal = _sentiment_signal(sentiment, social_volume)

    return {
        "slug":             slug,
        "social_volume_24h":social_volume,
        "social_score":     round(social_score, 1),
        "sentiment_pct":    round(sentiment, 1),
        "social_dominance": round(social_dominance, 2),
        "alt_rank":         alt_rank,
        "signal":           signal,
        "summary": (
            f"{slug}: vol={social_volume:,} "
            f"sentiment={sentiment:.0f}% "
            f"signal={signal:+.1f}"
        ),
    }


def fetch_all_sentiment(pairs: list) -> dict:
    """
    Fetch LunarCrush sentiment for all traded pairs.
    Returns per-coin signals + combined weighted score.
    """
    key = _api_key()
    if not key:
        return {"available": False}

    results = {}
    signals = []

    seen_slugs = set()
    for pair in pairs:
        slug = _PAIR_TO_SLUG.get(pair)
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        coin_data = get_coin_sentiment(slug)
        if coin_data:
            results[pair] = coin_data
            signals.append(coin_data["signal"])

    if not results:
        return {"available": False}

    combined = round(sum(signals) / len(signals), 2)
    logger.info(
        "LunarCrush: %d coins | combined=%.2f | signals=%s",
        len(results), combined,
        {p: f"{v['signal']:+.1f}" for p, v in results.items()},
    )

    return {
        "available": True,
        "coins":     results,
        "combined":  combined,
        "summary":   f"Social sentiment combined: {combined:+.2f} ({len(results)} coins)",
    }

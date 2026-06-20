"""
Sharpe.ai Data Module
======================
Fetches institutional-grade crypto derivatives and market data from
the Sharpe.ai API and converts it into signals for the trading bot.

Endpoints used:
  GET /v1/funding/rates?type=current   — perpetual funding rates (all exchanges)
  GET /v1/insider-selling/data         — systematic short-positioning scores (0-10)
  GET /v1/pump-dump/data               — pump-and-dump detection scores (0-10)
  GET /v1/news/curated                 — AI-curated crypto news headlines
  GET /v1/tracker/market-overview      — market cap, BTC dominance, top movers
  GET /v1/market/derivatives-overview  — aggregated OI and weighted funding rates

Authentication: Authorization: Bearer sk_live_...
Set SHARPE_API_KEY in environment / Railway Variables.

Funding rate signal logic (contrarian):
  Positive funding → longs paying shorts → market overextended bullish
    → contrarian BEARISH signal (negative score)
  Negative funding → shorts paying longs → market overextended bearish
    → contrarian BULLISH signal (positive score)
"""

import os
import time
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.sharpe.ai/api"
_CACHE: dict = {}
_CACHE_TTL = 540   # 9 minutes (inside the 10-min intelligence refresh cycle)

# Map Kraken pair names to Sharpe base_coin tickers
_PAIR_TO_COIN = {
    "XBTEUR": "BTC",
    "ETHEUR": "ETH",
    "SOLEUR": "SOL",
    "XRPEUR": "XRP",
}


def _api_key() -> str:
    return os.getenv("SHARPE_API_KEY", "")


def _get(path: str, params: dict = None, timeout: int = 10) -> Optional[dict]:
    """Cached GET against the Sharpe.ai API."""
    key = os.getenv("SHARPE_API_KEY", "")
    if not key:
        return None

    cache_key = f"{path}:{str(params)}"
    now = time.time()
    if cache_key in _CACHE and now - _CACHE[cache_key]["ts"] < _CACHE_TTL:
        return _CACHE[cache_key]["data"]

    try:
        resp = requests.get(
            f"{_BASE_URL}{path}",
            params=params or {},
            headers={
                "Authorization": f"Bearer {key}",
                "User-Agent": "tradingbot/1.0",
            },
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            _CACHE[cache_key] = {"data": data, "ts": now}
            return data
        logger.debug("Sharpe.ai %s → HTTP %d", path, resp.status_code)
    except Exception as exc:
        logger.debug("Sharpe.ai fetch failed [%s]: %s", path, exc)
    return None


# ── Funding rates ──────────────────────────────────────────────────────────────

def _funding_score(avg_rate: float) -> float:
    """
    Convert an average perpetual funding rate to a signal score [-5, +5].

    Positive rate → market is long-biased → contrarian bearish → negative score.
    Negative rate → market is short-biased → contrarian bullish → positive score.
    """
    # Annualise for easier reasoning: rate × 3 × 365 (three 8-hour periods/day)
    apy = avg_rate * 3 * 365  # e.g. 0.0001 → ~10.95% APY

    if apy > 1.00:    return -5.0   # >100% APY — extreme long crowding
    if apy > 0.35:    return -4.0   # >35% APY
    if apy > 0.15:    return -3.0   # >15% APY
    if apy > 0.05:    return -2.0   # >5% APY
    if apy > 0.01:    return -1.0   # slightly positive
    if apy > -0.01:   return  0.0   # neutral
    if apy > -0.05:   return +1.0   # slightly negative
    if apy > -0.15:   return +2.0
    if apy > -0.35:   return +3.0
    if apy > -1.00:   return +4.0
    return +5.0                      # < -100% APY — extreme short crowding


def get_funding_data(pairs: list) -> dict:
    """
    Fetch current funding rates and compute per-coin scores.

    Returns:
        {
          "coin_rates":   {coin: avg_rate},   e.g. {"BTC": 0.0001}
          "coin_scores":  {coin: score},       e.g. {"BTC": -1.0}
          "combined_score": float,             weighted average over traded coins
          "summary": str,                      human-readable summary line
        }
    """
    coins = list({_PAIR_TO_COIN[p] for p in pairs if p in _PAIR_TO_COIN})
    result = {"coin_rates": {}, "coin_scores": {}, "combined_score": 0.0, "summary": ""}

    data = _get("/v1/funding/rates", {"type": "current"})
    if not data or not data.get("data"):
        return result

    rows = data["data"]
    # Group by base_coin, average across exchanges
    buckets: dict = {}
    for row in rows:
        coin = row.get("base_coin", "").upper()
        rate = row.get("rate")
        if coin in coins and rate is not None:
            buckets.setdefault(coin, []).append(float(rate))

    scores = []
    for coin in coins:
        if coin not in buckets:
            continue
        avg = sum(buckets[coin]) / len(buckets[coin])
        score = _funding_score(avg)
        result["coin_rates"][coin] = round(avg, 6)
        result["coin_scores"][coin] = score
        scores.append(score)

    if scores:
        result["combined_score"] = round(sum(scores) / len(scores), 2)

    parts = [f"{c}: {r:+.5f} ({result['coin_scores'][c]:+.0f})"
             for c, r in result["coin_rates"].items()]
    result["summary"] = "Funding rates — " + " | ".join(parts)
    return result


# ── Insider selling ────────────────────────────────────────────────────────────

def get_insider_selling(pairs: list) -> dict:
    """
    Fetch insider-selling composite scores (0-10) for traded coins.
    Higher score = persistent negative funding = systematic short positioning.
    Maps to signal score: high insider selling → bearish → negative score.

    Returns:
        {
          "coin_scores": {coin: insider_score_0_to_10},
          "signal_scores": {coin: signal_-5_to_+5},
          "summary": str,
        }
    """
    coins = {_PAIR_TO_COIN[p] for p in pairs if p in _PAIR_TO_COIN}
    result = {"coin_scores": {}, "signal_scores": {}, "summary": ""}

    data = _get("/v1/insider-selling/data", {"limit": 200})
    if not data or not data.get("data"):
        return result

    rows = data["data"] if isinstance(data["data"], list) else []
    for row in rows:
        coin = (row.get("coin") or row.get("base_coin") or row.get("symbol") or "").upper()
        if coin not in coins:
            continue
        score = float(row.get("score") or row.get("insider_score") or 0)
        result["coin_scores"][coin] = round(score, 2)
        # Map 0-10 insider score to signal: high score → bearish → negative signal
        signal = round(-(score / 10) * 4, 1)   # 10 → -4, 0 → 0
        result["signal_scores"][coin] = signal

    parts = [f"{c}: insider={s:.1f}" for c, s in result["coin_scores"].items()]
    result["summary"] = "Insider selling — " + " | ".join(parts) if parts else ""
    return result


# ── Pump & dump detection ──────────────────────────────────────────────────────

def get_pump_dump(pairs: list) -> dict:
    """
    Fetch pump-and-dump detection scores.
    Flags: price rising + funding negative = suspicious.
    Returns coin → (pd_score 0-10, phase str).
    """
    coins = {_PAIR_TO_COIN[p] for p in pairs if p in _PAIR_TO_COIN}
    result: dict = {}

    data = _get("/v1/pump-dump/data", {"limit": 200})
    if not data or not data.get("data"):
        return result

    rows = data["data"] if isinstance(data["data"], list) else []
    for row in rows:
        coin = (row.get("coin") or row.get("base_coin") or "").upper()
        if coin in coins:
            result[coin] = {
                "score": float(row.get("score") or 0),
                "phase": row.get("phase") or "unknown",
            }
    return result


# ── Curated news ───────────────────────────────────────────────────────────────

def get_curated_news(limit: int = 8) -> list:
    """
    Fetch AI-curated crypto news headlines from Sharpe.ai.
    Returns list of headline strings.
    """
    data = _get("/v1/news/curated", {"limit": limit, "category": "crypto"})
    if not data or not data.get("data"):
        return []
    rows = data["data"] if isinstance(data["data"], list) else []
    headlines = []
    for row in rows:
        title = row.get("title") or row.get("headline") or row.get("banner") or ""
        if title:
            headlines.append(str(title))
    return headlines[:limit]


# ── Market overview ────────────────────────────────────────────────────────────

def get_market_overview() -> dict:
    """
    Fetch broad market overview: BTC dominance, 24h change, top movers.
    Returns a summary dict.
    """
    data = _get("/v1/tracker/market-overview")
    if not data or not data.get("data"):
        return {}
    d = data["data"]
    return {
        "btc_dominance":    d.get("btc_dominance") or d.get("btcDominance"),
        "market_cap_change_24h": d.get("market_cap_change_24h") or d.get("marketCapChange24h"),
        "top_gainers":      d.get("top_gainers") or d.get("topGainers") or [],
        "top_losers":       d.get("top_losers") or d.get("topLosers") or [],
    }


# ── Derivatives overview ───────────────────────────────────────────────────────

def get_derivatives_overview() -> dict:
    """
    Fetch aggregated derivatives metrics: total OI, OI-weighted funding rate.
    """
    data = _get("/v1/market/derivatives-overview")
    if not data or not data.get("data"):
        return {}
    d = data["data"]
    return {
        "total_oi_usd":          d.get("total_oi_usd") or d.get("totalOiUsd"),
        "avg_funding_rate":      d.get("avg_funding_rate") or d.get("avgFundingRate"),
        "oi_weighted_funding":   d.get("oi_weighted_funding") or d.get("oiWeightedFunding"),
    }


# ── Combined fetch ─────────────────────────────────────────────────────────────

def fetch_all(pairs: list) -> dict:
    """
    Fetch all Sharpe.ai data in one call. Used by market_intelligence.py.

    Returns:
        {
          "funding":    dict from get_funding_data(),
          "insider":    dict from get_insider_selling(),
          "pump_dump":  dict from get_pump_dump(),
          "news":       list[str],
          "overview":   dict,
          "derivatives":dict,
          "available":  bool,
        }
    """
    key = _api_key()
    if not key:
        return {"available": False}

    try:
        funding    = get_funding_data(pairs)
        insider    = get_insider_selling(pairs)
        pump_dump  = get_pump_dump(pairs)
        news       = get_curated_news()
        overview   = get_market_overview()
        derivatives= get_derivatives_overview()

        logger.info(
            "Sharpe.ai: funding=%s insider=%s news=%d",
            funding.get("coin_scores"),
            insider.get("signal_scores"),
            len(news),
        )
        return {
            "funding":    funding,
            "insider":    insider,
            "pump_dump":  pump_dump,
            "news":       news,
            "overview":   overview,
            "derivatives":derivatives,
            "available":  True,
        }
    except Exception as exc:
        logger.warning("Sharpe.ai fetch_all failed: %s", exc)
        return {"available": False}

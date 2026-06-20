"""
Sharpe.ai Data Module
======================
Fetches institutional-grade crypto derivatives data from the Sharpe.ai API.

Endpoints used:
  GET /v1/funding/rates?type=history&coin=X&days=1   — recent funding per coin
  GET /v1/market/derivatives-overview                 — market-wide OI & funding
  GET /v1/insider-selling/data                        — systematic short-positioning scores
  GET /v1/news/feed                                   — raw news feed

Authentication: Authorization: Bearer sk_live_...
Set SHARPE_API_KEY in environment / Railway Variables.
"""

import os
import time
import logging
import threading
import requests
from typing import Optional

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.sharpe.ai/api"
_CACHE: dict = {}
_CACHE_TTL = 540   # 9 minutes

_PAIR_TO_COIN = {
    "XBTEUR": "BTC",
    "ETHEUR": "ETH",
    "SOLEUR": "SOL",
    "XRPEUR": "XRP",
}


def _api_key() -> str:
    return os.getenv("SHARPE_API_KEY", "")


def _get(path: str, params: dict = None, timeout: int = 10) -> Optional[dict]:
    key = _api_key()
    if not key:
        return None
    cache_key = f"{path}:{str(sorted((params or {}).items()))}"
    now = time.time()
    if cache_key in _CACHE and now - _CACHE[cache_key]["ts"] < _CACHE_TTL:
        return _CACHE[cache_key]["data"]
    try:
        resp = requests.get(
            f"{_BASE_URL}{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {key}", "User-Agent": "tradingbot/1.0"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            _CACHE[cache_key] = {"data": data, "ts": now}
            return data
        logger.debug("Sharpe.ai %s → HTTP %d: %s", path, resp.status_code, resp.text[:100])
    except Exception as exc:
        logger.debug("Sharpe.ai fetch failed [%s]: %s", path, exc)
    return None


# ── Funding rate score ─────────────────────────────────────────────────────────

def _funding_score(rate: float, interval_hours: float) -> float:
    """
    Convert a raw funding rate to a [-5, +5] contrarian signal score.
    Normalises to 8-hour equivalent then annualises.
    Positive rate = longs crowded = contrarian bearish = negative score.
    """
    rate_8h = rate * (8.0 / max(interval_hours, 0.001))
    apy = rate_8h * 3 * 365

    if apy >  1.00: return -5.0
    if apy >  0.35: return -4.0
    if apy >  0.15: return -3.0
    if apy >  0.05: return -2.0
    if apy >  0.01: return -1.0
    if apy > -0.01: return  0.0
    if apy > -0.05: return +1.0
    if apy > -0.15: return +2.0
    if apy > -0.35: return +3.0
    if apy > -1.00: return +4.0
    return +5.0


# ── Per-coin funding rates (parallel) ─────────────────────────────────────────

def get_funding_data(pairs: list) -> dict:
    """
    Fetch most-recent funding rate for each of our coins using the history endpoint.
    Returns coin_rates, coin_scores and a summary string.
    """
    coins = list({_PAIR_TO_COIN[p] for p in pairs if p in _PAIR_TO_COIN})
    coin_rates: dict = {}
    coin_scores: dict = {}
    _lock = threading.Lock()

    def _fetch_coin(coin):
        data = _get("/v1/funding/rates", {"type": "history", "coin": coin, "days": 1, "limit": 3})
        if not data:
            return
        rows = data.get("data", [])
        if not isinstance(rows, list) or not rows:
            return
        # Average the last few entries across exchanges
        valid = [(r.get("rate"), r.get("interval_hours", 8)) for r in rows
                 if r.get("rate") is not None]
        if not valid:
            return
        avg_rate = sum(r for r, _ in valid) / len(valid)
        avg_interval = sum(i for _, i in valid) / len(valid)
        score = _funding_score(avg_rate, avg_interval)
        with _lock:
            coin_rates[coin] = round(avg_rate, 8)
            coin_scores[coin] = score

    threads = [threading.Thread(target=_fetch_coin, args=(c,), daemon=True) for c in coins]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=12)

    combined = round(sum(coin_scores.values()) / len(coin_scores), 2) if coin_scores else 0.0
    parts = [f"{c}: {coin_rates.get(c, 0):+.6f} (signal {coin_scores.get(c, 0):+.0f})"
             for c in coins if c in coin_rates]
    summary = "Funding rates — " + " | ".join(parts) if parts else ""

    return {
        "coin_rates":     coin_rates,
        "coin_scores":    coin_scores,
        "combined_score": combined,
        "summary":        summary,
    }


# ── Derivatives overview ───────────────────────────────────────────────────────

def get_derivatives_overview() -> dict:
    data = _get("/v1/market/derivatives-overview")
    if not data:
        return {}
    d = data.get("data", {})
    if not isinstance(d, dict):
        return {}
    return {
        "total_oi_usd":            d.get("total_oi_usd"),
        "avg_funding_rate":        d.get("avg_funding_rate"),
        "oi_weighted_funding_rate":d.get("oi_weighted_funding_rate"),
        "top_coins_oi":            d.get("top_coins_oi", [])[:5],
    }


# ── Insider selling ────────────────────────────────────────────────────────────

def get_insider_selling(pairs: list) -> dict:
    """
    Returns top tokens flagged for insider selling as general market context.
    BTC/ETH/SOL/XRP rarely appear here — this signals which small-caps are
    being systematically shorted by insiders, useful market-wide context.
    """
    data = _get("/v1/insider-selling/data", {"limit": 100})
    if not data:
        return {"top_flagged": [], "signal_scores": {}, "summary": ""}

    coins_list = data.get("data", {})
    if isinstance(coins_list, dict):
        coins_list = coins_list.get("coins", [])
    if not isinstance(coins_list, list):
        return {"top_flagged": [], "signal_scores": {}, "summary": ""}

    top = sorted(coins_list, key=lambda x: float(x.get("score") or 0), reverse=True)[:5]
    top_flagged = [{"symbol": c.get("symbol"), "score": c.get("score"),
                    "phase": c.get("manipulation_phase")} for c in top]

    # Market sentiment: high average insider score = bearish signal
    avg_score = sum(float(c.get("score") or 0) for c in top) / len(top) if top else 0
    market_signal = round(-(avg_score / 10) * 3, 1)  # 10 → -3, 0 → 0

    parts = [f"{t['symbol']}:{t['score']}" for t in top_flagged]
    summary = f"Insider selling top flagged: {', '.join(parts)} | market signal: {market_signal:+.1f}" if parts else ""

    return {"top_flagged": top_flagged, "market_signal": market_signal, "summary": summary}


# ── News feed ─────────────────────────────────────────────────────────────────

def get_news(limit: int = 6) -> list:
    """Fetch news headlines — tries feed endpoint, extracts titles."""
    data = _get("/v1/news/feed", {"limit": limit, "category": "crypto"})
    if not data:
        return []
    raw = data.get("data", [])
    # Handle list or dict with articles/items key
    if isinstance(raw, dict):
        raw = raw.get("articles") or raw.get("items") or raw.get("news") or []
    if not isinstance(raw, list):
        return []
    headlines = []
    for row in raw:
        title = row.get("title") or row.get("headline") or row.get("name") or ""
        if title:
            headlines.append(str(title))
    return headlines[:limit]


# ── Combined fetch ─────────────────────────────────────────────────────────────

def fetch_all(pairs: list) -> dict:
    key = _api_key()
    if not key:
        return {"available": False}

    try:
        funding    = get_funding_data(pairs)
        derivatives= get_derivatives_overview()
        insider    = get_insider_selling(pairs)
        news       = get_news()

        logger.info(
            "Sharpe.ai: funding_scores=%s insider_signal=%s derivatives_oi=$%.0fB news=%d",
            funding.get("coin_scores", {}),
            insider.get("market_signal", 0),
            (derivatives.get("total_oi_usd") or 0) / 1e9,
            len(news),
        )
        return {
            "funding":    funding,
            "derivatives":derivatives,
            "insider":    insider,
            "news":       news,
            "available":  True,
        }
    except Exception as exc:
        logger.warning("Sharpe.ai fetch_all failed: %s", exc)
        return {"available": False}

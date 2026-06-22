"""
Social Sentiment Module
========================
Free social/trend data from CoinGecko and Alternative.me.

Reddit API now requires OAuth2 (blocked free access in 2023).
CoinGecko community data returns zeros on free tier.

Working free sources:
  CoinGecko /search/trending  — top 7 trending coins RIGHT NOW (retail attention)
  CoinGecko /coins/{id}       — 24h/7d price change as momentum proxy
  Alternative.me /fng         — Fear & Greed index (already used in AI panel,
                                 applied here coin-specifically)

Signal logic:
  Coin in trending list     → strong retail attention → bullish +2 to +3
  Coin NOT trending         → no social momentum → neutral 0
  Fear & Greed extreme fear  → contrarian bullish +1 (market oversold)
  Fear & Greed extreme greed → contrarian bearish -1 (market overbought)
  Strong 24h price change    → momentum confirmation
"""

import time
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE: dict = {}
_CACHE_TTL = 300   # 5 minutes

_PAIR_TO_CG_ID = {
    "XBTEUR":   "bitcoin",
    "XXBTZEUR": "bitcoin",
    "ETHEUR":   "ethereum",
    "XETHZEUR": "ethereum",
    "SOLEUR":   "solana",
    "XXRPZEUR": "ripple",
    "XRPEUR":   "ripple",
    "ADAEUR":   "cardano",
    "DOTEUR":   "polkadot",
    "LINKEUR":  "chainlink",
}

_PAIR_TO_SYMBOL = {
    "XBTEUR":   "BTC",  "XXBTZEUR": "BTC",
    "ETHEUR":   "ETH",  "XETHZEUR": "ETH",
    "SOLEUR":   "SOL",
    "XXRPZEUR": "XRP",  "XRPEUR":   "XRP",
    "ADAEUR":   "ADA",
    "DOTEUR":   "DOT",
    "LINKEUR":  "LINK",
}


def _cached_get(url: str, params: dict = None, timeout: int = 8) -> Optional[dict]:
    now = time.time()
    key = url + str(sorted((params or {}).items()))
    if key in _CACHE and now - _CACHE[key]["ts"] < _CACHE_TTL:
        return _CACHE[key]["data"]
    try:
        r = requests.get(url, params=params or {},
                         headers={"User-Agent": "tradingbot/1.0"}, timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            _CACHE[key] = {"data": data, "ts": now}
            return data
    except Exception as exc:
        logger.debug("social fetch failed [%s]: %s", url, exc)
    return None


def get_trending_symbols() -> set:
    """Return the set of coin symbols currently trending on CoinGecko."""
    data = _cached_get("https://api.coingecko.com/api/v3/search/trending")
    if not data:
        return set()
    symbols = set()
    for c in data.get("coins", []):
        sym = c.get("item", {}).get("symbol", "").upper()
        if sym:
            symbols.add(sym)
    return symbols


def get_fear_greed() -> Optional[int]:
    """Return current Fear & Greed value (0-100)."""
    data = _cached_get("https://api.alternative.me/fng/?limit=1")
    if data and data.get("data"):
        try:
            return int(data["data"][0].get("value", 50))
        except Exception:
            pass
    return None


def get_coin_price_change(cg_id: str) -> dict:
    """24h and 7d price change from CoinGecko market data."""
    data = _cached_get(
        f"https://api.coingecko.com/api/v3/coins/{cg_id}",
        params={"localization": "false", "tickers": "false",
                "community_data": "false", "developer_data": "false", "sparkline": "false"},
    )
    if not data:
        return {}
    md = data.get("market_data", {})
    return {
        "change_24h": float(md.get("price_change_percentage_24h") or 0),
        "change_7d":  float(md.get("price_change_percentage_7d")  or 0),
    }


def get_coin_sentiment(pair: str) -> dict:
    """Derive social sentiment signal for a single pair."""
    symbol  = _PAIR_TO_SYMBOL.get(pair, "")
    cg_id   = _PAIR_TO_CG_ID.get(pair, "")
    if not symbol:
        return {}

    trending = get_trending_symbols()
    fg       = get_fear_greed()
    price_ch = get_coin_price_change(cg_id) if cg_id else {}

    is_trending   = symbol in trending
    change_24h    = price_ch.get("change_24h", 0)

    signal = 0.0

    # Trending = retail attention = bullish
    if is_trending:
        signal += 3.0

    # Fear & Greed contrarian signal
    if fg is not None:
        if fg <= 20:
            signal += 1.5   # extreme fear = contrarian bullish
        elif fg <= 35:
            signal += 0.5
        elif fg >= 80:
            signal -= 1.5   # extreme greed = contrarian bearish
        elif fg >= 65:
            signal -= 0.5

    # Momentum confirmation from price change
    if change_24h > 5:
        signal += 0.5
    elif change_24h < -5:
        signal -= 0.5

    signal = round(max(-5.0, min(5.0, signal)), 1)

    trending_str = "TRENDING" if is_trending else "not trending"
    fg_str       = f"F&G {fg}" if fg is not None else ""
    return {
        "symbol":       symbol,
        "is_trending":  is_trending,
        "fear_greed":   fg,
        "change_24h":   change_24h,
        "signal":       signal,
        "summary": (
            f"{symbol}: {trending_str} | {fg_str} | "
            f"24h {change_24h:+.1f}% | signal {signal:+.1f}"
        ),
    }


def fetch_all_sentiment(pairs: list) -> dict:
    """Fetch social sentiment for all traded pairs."""
    trending = get_trending_symbols()
    fg       = get_fear_greed()

    results = {}
    signals = []
    seen    = set()

    for pair in pairs:
        sym = _PAIR_TO_SYMBOL.get(pair, "")
        if not sym or sym in seen:
            continue
        seen.add(sym)

        data = get_coin_sentiment(pair)
        if data:
            results[pair] = data
            signals.append(data["signal"])

    if not results:
        return {"available": False}

    combined     = round(sum(signals) / len(signals), 2) if signals else 0.0
    trending_our = [_PAIR_TO_SYMBOL.get(p, "") for p in pairs
                    if _PAIR_TO_SYMBOL.get(p, "") in trending]

    logger.info(
        "Social: F&G=%s | trending=%s | our coins trending=%s | combined=%.2f",
        fg, trending, trending_our, combined
    )

    return {
        "available":    True,
        "coins":        results,
        "combined":     combined,
        "trending_now": sorted(trending),
        "fear_greed":   fg,
        "summary":      (
            f"Social: F&G={fg}/100 | "
            f"trending: {', '.join(trending_our) or 'none of ours'} | "
            f"combined {combined:+.2f}"
        ),
    }

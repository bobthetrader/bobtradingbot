"""
Social Sentiment Module
========================
Free social/trend data from CoinGecko, Reddit, and Alternative.me.

Sources:
  CoinGecko /search/trending  — top 7 trending coins RIGHT NOW (retail attention)
  CoinGecko /coins/{id}       — 24h/7d price change as momentum proxy
  Reddit public JSON          — mention counts in r/cryptocurrency + r/bitcoin hot posts
                                No OAuth needed — public subreddit JSON endpoint
  Alternative.me /fng         — Fear & Greed index

Signal logic:
  Coin in CoinGecko trending  → +2.0
  Reddit mentions (high)      → +1.5 | (medium) → +0.5 | (low) → 0
  Fear & Greed extreme fear   → contrarian bullish +1 (market oversold)
  Fear & Greed extreme greed  → contrarian bearish -1 (market overbought)
  Strong 24h price change     → momentum confirmation ±0.5
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


_REDDIT_SUBREDDITS = ["cryptocurrency", "bitcoin", "ethereum", "CryptoMarkets"]
_REDDIT_SYMBOL_ALIASES = {
    "BTC": ["BTC", "Bitcoin", "BITCOIN"],
    "ETH": ["ETH", "Ethereum", "ETHEREUM"],
    "SOL": ["SOL", "Solana", "SOLANA"],
    "XRP": ["XRP", "Ripple", "RIPPLE"],
    "ADA": ["ADA", "Cardano", "CARDANO"],
    "DOT": ["DOT", "Polkadot", "POLKADOT"],
    "LINK": ["LINK", "Chainlink", "CHAINLINK"],
    "AVAX": ["AVAX", "Avalanche", "AVALANCHE"],
    "ATOM": ["ATOM", "Cosmos", "COSMOS"],
    "UNI": ["UNI", "Uniswap", "UNISWAP"],
}


def get_reddit_mentions(symbols: list) -> dict:
    """Count symbol mentions in hot posts across crypto subreddits.

    Uses public Reddit JSON (no OAuth needed).
    Returns {symbol: mention_count} for the provided symbols.
    Cached for 10 minutes to avoid hammering Reddit.
    """
    cache_key = "reddit_mentions_" + ",".join(sorted(symbols))
    now = time.time()
    if cache_key in _CACHE and (now - _CACHE[cache_key]["ts"]) < 600:
        return _CACHE[cache_key]["data"]

    mention_counts = {sym: 0 for sym in symbols}

    for subreddit in _REDDIT_SUBREDDITS:
        try:
            url = f"https://www.reddit.com/r/{subreddit}/hot.json"
            resp = requests.get(
                url,
                params={"limit": 50},
                headers={"User-Agent": "tradingbot-sentiment/1.0"},
                timeout=8,
            )
            if resp.status_code != 200:
                continue

            posts = resp.json().get("data", {}).get("children", [])
            for post in posts:
                data = post.get("data", {})
                text = (data.get("title", "") + " " + data.get("selftext", "")).upper()
                for sym in symbols:
                    aliases = _REDDIT_SYMBOL_ALIASES.get(sym, [sym])
                    for alias in aliases:
                        mention_counts[sym] += text.count(alias.upper())
        except Exception as exc:
            logger.debug("Reddit fetch failed for r/%s: %s", subreddit, exc)

    _CACHE[cache_key] = {"data": mention_counts, "ts": now}
    logger.debug("Reddit mentions: %s", mention_counts)
    return mention_counts


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
    """24h price change from CoinGecko simple/price — ~100 bytes vs 30KB for /coins/{id}."""
    data = _cached_get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": cg_id, "vs_currencies": "eur",
                "include_24hr_change": "true"},
    )
    if not data or cg_id not in data:
        return {}
    return {
        "change_24h": float(data[cg_id].get("eur_24h_change") or 0),
        "change_7d":  0.0,
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


def _batch_price_changes(cg_ids: list) -> dict:
    """Fetch 24h price changes for all coins in ONE request (~200 bytes vs 30KB×N)."""
    ids_str = ",".join(set(cg_ids))
    data = _cached_get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": ids_str, "vs_currencies": "eur", "include_24hr_change": "true"},
    )
    if not data:
        return {}
    return {cg_id: float(data.get(cg_id, {}).get("eur_24h_change") or 0) for cg_id in cg_ids}


def fetch_all_sentiment(pairs: list) -> dict:
    """Fetch social sentiment for all traded pairs."""
    trending = get_trending_symbols()
    fg       = get_fear_greed()

    # Batch price changes — one request for all coins instead of one per coin
    cg_ids = list({_PAIR_TO_CG_ID[p] for p in pairs if p in _PAIR_TO_CG_ID})
    price_changes = _batch_price_changes(cg_ids)

    # Reddit mention counts (cached 10 min)
    all_symbols = list({_PAIR_TO_SYMBOL[p] for p in pairs if p in _PAIR_TO_SYMBOL})
    reddit_mentions = {}
    try:
        reddit_mentions = get_reddit_mentions(all_symbols)
    except Exception as exc:
        logger.debug("Reddit mentions failed: %s", exc)

    # Derive thresholds from total mentions across all coins
    total_mentions = sum(reddit_mentions.values()) or 1
    reddit_high_thresh  = max(10, total_mentions * 0.25)   # top 25% = high
    reddit_med_thresh   = max(3,  total_mentions * 0.10)   # top 10% = medium

    results = {}
    signals = []
    seen    = set()

    for pair in pairs:
        sym   = _PAIR_TO_SYMBOL.get(pair, "")
        cg_id = _PAIR_TO_CG_ID.get(pair, "")
        if not sym or sym in seen:
            continue
        seen.add(sym)

        is_trending  = sym in trending
        change_24h   = price_changes.get(cg_id, 0)
        reddit_count = reddit_mentions.get(sym, 0)

        signal = 0.0
        if is_trending:
            signal += 2.0
        # Reddit signal
        if reddit_count >= reddit_high_thresh:
            signal += 1.5
        elif reddit_count >= reddit_med_thresh:
            signal += 0.5
        if fg is not None:
            if fg <= 20:   signal += 1.5
            elif fg <= 35: signal += 0.5
            elif fg >= 80: signal -= 1.5
            elif fg >= 65: signal -= 0.5
        if change_24h > 5:   signal += 0.5
        elif change_24h < -5: signal -= 0.5
        signal = round(max(-5.0, min(5.0, signal)), 1)

        data = {
            "symbol":        sym,
            "is_trending":   is_trending,
            "fear_greed":    fg,
            "change_24h":    change_24h,
            "signal":        signal,
            "reddit_count":  reddit_count,
            "summary": (
                f"{sym}: {'TRENDING' if is_trending else 'not trending'} | "
                f"Reddit {reddit_count} mentions | F&G {fg} | "
                f"24h {change_24h:+.1f}% | signal {signal:+.1f}"
            ),
        }
        results[pair] = data
        signals.append(signal)

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

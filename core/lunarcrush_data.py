"""
Social Sentiment Module
========================
Free social data from Reddit public API + CoinGecko community stats.
LunarCrush paid tier not required.

Sources (both completely free, no API key):
  Reddit public API  — active users / subscribers per crypto subreddit
  CoinGecko          — reddit_accounts_active_48h, twitter_followers

Signal logic:
  High active_users / subscribers ratio = high engagement = bullish momentum
  Rising active users vs recent baseline = community heating up
"""

import time
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE: dict = {}
_CACHE_TTL = 300   # 5 minutes

# Map Kraken pairs to Reddit subreddits + CoinGecko IDs
_PAIR_META = {
    "XBTEUR":   {"subreddit": "Bitcoin",    "cg_id": "bitcoin"},
    "XXBTZEUR": {"subreddit": "Bitcoin",    "cg_id": "bitcoin"},
    "XETHZEUR": {"subreddit": "ethereum",   "cg_id": "ethereum"},
    "ETHEUR":   {"subreddit": "ethereum",   "cg_id": "ethereum"},
    "SOLEUR":   {"subreddit": "solana",     "cg_id": "solana"},
    "XXRPZEUR": {"subreddit": "Ripple",     "cg_id": "ripple"},
    "XRPEUR":   {"subreddit": "Ripple",     "cg_id": "ripple"},
    "ADAEUR":   {"subreddit": "cardano",    "cg_id": "cardano"},
    "DOTEUR":   {"subreddit": "dot",        "cg_id": "polkadot"},
    "LINKEUR":  {"subreddit": "LINKTrader", "cg_id": "chainlink"},
}


def _cached_get(url: str, headers: dict = None, timeout: int = 8) -> Optional[dict]:
    now = time.time()
    if url in _CACHE and now - _CACHE[url]["ts"] < _CACHE_TTL:
        return _CACHE[url]["data"]
    try:
        r = requests.get(
            url,
            headers={**(headers or {}), "User-Agent": "tradingbot/1.0"},
            timeout=timeout,
        )
        if r.status_code == 200:
            data = r.json()
            _CACHE[url] = {"data": data, "ts": now}
            return data
    except Exception as exc:
        logger.debug("Social fetch failed [%s]: %s", url, exc)
    return None


def get_reddit_stats(subreddit: str) -> dict:
    """Fetch subreddit active users and subscriber count — completely free."""
    data = _cached_get(f"https://www.reddit.com/r/{subreddit}/about.json")
    if not data:
        return {}
    d = data.get("data", {})
    subscribers  = int(d.get("subscribers", 0))
    active       = int(d.get("active_user_count", 0))
    # Engagement ratio: active/subscribers (higher = more community interest)
    ratio = (active / subscribers * 100) if subscribers > 0 else 0
    return {
        "subscribers":  subscribers,
        "active_users": active,
        "engagement_pct": round(ratio, 3),
    }


def get_coingecko_community(cg_id: str) -> dict:
    """Fetch community stats from CoinGecko — free, no key needed."""
    data = _cached_get(
        f"https://api.coingecko.com/api/v3/coins/{cg_id}",
        headers={"Accept": "application/json"},
    )
    if not data:
        return {}
    cd = data.get("community_data", {})
    return {
        "reddit_subscribers":      int(cd.get("reddit_subscribers", 0)),
        "reddit_active_48h":       int(cd.get("reddit_accounts_active_48h", 0)),
        "reddit_avg_posts_48h":    float(cd.get("reddit_average_posts_48h", 0)),
        "reddit_avg_comments_48h": float(cd.get("reddit_average_comments_48h", 0)),
        "twitter_followers":       int(cd.get("twitter_followers", 0)),
    }


def _compute_signal(reddit: dict, cg: dict) -> float:
    """
    Derive a -5 to +5 social signal from community activity metrics.
    High engagement ratio = community is active = mild bullish signal.
    """
    engagement = reddit.get("engagement_pct", 0)
    active_48h = cg.get("reddit_active_48h", 0)
    avg_posts  = cg.get("reddit_avg_posts_48h", 0)

    signal = 0.0

    # Engagement ratio signal (active/subscribers %)
    if engagement > 0.5:
        signal += 2.0
    elif engagement > 0.2:
        signal += 1.0
    elif engagement < 0.05:
        signal -= 1.0

    # Active accounts in last 48h (absolute volume)
    if active_48h > 5000:
        signal += 1.0
    elif active_48h > 2000:
        signal += 0.5

    # Post activity
    if avg_posts > 100:
        signal += 0.5

    return round(max(-5.0, min(5.0, signal)), 1)


def get_coin_sentiment(pair: str) -> dict:
    """Fetch social sentiment for a single trading pair."""
    meta = _PAIR_META.get(pair, {})
    if not meta:
        return {}

    reddit = get_reddit_stats(meta["subreddit"])
    cg     = get_coingecko_community(meta["cg_id"])
    signal = _compute_signal(reddit, cg)

    active  = reddit.get("active_users", 0)
    subs    = reddit.get("subscribers", 0)
    eng     = reddit.get("engagement_pct", 0)

    return {
        "subreddit":       meta["subreddit"],
        "subscribers":     subs,
        "active_users":    active,
        "engagement_pct":  eng,
        "reddit_active_48h": cg.get("reddit_active_48h", 0),
        "twitter_followers": cg.get("twitter_followers", 0),
        "signal":          signal,
        "summary": (
            f"r/{meta['subreddit']}: {active:,} active / {subs:,} subs "
            f"({eng:.2f}% engaged) | signal {signal:+.1f}"
        ),
    }


def fetch_all_sentiment(pairs: list) -> dict:
    """Fetch social sentiment for all traded pairs."""
    results = {}
    signals = []
    seen    = set()

    for pair in pairs:
        meta = _PAIR_META.get(pair, {})
        if not meta or meta.get("subreddit") in seen:
            continue
        seen.add(meta.get("subreddit"))

        data = get_coin_sentiment(pair)
        if data:
            results[pair] = data
            signals.append(data["signal"])

    if not results:
        return {"available": False}

    combined = round(sum(signals) / len(signals), 2) if signals else 0.0
    logger.info("Social sentiment: %d coins | combined=%.2f", len(results), combined)

    return {
        "available": True,
        "coins":     results,
        "combined":  combined,
        "summary":   f"Social combined: {combined:+.2f} ({len(results)} coins via Reddit/CoinGecko)",
    }

"""
New Listings Monitor
=====================
Polls Sharpe.ai for new spot listings on Kraken.
Tracks each new coin for 12 hours with a simple momentum strategy:

  - Detection: New listing found → send Telegram alert, record initial price
  - Entry:     Price rises 2%+ from detection price → BUY
  - Exit:      12 hours after BUY, force-sell regardless of P&L
  - Cleanup:   Remove from watchlist after 12h if never bought

Strategy rationale: new exchange listings often pump on retail FOMO in the
first hours, then retrace. We capture the pump and exit before the drawdown.

State is persisted to data/listing_watchlist.json so restarts don't lose
tracked listings.
"""

import os
import json
import re
import time
import logging
import requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Optional

logger = logging.getLogger(__name__)

_KRAKEN_BLOG_RSS  = "https://blog.kraken.com/feed"
_KRAKEN_ASSET_PAIRS = "https://api.kraken.com/0/public/AssetPairs"
_KNOWN_PAIRS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "known_kraken_pairs.json")

# Words that appear in ALLCAPS in blog titles but aren't coin symbols
_EXCLUDE_WORDS = {
    'EUR','USD','USDT','USDC','NOW','NEW','THE','FOR','AND','WITH',
    'ON','AT','IS','ARE','HAS','GET','ALL','ETF','API','DCA',
    'NFT','DAO','TVL','APY','APR','CEX','DEX','IPO','ICO','KYC',
    'AML','SOC','SEC','ETF','BTC','ETH','SOL','XRP','ADA','DOT',
}

_BASE_URL    = "https://www.sharpe.ai/api"
_STATE_FILE  = os.path.join(os.path.dirname(__file__), "..", "data", "listing_watchlist.json")
_CACHE_TTL   = 540   # 9 min cache so 10-min poll doesn't hammer the API


# ── Sharpe.ai fetch ────────────────────────────────────────────────────────────

def _api_key() -> str:
    return os.getenv("SHARPE_API_KEY", "")


def fetch_new_kraken_listings(hours_lookback: int = 48) -> list:
    """
    Return recent new spot listings on Kraken from Sharpe.ai.
    Each entry: {symbol, name, listed_at, kraken_pair}
    """
    key = _api_key()
    if not key:
        return []

    days = max(1, hours_lookback // 24 + 1)
    try:
        resp = requests.get(
            f"{_BASE_URL}/v1/listings/recent",
            params={
                "exchange":    "kraken",
                "event_type":  "listing",
                "market_type": "spot",
                "days":        days,
                "limit":       100,
            },
            headers={"Authorization": f"Bearer {key}", "User-Agent": "tradingbot/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.debug("Sharpe listings HTTP %d", resp.status_code)
            return []

        raw = resp.json().get("data", [])
        if not isinstance(raw, list):
            return []

        cutoff = time.time() - hours_lookback * 3600
        results = []

        for item in raw:
            # Try various date field names Sharpe.ai might use
            listed_str = (item.get("listed_at") or item.get("listing_date")
                          or item.get("date") or item.get("start_time") or "")
            if not listed_str:
                continue
            try:
                import datetime
                dt = datetime.datetime.fromisoformat(listed_str.replace("Z", "+00:00"))
                listed_ts = dt.timestamp()
            except Exception:
                continue

            if listed_ts < cutoff:
                continue

            symbol = (item.get("symbol") or item.get("coin") or item.get("base_coin") or "").upper()
            if not symbol or len(symbol) > 10:
                continue

            results.append({
                "symbol":      symbol,
                "name":        item.get("name", symbol),
                "listed_at":   listed_ts,
                # Try plain EUR first; bot will test variants at buy time
                "kraken_pair": symbol + "EUR",
                "pair_variants": [
                    symbol + "EUR",
                    "X" + symbol + "ZEUR",
                    "X" + symbol + "EUR",
                    symbol + "USD",        # e.g. NESUSD
                    "X" + symbol + "ZUSD", # e.g. XXBTZUSD (older Kraken format)
                    "X" + symbol + "USD",  # e.g. XNESUSD
                    symbol + "USDT",
                ],
            })

        logger.info("Sharpe.ai: %d new Kraken listing(s) in last %dh", len(results), hours_lookback)
        return results

    except Exception as exc:
        logger.debug("listings_monitor fetch failed: %s", exc)
        return []


# ── Kraken blog RSS ────────────────────────────────────────────────────────────

def fetch_kraken_blog_listings(hours_lookback: int = 48) -> list:
    """
    Parse the Kraken blog RSS feed for new listing announcements.
    Often published hours before the pair goes live — gives early warning.
    No API key needed.
    """
    try:
        resp = requests.get(
            _KRAKEN_BLOG_RSS,
            headers={"User-Agent": "tradingbot/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        root    = ET.fromstring(resp.text)
        channel = root.find("channel")
        if channel is None:
            return []

        cutoff   = time.time() - hours_lookback * 3600
        keywords = ["available", "listed", "trading", "launched", "now live",
                    "new listing", "now on kraken", "adds", "token launch"]
        results  = []

        for item in channel.findall("item"):
            title    = item.findtext("title", "")
            link     = item.findtext("link", "")
            pub_date = item.findtext("pubDate", "")

            try:
                listed_ts = parsedate_to_datetime(pub_date).timestamp()
            except Exception:
                continue
            if listed_ts < cutoff:
                continue
            if not any(kw in title.lower() for kw in keywords):
                continue

            # Extract symbol — parentheses first: "Bitcoin Cash (BCH)"
            symbols = re.findall(r"\(([A-Z]{2,10})\)", title)
            if not symbols:
                # fallback: ALLCAPS words not in exclude list
                symbols = [
                    w for w in re.findall(r"\b([A-Z]{3,8})\b", title)
                    if w not in _EXCLUDE_WORDS
                ]

            for symbol in symbols[:2]:
                results.append({
                    "symbol":       symbol,
                    "name":         title[:80],
                    "listed_at":    listed_ts,
                    "kraken_pair":  symbol + "EUR",
                    "source":       "kraken_blog",
                    "link":         link,
                    "pair_variants": [
                        symbol + "EUR",
                        "X" + symbol + "ZEUR",
                        "X" + symbol + "EUR",
                        symbol + "USD",    # Kraken USD pairs (e.g. NESUSD)
                        symbol + "USDT",
                    ],
                })

        logger.info("Kraken blog RSS: %d potential listing(s) found", len(results))
        return results

    except Exception as exc:
        logger.debug("Kraken blog RSS (listings) failed: %s", exc)
        return []


_HEADLINES_CACHE: dict = {"data": [], "ts": 0.0}
_HEADLINES_TTL = 4 * 3600   # 4 hours — Kraken blog posts rarely more than once a day


def fetch_kraken_blog_headlines(limit: int = 8) -> list:
    """
    Return the latest headlines from the Kraken blog RSS.
    Cached for 4 hours — Kraken rarely posts more than once a day.
    """
    if time.time() - _HEADLINES_CACHE["ts"] < _HEADLINES_TTL:
        return _HEADLINES_CACHE["data"][:limit]
    try:
        resp = requests.get(
            _KRAKEN_BLOG_RSS,
            headers={"User-Agent": "tradingbot/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        root    = ET.fromstring(resp.text)
        channel = root.find("channel")
        if channel is None:
            return []

        listing_keywords = ["available", "listed", "trading", "launched",
                            "now live", "new listing", "now on kraken", "adds"]
        results = []
        for item in channel.findall("item")[:limit]:
            title    = item.findtext("title", "").strip()
            link     = item.findtext("link", "").strip()
            pub_date = item.findtext("pubDate", "").strip()
            if not title:
                continue
            try:
                ts = parsedate_to_datetime(pub_date).timestamp()
            except Exception:
                ts = 0
            results.append({
                "title":      title,
                "link":       link,
                "ts":         ts,
                "is_listing": any(kw in title.lower() for kw in listing_keywords),
            })
        _HEADLINES_CACHE["data"] = results
        _HEADLINES_CACHE["ts"]   = time.time()
        return results[:limit]
    except Exception as exc:
        logger.debug("Kraken blog headlines failed: %s", exc)
        return []

    except Exception as exc:
        logger.debug("Kraken blog RSS failed: %s", exc)
        return []


# ── Kraken AssetPairs polling ──────────────────────────────────────────────────

def _load_known_pairs() -> set:
    try:
        if os.path.exists(_KNOWN_PAIRS_FILE):
            with open(_KNOWN_PAIRS_FILE, "r") as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()


def _save_known_pairs(pairs: set) -> None:
    try:
        os.makedirs(os.path.dirname(_KNOWN_PAIRS_FILE), exist_ok=True)
        with open(_KNOWN_PAIRS_FILE, "w") as f:
            json.dump(sorted(pairs), f)
    except Exception:
        pass


def fetch_kraken_new_pairs(hours_lookback: int = 48) -> list:
    """
    Poll Kraken's public AssetPairs endpoint and compare against stored list.
    Returns pairs that are NEW since last check — no API key, real-time.
    This is the most reliable source: detects the exact moment a pair is tradeable.
    """
    try:
        resp = requests.get(
            _KRAKEN_ASSET_PAIRS,
            headers={"User-Agent": "tradingbot/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []

        current = set(resp.json().get("result", {}).keys())
        known   = _load_known_pairs()

        if not known:
            # First run — just save the current list, no "new" pairs yet
            _save_known_pairs(current)
            logger.info("Kraken AssetPairs: baseline saved (%d pairs)", len(current))
            return []

        new_pairs = current - known
        if new_pairs:
            _save_known_pairs(current)
            logger.info("Kraken AssetPairs: %d NEW pair(s) detected: %s",
                        len(new_pairs), new_pairs)

        results = []
        now = time.time()
        for pair_name in new_pairs:
            # Only interested in EUR pairs
            if not (pair_name.endswith("EUR") or pair_name.endswith("ZEUR")):
                continue
            # Derive base symbol
            symbol = pair_name.replace("ZEUR", "").replace("EUR", "").lstrip("X")
            if not symbol or symbol in _EXCLUDE_WORDS:
                continue
            results.append({
                "symbol":       symbol,
                "name":         f"{symbol} (new Kraken listing)",
                "listed_at":    now,
                "kraken_pair":  pair_name,   # exact name, no guessing needed
                "source":       "kraken_assetpairs",
                "pair_variants": [pair_name],
            })

        return results

    except Exception as exc:
        logger.debug("Kraken AssetPairs poll failed: %s", exc)
        return []


# ── Watchlist state ────────────────────────────────────────────────────────────

def load_watchlist() -> dict:
    """Load listing watchlist — PostgreSQL first, JSON fallback."""
    try:
        from core.db_postgres import load_listing_watchlist as _pg_load
        pg_data = _pg_load()
        if pg_data is not None:
            return pg_data
    except Exception:
        pass
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_watchlist(watchlist: dict) -> None:
    """Persist listing watchlist to JSON file + PostgreSQL (dual-write)."""
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(watchlist, f, indent=2)
        os.replace(tmp, _STATE_FILE)
    except Exception as exc:
        logger.debug("save_watchlist JSON failed: %s", exc)
    try:
        from core.db_postgres import save_listing_watchlist as _pg_save
        _pg_save(watchlist)
    except Exception:
        pass


def add_to_watchlist(watchlist: dict, listing: dict, initial_price: float) -> bool:
    """
    Add a new listing to the watchlist if not already tracked.
    Returns True if newly added.
    """
    symbol = listing["symbol"]
    if symbol in watchlist:
        return False
    watchlist[symbol] = {
        "symbol":       symbol,
        "name":         listing.get("name", symbol),
        "kraken_pair":  listing["kraken_pair"],
        "listed_at":    listing["listed_at"],
        "detected_at":  time.time(),
        "initial_price":initial_price,
        "bought":       False,
        "buy_ts":       None,
        "buy_price":    None,
    }
    save_watchlist(watchlist)
    return True


def mark_bought(watchlist: dict, symbol: str, price: float) -> None:
    if symbol in watchlist:
        watchlist[symbol]["bought"]    = True
        watchlist[symbol]["buy_ts"]    = time.time()
        watchlist[symbol]["buy_price"] = price
        save_watchlist(watchlist)


def remove_from_watchlist(watchlist: dict, symbol: str) -> None:
    watchlist.pop(symbol, None)
    save_watchlist(watchlist)


# ── Trend check ────────────────────────────────────────────────────────────────

def is_trending_up(entry: dict, current_price: float,
                   threshold_pct: float = 2.0) -> bool:
    """
    Returns True when current price is threshold_pct% above the initial price
    recorded at detection time — indicating an upward trend after listing.
    """
    initial = entry.get("initial_price", 0)
    if not initial or initial <= 0:
        return False
    return current_price >= initial * (1 + threshold_pct / 100)


def is_expired(entry: dict, hold_hours: int = 12) -> bool:
    """Returns True if the 12-hour hold window has passed since the buy."""
    buy_ts = entry.get("buy_ts")
    if not buy_ts:
        # Never bought — expire if we're past the detection window
        detected = entry.get("detected_at", time.time())
        return (time.time() - detected) > hold_hours * 3600
    return (time.time() - buy_ts) > hold_hours * 3600


# ── CoinGecko pre-watchlist ────────────────────────────────────────────────────
# Monitors CoinGecko for newly listed coins, then polls Kraken AssetPairs for
# 24 hours to catch the moment a coin gets listed for trading.

_COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
_COINGECKO_KNOWN_FILE  = os.path.join(os.path.dirname(__file__), "..", "data", "coingecko_known.json")
_COINGECKO_PRE_FILE    = os.path.join(os.path.dirname(__file__), "..", "data", "coingecko_prewatchlist.json")
_PREWATCHLIST_TTL_HOURS = 24   # monitor each coin for 24h before giving up

_PAIR_VARIANTS_TMPL = lambda sym: [
    sym + "EUR",
    "X" + sym + "ZEUR",
    "X" + sym + "EUR",
    sym + "USD",
    "X" + sym + "ZUSD",
    "X" + sym + "USD",
    sym + "USDT",
]


def fetch_coingecko_new_coins(per_page: int = 100) -> list:
    """Poll CoinGecko for recently added coins (sorted by gecko_desc).

    Compares against a saved baseline of known coin IDs.
    Returns list of new {symbol, name, coingecko_id} dicts.
    No API key required — uses public free tier.
    """
    try:
        resp = requests.get(
            _COINGECKO_MARKETS_URL,
            params={
                "vs_currency": "usd",
                "order":       "gecko_desc",
                "per_page":    per_page,
                "page":        1,
                "sparkline":   "false",
            },
            timeout=10,
            headers={"User-Agent": "tradingbot/1.0"},
        )
        if resp.status_code != 200:
            logger.debug("CoinGecko markets HTTP %d", resp.status_code)
            return []

        coins = resp.json()
        if not isinstance(coins, list):
            return []

        # Load known IDs baseline
        known_ids: set = set()
        if os.path.exists(_COINGECKO_KNOWN_FILE):
            try:
                known_ids = set(json.load(open(_COINGECKO_KNOWN_FILE)))
            except Exception:
                pass

        current_ids = {c["id"] for c in coins if c.get("id")}

        # On first run — save baseline, return nothing
        if not known_ids:
            _save_coingecko_known(current_ids)
            logger.info("CoinGecko: baseline saved (%d coins)", len(current_ids))
            return []

        new_coins = []
        for coin in coins:
            cid = coin.get("id", "")
            if cid and cid not in known_ids:
                symbol = (coin.get("symbol") or "").upper()
                name   = coin.get("name", symbol)
                if symbol and 2 <= len(symbol) <= 6 and symbol not in _EXCLUDE_WORDS:
                    new_coins.append({
                        "coingecko_id": cid,
                        "symbol":       symbol,
                        "name":         name,
                    })

        # Update baseline with all current coins
        _save_coingecko_known(known_ids | current_ids)

        if new_coins:
            logger.info("CoinGecko: %d new coin(s) detected: %s",
                        len(new_coins), [c["symbol"] for c in new_coins])
        return new_coins

    except Exception as exc:
        logger.debug("CoinGecko fetch failed: %s", exc)
        return []


def _save_coingecko_known(ids: set) -> None:
    try:
        os.makedirs(os.path.dirname(_COINGECKO_KNOWN_FILE), exist_ok=True)
        with open(_COINGECKO_KNOWN_FILE, "w") as f:
            json.dump(list(ids), f)
    except Exception as exc:
        logger.debug("CoinGecko known save failed: %s", exc)


def load_prewatchlist() -> dict:
    """Load CoinGecko pre-watchlist from disk."""
    try:
        if os.path.exists(_COINGECKO_PRE_FILE):
            return json.load(open(_COINGECKO_PRE_FILE))
    except Exception:
        pass
    return {}


def save_prewatchlist(prewatchlist: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_COINGECKO_PRE_FILE), exist_ok=True)
        tmp = _COINGECKO_PRE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(prewatchlist, f, indent=2)
        os.replace(tmp, _COINGECKO_PRE_FILE)
    except Exception as exc:
        logger.debug("Prewatchlist save failed: %s", exc)


def add_to_prewatchlist(prewatchlist: dict, coin: dict) -> bool:
    """Add a CoinGecko coin to the pre-watchlist. Returns True if newly added."""
    symbol = coin["symbol"]
    if symbol in prewatchlist:
        return False
    prewatchlist[symbol] = {
        "symbol":       symbol,
        "name":         coin["name"],
        "coingecko_id": coin["coingecko_id"],
        "detected_at":  time.time(),
        "expires_at":   time.time() + _PREWATCHLIST_TTL_HOURS * 3600,
    }
    save_prewatchlist(prewatchlist)
    logger.info("CoinGecko pre-watchlist: added %s (%s) — monitoring Kraken for 24h",
                symbol, coin["name"])
    return True


def check_prewatchlist_on_kraken(prewatchlist: dict, api_client) -> list:
    """Check each pre-watched coin against Kraken AssetPairs.

    Returns list of {symbol, name, kraken_pair, pair_variants} for any coins
    that have appeared on Kraken since being added to the pre-watchlist.
    Removes expired entries (>24h) automatically.
    """
    now = time.time()
    found = []
    to_remove = []

    for symbol, entry in list(prewatchlist.items()):
        # Expire after 24h
        if now > entry.get("expires_at", now):
            logger.info("CoinGecko pre-watchlist: %s expired without Kraken listing", symbol)
            to_remove.append(symbol)
            continue

        # Try all pair variants against Kraken
        for variant in _PAIR_VARIANTS_TMPL(symbol):
            try:
                md = api_client.get_market_data(variant)
                if md:
                    key = next(iter(md), None)
                    if key:
                        price = float(md[key]["c"][0])
                        if price > 0:
                            logger.info(
                                "CoinGecko pre-watchlist: %s NOW LIVE on Kraken as %s @ %.6f",
                                symbol, variant, price,
                            )
                            found.append({
                                "symbol":       symbol,
                                "name":         entry["name"],
                                "kraken_pair":  variant,
                                "listed_at":    now,
                                "source":       "coingecko_prewatchlist",
                                "pair_variants": _PAIR_VARIANTS_TMPL(symbol),
                            })
                            to_remove.append(symbol)
                            break
            except Exception:
                continue

    for sym in set(to_remove):
        prewatchlist.pop(sym, None)
    if to_remove:
        save_prewatchlist(prewatchlist)

    return found

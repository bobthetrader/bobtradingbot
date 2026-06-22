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
import time
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

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
                "kraken_pair": symbol + "EUR",
            })

        logger.info("Sharpe.ai: %d new Kraken listing(s) in last %dh", len(results), hours_lookback)
        return results

    except Exception as exc:
        logger.debug("listings_monitor fetch failed: %s", exc)
        return []


# ── Watchlist state ────────────────────────────────────────────────────────────

def load_watchlist() -> dict:
    """Load listing watchlist from disk."""
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_watchlist(watchlist: dict) -> None:
    """Persist listing watchlist to disk."""
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(watchlist, f, indent=2)
        os.replace(tmp, _STATE_FILE)
    except Exception as exc:
        logger.debug("save_watchlist failed: %s", exc)


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

"""Kraken fee schedule sync — fetches live taker/maker tiers from the public API.

Writes data/kraken_fees.json once per day. Both the main bot and scalper read
from this file so fee assumptions stay current if Kraken changes their schedule.

Canonical pair used: XBTEUR. All EUR crypto pairs share the same fee tier table.
Fee volume currency is ZUSD (30-day rolling USD volume determines tier).
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_KRAKEN_PUBLIC = "https://api.kraken.com/0/public/AssetPairs"
_CANONICAL_PAIR = "XBTEUR"
_CACHE_SECONDS = 86_400   # refresh once per 24h
_TIMEOUT = 10

# Hardcoded fallback — Kraken base tier verified 2026-06-28
_FALLBACK = {
    "fetched_at": 0,
    "taker_tiers": [
        [0,           0.40],
        [10_000,      0.35],
        [50_000,      0.24],
        [100_000,     0.22],
        [250_000,     0.20],
        [500_000,     0.18],
        [1_000_000,   0.16],
        [2_500_000,   0.14],
        [5_000_000,   0.12],
        [10_000_000,  0.10],
        [100_000_000, 0.08],
    ],
    "maker_tiers": [
        [0,           0.25],
        [10_000,      0.20],
        [50_000,      0.14],
        [100_000,     0.12],
        [250_000,     0.10],
        [500_000,     0.08],
        [1_000_000,   0.06],
        [2_500_000,   0.04],
        [5_000_000,   0.02],
        [10_000_000,  0.00],
    ],
    "fee_volume_currency": "ZUSD",
    "source": "fallback",
}


def _fetch_from_kraken() -> Optional[dict]:
    try:
        resp = requests.get(
            _KRAKEN_PUBLIC,
            params={"pair": _CANONICAL_PAIR},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            logger.warning("fee_sync: Kraken API error: %s", data["error"])
            return None
        pair_info = next(iter(data["result"].values()))
        return {
            "fetched_at": time.time(),
            "taker_tiers": pair_info["fees"],
            "maker_tiers": pair_info["fees_maker"],
            "fee_volume_currency": pair_info.get("fee_volume_currency", "ZUSD"),
            "source": "kraken_api",
        }
    except Exception as exc:
        logger.warning("fee_sync: fetch failed: %s", exc)
        return None


# In-process cache so frequent callers (scalper loop) avoid repeated disk reads
_mem_cache: dict = {}
_mem_cache_ts: float = 0.0
_MEM_CACHE_TTL = 3_600   # re-read file at most once per hour


def load(data_dir: str = "data") -> dict:
    """Load fees from cache file, refreshing if stale or missing."""
    global _mem_cache, _mem_cache_ts

    now = time.time()
    if _mem_cache and (now - _mem_cache_ts) < _MEM_CACHE_TTL:
        return _mem_cache

    path = Path(data_dir) / "kraken_fees.json"
    cached = None

    if path.exists():
        try:
            cached = json.loads(path.read_text())
        except Exception:
            cached = None

    age = now - (cached or {}).get("fetched_at", 0)
    if cached and age < _CACHE_SECONDS:
        _mem_cache = cached
        _mem_cache_ts = now
        return cached

    fresh = _fetch_from_kraken()
    if fresh:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(fresh, indent=2))
            logger.info(
                "fee_sync: updated %s (taker base %.2f%%, maker base %.2f%%)",
                path,
                fresh["taker_tiers"][0][1],
                fresh["maker_tiers"][0][1],
            )
        except Exception as exc:
            logger.warning("fee_sync: could not write cache: %s", exc)
        _mem_cache = fresh
        _mem_cache_ts = now
        return fresh

    if cached:
        logger.warning("fee_sync: API unavailable — using cached fees (age %.0fh)", age / 3600)
        _mem_cache = cached
        _mem_cache_ts = now
        return cached

    logger.warning("fee_sync: API unavailable and no cache — using hardcoded fallback")
    _mem_cache = _FALLBACK
    _mem_cache_ts = now
    return _FALLBACK


def taker_for_volume(fees: dict, volume_usd: float) -> float:
    """Return taker fee % for the given 30-day USD volume."""
    tiers = fees.get("taker_tiers", _FALLBACK["taker_tiers"])
    rate = tiers[0][1]
    for threshold, fee in tiers:
        if volume_usd >= threshold:
            rate = fee
    return rate


def maker_for_volume(fees: dict, volume_usd: float) -> float:
    """Return maker fee % for the given 30-day USD volume."""
    tiers = fees.get("maker_tiers", _FALLBACK["maker_tiers"])
    rate = tiers[0][1]
    for threshold, fee in tiers:
        if volume_usd >= threshold:
            rate = fee
    return rate


def base_taker(fees: dict) -> float:
    """Worst-case taker fee % (lowest volume tier)."""
    return fees.get("taker_tiers", _FALLBACK["taker_tiers"])[0][1]


def base_maker(fees: dict) -> float:
    """Worst-case maker fee % (lowest volume tier)."""
    return fees.get("maker_tiers", _FALLBACK["maker_tiers"])[0][1]

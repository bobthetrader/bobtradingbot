"""Liquid EUR pair discovery — shared screener for widening a bot's trading universe.

Fetches all Kraken EUR spot pairs, excludes stablecoins / non-spot instruments,
ranks by 24h EUR volume, and returns the top-N altnames above a volume floor.

Used by the main bot's dynamic universe (config [bot_settings] dynamic_pairs) so it
can trade any *sufficiently liquid* pair its signal likes rather than a hardcoded
list. The volume floor is deliberately high so only liquid large/mid-caps
qualify — maker (post-only) fills need real depth.
"""

import logging

logger = logging.getLogger(__name__)

# Keywords in altname that flag a stablecoin or non-spot instrument to exclude.
_EXCLUDE_KEYWORDS = ("USD", "USDT", "USDC", "DAI", "BUSD", "TUSD", "FRAX",
                     "LUSD", "GUSD", "PYUSD", "EURT", "EURC", "STEUR", "EURR", "PAX")


def discover_liquid_eur_pairs(api, min_vol_eur: float = 500_000,
                              max_pairs: int = 30, chunk: int = 50) -> list:
    """Return up to ``max_pairs`` liquid EUR pair altnames, ranked by 24h EUR volume.

    ``api`` must expose ``get_asset_pairs()`` and ``get_ticker_batch(list)`` (the
    KrakenAPI wrapper does). Returns [] on any failure so the caller can no-op.
    """
    try:
        all_pairs = api.get_asset_pairs()
    except Exception as exc:
        logger.warning("[SCREEN] get_asset_pairs failed: %s", exc)
        return []
    if not all_pairs:
        return []

    # Online EUR spot pairs only, stablecoins excluded → altname -> official key
    eur_map = {}
    for official_key, info in all_pairs.items():
        if official_key.endswith(".d"):
            continue
        if info.get("status") != "online":
            continue
        if info.get("quote") not in ("ZEUR", "EUR"):
            continue
        altname = info.get("altname", official_key)
        if any(kw in altname.upper() for kw in _EXCLUDE_KEYWORDS):
            continue
        eur_map[altname] = official_key
    if not eur_map:
        return []

    rev = {v: k for k, v in eur_map.items()}   # official key -> altname
    altnames = list(eur_map.keys())
    volumes = {}                               # altname -> 24h EUR volume

    for i in range(0, len(altnames), chunk):
        batch = altnames[i:i + chunk]
        try:
            ticker = api.get_ticker_batch(batch)
        except Exception as exc:
            logger.debug("[SCREEN] ticker batch failed: %s", exc)
            continue
        if not ticker:
            continue
        for resp_key, tick in ticker.items():
            alt = rev.get(resp_key) or resp_key
            try:
                vol_base = float(tick["v"][1])   # 24h rolling base volume
                price    = float(tick["c"][0])   # last trade price
                volumes[alt] = vol_base * price
            except (KeyError, IndexError, ValueError, TypeError):
                pass

    qualified = [(a, v) for a, v in volumes.items() if v >= min_vol_eur]
    qualified.sort(key=lambda x: x[1], reverse=True)
    top = [a for a, _ in qualified[:max_pairs]]
    logger.info("[SCREEN] %d EUR pairs -> %d qualify (>=€%.0f/day) -> top %d",
                len(eur_map), len(qualified), min_vol_eur, len(top))
    return top

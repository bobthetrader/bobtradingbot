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
Set SHARPE_API_KEY in .env on the server.
"""

import os
import json
import time
import logging
import threading
import requests
from typing import Optional

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.sharpe.ai/api"
_CACHE: dict = {}
_CACHE_TTL = 540   # 9 minutes

_MONTHLY_LIMIT = 10_000   # per key
_QUOTA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "sharpe_quota.json")
_quota_lock = threading.Lock()


def _api_keys() -> list:
    """Return list of configured Sharpe.ai keys (supports up to 2 for quota rotation)."""
    keys = []
    for var in ("SHARPE_API_KEY", "SHARPE_API_KEY_2"):
        k = os.getenv(var, "").strip()
        if k:
            keys.append(k)
    return keys


def _get_key_for_request() -> str:
    """Pick whichever key has remaining quota this month, alternating between them."""
    keys = _api_keys()
    if not keys:
        return ""
    with _quota_lock:
        try:
            now = time.localtime()
            month_key = f"{now.tm_year}-{now.tm_mon:02d}"
            quota = {}
            if os.path.exists(_QUOTA_FILE):
                try:
                    quota = json.loads(open(_QUOTA_FILE).read())
                except Exception:
                    quota = {}
            if quota.get("month") != month_key:
                quota = {"month": month_key, "key0": 0, "key1": 0}
            # Pick key with fewest calls this month
            counts = [quota.get(f"key{i}", 0) for i in range(len(keys))]
            idx = counts.index(min(counts))
            if counts[idx] >= _MONTHLY_LIMIT:
                logger.warning("Sharpe.ai all keys exhausted (%s calls) — skipping", counts)
                return ""
            quota[f"key{idx}"] = counts[idx] + 1
            os.makedirs(os.path.dirname(_QUOTA_FILE), exist_ok=True)
            open(_QUOTA_FILE, "w").write(json.dumps(quota))
            return keys[idx]
        except Exception:
            return keys[0]  # fail open

_PAIR_TO_COIN = {
    "XBTEUR":   "BTC",
    "XXBTZEUR": "BTC",
    "ETHEUR":   "ETH",
    "XETHZEUR": "ETH",
    "SOLEUR":   "SOL",
    "XRPEUR":   "XRP",
    "XXRPZEUR": "XRP",
    "ADAEUR":   "ADA",
    "DOTEUR":   "DOT",
    "LINKEUR":  "LINK",
}


def _api_key() -> str:
    return _get_key_for_request()


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


# ── Binance funding rates (no API key required) ────────────────────────────────

_PAIR_TO_BINANCE = {
    "XBTEUR": "BTCUSDT", "XXBTZEUR": "BTCUSDT",
    "ETHEUR": "ETHUSDT", "XETHZEUR": "ETHUSDT",
    "SOLEUR": "SOLUSDT", "XRPEUR": "XRPUSDT", "XXRPZEUR": "XRPUSDT",
    "ADAEUR": "ADAUSDT", "DOTEUR": "DOTUSDT", "LINKEUR": "LINKUSDT",
}
_BINANCE_CACHE: dict = {}


def fetch_binance_funding(pairs: list) -> dict:
    """Fetch latest funding rates from Binance futures — no API key needed."""
    coins_map = {_PAIR_TO_BINANCE[p]: _PAIR_TO_COIN[p]
                 for p in pairs if p in _PAIR_TO_BINANCE and p in _PAIR_TO_COIN}
    coin_scores: dict = {}
    cache_key = "binance_funding"
    now = time.time()
    if cache_key in _BINANCE_CACHE and now - _BINANCE_CACHE[cache_key]["ts"] < _CACHE_TTL:
        return _BINANCE_CACHE[cache_key]["data"]
    try:
        for symbol, coin in coins_map.items():
            try:
                r = requests.get(
                    "https://fapi.binance.com/fapi/v1/fundingRate",
                    params={"symbol": symbol, "limit": 1}, timeout=8
                )
                if r.status_code == 200:
                    rows = r.json()
                    if rows:
                        rate = float(rows[0].get("fundingRate", 0))
                        coin_scores[coin] = _funding_score(rate, 8.0)
            except Exception:
                pass
        result = {"coin_scores": coin_scores,
                  "combined_score": round(sum(coin_scores.values()) / len(coin_scores), 2) if coin_scores else 0.0}
        _BINANCE_CACHE[cache_key] = {"data": result, "ts": now}
        logger.debug("Binance funding: %s", coin_scores)
        return result
    except Exception as exc:
        logger.debug("Binance funding fetch failed: %s", exc)
        return {"coin_scores": {}, "combined_score": 0.0}


# ── Bybit funding rates (no API key required) ──────────────────────────────────

_BYBIT_CACHE: dict = {}


def fetch_bybit_funding(pairs: list) -> dict:
    """Fetch latest funding rates from Bybit — no API key needed."""
    coins_map = {_PAIR_TO_BINANCE[p]: _PAIR_TO_COIN[p]
                 for p in pairs if p in _PAIR_TO_BINANCE and p in _PAIR_TO_COIN}
    coin_scores: dict = {}
    cache_key = "bybit_funding"
    now = time.time()
    if cache_key in _BYBIT_CACHE and now - _BYBIT_CACHE[cache_key]["ts"] < _CACHE_TTL:
        return _BYBIT_CACHE[cache_key]["data"]
    try:
        for symbol, coin in coins_map.items():
            try:
                r = requests.get(
                    "https://api.bybit.com/v5/market/funding/history",
                    params={"category": "linear", "symbol": symbol, "limit": 1}, timeout=8
                )
                if r.status_code == 200:
                    data = r.json()
                    rows = data.get("result", {}).get("list", [])
                    if rows:
                        rate = float(rows[0].get("fundingRate", 0))
                        coin_scores[coin] = _funding_score(rate, 8.0)
            except Exception:
                pass
        result = {"coin_scores": coin_scores,
                  "combined_score": round(sum(coin_scores.values()) / len(coin_scores), 2) if coin_scores else 0.0}
        _BYBIT_CACHE[cache_key] = {"data": result, "ts": now}
        logger.debug("Bybit funding: %s", coin_scores)
        return result
    except Exception as exc:
        logger.debug("Bybit funding fetch failed: %s", exc)
        return {"coin_scores": {}, "combined_score": 0.0}


# ── Combined fetch ─────────────────────────────────────────────────────────────

_WEIGHTS = {"sharpe": 0.40, "binance": 0.35, "bybit": 0.25}


def _merge_funding_scores(sharpe_scores: dict, binance_scores: dict, bybit_scores: dict) -> dict:
    """Merge per-coin scores from all sources using weighted average."""
    all_coins = set(sharpe_scores) | set(binance_scores) | set(bybit_scores)
    merged = {}
    for coin in all_coins:
        sources = []
        if coin in sharpe_scores:
            sources.append((sharpe_scores[coin], _WEIGHTS["sharpe"]))
        if coin in binance_scores:
            sources.append((binance_scores[coin], _WEIGHTS["binance"]))
        if coin in bybit_scores:
            sources.append((bybit_scores[coin], _WEIGHTS["bybit"]))
        if not sources:
            continue
        total_weight = sum(w for _, w in sources)
        merged[coin] = round(sum(s * w for s, w in sources) / total_weight, 2)
    return merged


def fetch_all(pairs: list) -> dict:
    # Binance + Bybit always run (free, no key)
    binance = fetch_binance_funding(pairs)
    bybit   = fetch_bybit_funding(pairs)

    # Sharpe.ai only runs when key available and quota remains
    sharpe_funding   = {"coin_scores": {}}
    sharpe_derivs    = {}
    sharpe_insider   = {"market_signal": 0, "top_flagged": [], "summary": ""}
    sharpe_news      = []
    key = _api_key()
    if key:
        try:
            sharpe_funding  = get_funding_data(pairs)
            sharpe_derivs   = get_derivatives_overview()
            sharpe_insider  = get_insider_selling(pairs)
            sharpe_news     = get_news()
        except Exception as exc:
            logger.debug("Sharpe.ai fetch failed: %s", exc)

    # Merge funding scores from all available sources
    merged_scores = _merge_funding_scores(
        sharpe_funding.get("coin_scores", {}),
        binance.get("coin_scores", {}),
        bybit.get("coin_scores", {}),
    )
    combined_score = round(sum(merged_scores.values()) / len(merged_scores), 2) if merged_scores else 0.0

    sources_active = ["Binance", "Bybit"] + (["Sharpe.ai"] if key else [])
    logger.info(
        "Funding data: sources=%s scores=%s insider_signal=%s",
        "+".join(sources_active), merged_scores,
        sharpe_insider.get("market_signal", 0),
    )

    merged_funding = {
        "coin_scores":    merged_scores,
        "combined_score": combined_score,
        "summary":        f"Sources: {', '.join(sources_active)}",
    }

    return {
        "funding":    merged_funding,
        "derivatives":sharpe_derivs,
        "insider":    sharpe_insider,
        "news":       sharpe_news,
        "available":  True,
    }

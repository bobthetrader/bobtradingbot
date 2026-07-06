"""Aggregate-market-data provider failover + performance scoreboard.

Routes the two data shapes that BOTH CoinGecko (free, keyless) and
CoinMarketCap (free tier, key via env COINMARKETCAP_API_KEY) can serve:

  * get_global_metrics()      — BTC dominance, global mcap 24h change
  * get_24h_changes(cg_ids)   — per-coin 24h % change

Behaviour:
  * Providers are tried in order (env API_PROVIDER_ORDER, default
    "coingecko,coinmarketcap"); first success wins, failures fall through.
  * Results are TTL-cached (600s) so total request volume stays the same as
    the old direct calls.
  * SHADOW SAMPLING: every Nth routed call (env API_SHADOW_EVERY, default 10,
    0=off) the non-primary provider is also queried with the same request and
    the result discarded — this is what generates comparable latency/error
    stats for the scoreboard even while the primary is healthy.
  * Every call (live or shadow) records latency + outcome to
    data/api_provider_stats.json. A scoreboard line is logged every 6h; run
    `py scripts/api_scoreboard.py` for the on-demand report.

Cost model: CoinGecko free = 0 credits but hard rate limits; CMC free tier =
10k credits/month (~333/day), 1 credit per call (quotes: 1 per 100 symbols).
"""
from __future__ import annotations
import json
import logging
import os
import threading
import time
from collections import deque
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_STATS_PATH = os.path.join(_DATA_DIR, "api_provider_stats.json")

_CACHE_TTL = 600          # matches the old _cached_get behaviour
_SCOREBOARD_EVERY_S = 6 * 3600
_LAT_WINDOW = 200         # latencies kept per provider/endpoint

# CoinGecko id -> CMC symbol for the coins this bot touches. CMC quotes are
# symbol-keyed; ids not listed here simply can't be served by CMC (the router
# then falls back / reports partial).
_CG_ID_TO_SYMBOL = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "ripple": "XRP",
    "cardano": "ADA", "polkadot": "DOT", "chainlink": "LINK",
    "litecoin": "LTC", "dogecoin": "DOGE", "avalanche-2": "AVAX",
    "stellar": "XLM", "uniswap": "UNI", "aave": "AAVE", "near": "NEAR",
    "sui": "SUI", "worldcoin-wld": "WLD", "bittensor": "TAO",
    "hyperliquid": "HYPE", "ondo-finance": "ONDO", "jupiter-exchange-solana": "JUP",
    "bitcoin-cash": "BCH", "tron": "TRX", "polygon-ecosystem-token": "POL",
    "hedera-hashgraph": "HBAR", "internet-computer": "ICP",
}


class _Stats:
    """Rolling per-provider/endpoint stats, persisted to JSON."""

    def __init__(self):
        self._lock = threading.Lock()
        self._d: Dict[str, dict] = {}
        self._dirty = 0
        self._last_scoreboard = 0.0
        self._load()

    def _load(self):
        try:
            with open(_STATS_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for k, v in raw.items():
                v["lat"] = deque(v.get("lat", []), maxlen=_LAT_WINDOW)
                self._d[k] = v
        except Exception:
            self._d = {}

    def _save(self):
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            out = {}
            for k, v in self._d.items():
                o = dict(v)
                o["lat"] = list(v["lat"])
                out[k] = o
            tmp = _STATS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f)
            os.replace(tmp, _STATS_PATH)
        except Exception as exc:
            logger.debug("api stats save failed: %s", exc)

    def record(self, provider: str, endpoint: str, ms: float, ok: bool,
               shadow: bool, err: str = ""):
        key = f"{provider}:{endpoint}"
        with self._lock:
            s = self._d.setdefault(key, {
                "calls": 0, "fails": 0, "shadow_calls": 0,
                "lat": deque(maxlen=_LAT_WINDOW), "last_error": "", "last_ts": 0,
            })
            s["calls"] += 1
            if shadow:
                s["shadow_calls"] += 1
            if ok:
                s["lat"].append(round(ms, 1))
            else:
                s["fails"] += 1
                s["last_error"] = str(err)[:200]
            s["last_ts"] = int(time.time())
            self._dirty += 1
            if self._dirty >= 10:
                self._dirty = 0
                self._save()
        now = time.time()
        if now - self._last_scoreboard >= _SCOREBOARD_EVERY_S:
            self._last_scoreboard = now
            logger.info("API scoreboard:\n%s", scoreboard_text())

    def snapshot(self) -> Dict[str, dict]:
        with self._lock:
            return {k: {**v, "lat": list(v["lat"])} for k, v in self._d.items()}


_stats = _Stats()
_cache: Dict[str, dict] = {}
_cache_lock = threading.Lock()
_call_counter = {"n": 0}


def _cached(key: str) -> Optional[dict]:
    with _cache_lock:
        e = _cache.get(key)
        if e and time.time() - e["ts"] < _CACHE_TTL:
            return e["data"]
    return None


def _store(key: str, data: dict):
    with _cache_lock:
        _cache[key] = {"data": data, "ts": time.time()}


def _cmc_key() -> str:
    return os.environ.get("COINMARKETCAP_API_KEY", "").strip()


def _provider_order() -> List[str]:
    raw = os.environ.get("API_PROVIDER_ORDER", "coingecko,coinmarketcap")
    order = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return [p for p in order if p in ("coingecko", "coinmarketcap")] or ["coingecko"]


def _available(provider: str) -> bool:
    return provider != "coinmarketcap" or bool(_cmc_key())


def _timed(provider: str, endpoint: str, shadow: bool, fn):
    t0 = time.time()
    try:
        out = fn()
        _stats.record(provider, endpoint, (time.time() - t0) * 1000, True, shadow)
        return out
    except Exception as exc:
        _stats.record(provider, endpoint, (time.time() - t0) * 1000, False, shadow, err=repr(exc))
        if not shadow:
            logger.debug("%s %s failed: %s", provider, endpoint, exc)
        raise


# ── provider implementations ──────────────────────────────────────────────────

def _cg_global() -> dict:
    r = requests.get("https://api.coingecko.com/api/v3/global", timeout=8,
                     headers={"User-Agent": "tradingbot/1.0"})
    r.raise_for_status()
    d = r.json()["data"]
    return {
        "btc_dominance_pct": round(float(d["market_cap_percentage"]["btc"]), 1),
        "mcap_change_24h_pct": round(float(d.get("market_cap_change_percentage_24h_usd") or 0), 2),
        "active_cryptos": d.get("active_cryptocurrencies", "?"),
        "provider": "coingecko",
    }


def _cmc_global() -> dict:
    r = requests.get("https://pro-api.coinmarketcap.com/v1/global-metrics/quotes/latest",
                     timeout=8, headers={"X-CMC_PRO_API_KEY": _cmc_key(),
                                         "User-Agent": "tradingbot/1.0"})
    r.raise_for_status()
    d = r.json()["data"]
    return {
        "btc_dominance_pct": round(float(d.get("btc_dominance") or 0), 1),
        "mcap_change_24h_pct": round(float(
            d.get("quote", {}).get("USD", {}).get("total_market_cap_yesterday_percentage_change") or 0), 2),
        "active_cryptos": d.get("active_cryptocurrencies", "?"),
        "provider": "coinmarketcap",
    }


def _cg_changes(cg_ids: List[str], vs: str) -> Dict[str, float]:
    r = requests.get("https://api.coingecko.com/api/v3/simple/price", timeout=8,
                     params={"ids": ",".join(sorted(set(cg_ids))), "vs_currencies": vs,
                             "include_24hr_change": "true"},
                     headers={"User-Agent": "tradingbot/1.0"})
    r.raise_for_status()
    data = r.json()
    return {cid: float(data.get(cid, {}).get(f"{vs}_24h_change") or 0) for cid in cg_ids}


def _cmc_changes(cg_ids: List[str], vs: str) -> Dict[str, float]:
    sym_map = {cid: _CG_ID_TO_SYMBOL[cid] for cid in cg_ids if cid in _CG_ID_TO_SYMBOL}
    if not sym_map:
        raise ValueError("no cg_id->symbol mapping for requested ids")
    convert = vs.upper()
    r = requests.get("https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest",
                     timeout=8,
                     params={"symbol": ",".join(sorted(set(sym_map.values()))), "convert": convert},
                     headers={"X-CMC_PRO_API_KEY": _cmc_key(), "User-Agent": "tradingbot/1.0"})
    r.raise_for_status()
    data = r.json()["data"]
    out: Dict[str, float] = {}
    for cid, sym in sym_map.items():
        try:
            entry = data[sym][0] if isinstance(data.get(sym), list) else data.get(sym, {})
            out[cid] = float(entry["quote"][convert]["percent_change_24h"] or 0)
        except Exception:
            out[cid] = 0.0
    return out


_IMPL = {
    "coingecko": {"global": _cg_global, "changes": _cg_changes},
    "coinmarketcap": {"global": _cmc_global, "changes": _cmc_changes},
}


# ── router ─────────────────────────────────────────────────────────────────────

def _shadow_sample(endpoint: str, primary_used: str, args=(),):
    """Every Nth call, exercise the OTHER provider(s) so the scoreboard has
    comparable data even while the primary is healthy. Result discarded."""
    every = int(os.environ.get("API_SHADOW_EVERY", "10") or 0)
    if every <= 0:
        return
    _call_counter["n"] += 1
    if _call_counter["n"] % every:
        return
    for p in _provider_order():
        if p == primary_used or not _available(p):
            continue

        def _run(p=p):
            try:
                _timed(p, endpoint, True, lambda: _IMPL[p][endpoint](*args))
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True, name=f"shadow-{p}").start()


def get_global_metrics() -> Optional[dict]:
    """BTC dominance / global mcap 24h change, provider-failover + cached."""
    hit = _cached("global")
    if hit is not None:
        return hit
    for p in _provider_order():
        if not _available(p):
            continue
        try:
            out = _timed(p, "global", False, _IMPL[p]["global"])
            _store("global", out)
            _shadow_sample("global", p)
            return out
        except Exception:
            continue
    return None


def get_24h_changes(cg_ids: List[str], vs: str = "eur") -> Dict[str, float]:
    """Per-coin 24h % change keyed by CoinGecko id, failover + cached."""
    if not cg_ids:
        return {}
    key = f"changes:{vs}:{','.join(sorted(set(cg_ids)))}"
    hit = _cached(key)
    if hit is not None:
        return hit
    for p in _provider_order():
        if not _available(p):
            continue
        try:
            out = _timed(p, "changes", False, lambda p=p: _IMPL[p]["changes"](cg_ids, vs))
            _store(key, out)
            _shadow_sample("changes", p, args=(cg_ids, vs))
            return out
        except Exception:
            continue
    return {}


# ── scoreboard ────────────────────────────────────────────────────────────────

def _pctile(vals: List[float], q: float) -> float:
    if not vals:
        return 0.0
    v = sorted(vals)
    return v[min(len(v) - 1, int(q * len(v)))]


def scoreboard_text() -> str:
    snap = _stats.snapshot()
    if not snap:
        return "no provider stats yet"
    by_provider: Dict[str, dict] = {}
    for key, s in snap.items():
        prov, _, ep = key.partition(":")
        agg = by_provider.setdefault(prov, {"calls": 0, "fails": 0, "lat": [], "shadow": 0})
        agg["calls"] += s["calls"]
        agg["fails"] += s["fails"]
        agg["shadow"] += s.get("shadow_calls", 0)
        agg["lat"].extend(s["lat"])
    lines = [f"  {'provider':<14} {'calls':>6} {'ok%':>6} {'p50ms':>7} {'p90ms':>7} {'cost':>16}"]
    best = None
    for prov, a in sorted(by_provider.items()):
        ok_pct = 100.0 * (a["calls"] - a["fails"]) / a["calls"] if a["calls"] else 0.0
        p50, p90 = _pctile(a["lat"], 0.50), _pctile(a["lat"], 0.90)
        cost = "free (rate-limited)" if prov == "coingecko" else f"~{a['calls']} credits"
        lines.append(f"  {prov:<14} {a['calls']:>6} {ok_pct:>5.1f}% {p50:>7.0f} {p90:>7.0f} {cost:>16}")
        if a["calls"] >= 10 and ok_pct >= 99.0 and (best is None or p50 < best[1]):
            best = (prov, p50)
    if best:
        lines.append(f"  => best (speed, >=99% ok, n>=10): {best[0]} (p50 {best[1]:.0f}ms)")
    else:
        lines.append("  => not enough clean data yet for a verdict (need >=10 calls at >=99% ok)")
    return "\n".join(lines)

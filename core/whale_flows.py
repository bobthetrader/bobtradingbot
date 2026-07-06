"""Whale exchange-flow signal (market-level smart money).

Fresh component: large transfers to/from known exchange hot wallets on
Ethereum via Alchemy `alchemy_getAssetTransfers` (existing ALCHEMY_API_KEY),
window = last ~60 minutes:
  ETH  -> exchange : sell pressure (bearish)
  ETH  <- exchange : accumulation (bullish)
  USDT/USDC -> exchange : dry powder arriving (bullish, half weight)

Slow component: CoinMetrics daily BTC+ETH exchange flows (existing
core.onchain_data helper) as trend context.

Output: get_whale_score() in [-5, +5]; 0.0 = neutral / no data. Failures are
always neutral — this module must never be the reason the bot cannot trade.
"""
from __future__ import annotations
import logging
import os
import threading
import time
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

_ALCHEMY_URL = "https://eth-mainnet.g.alchemy.com/v2/{key}"
_USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
_USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

# Known exchange hot/deposit wallets (publicly tagged, stable for years).
# Overridable via [smart_money] exchange_wallets in config.
DEFAULT_EXCHANGE_WALLETS = [
    "0x28C6c06298d514Db089934071355E5743bf21d60",  # Binance 14
    "0x21a31Ee1afC51d94C2eFcCAa2092aD1028285549",  # Binance 15
    "0xDFd5293D8e347dFe59E90eFd55b2956a1343963d",  # Binance 16
    "0x503828976D22510aad0201ac7EC88293211D23Da",  # Coinbase 4
    "0xddfAbCdc4D8FfC6d5beaf154f18B778f892A0740",  # Coinbase 5
    "0x3cD751E6b0078Be393132286c442345e5DC49699",  # Coinbase 6
    "0x267be1C1D684F78cb4F6a176C4911b741E4Ffdc0",  # Kraken 4
    "0xFa52274DD61E1643d2205169732f29114BC240b3",  # Kraken 6
    "0x6cC5F688a315f3dC28A7781717a9A798a59fDA7b",  # OKX
    "0xf89d7b9c864f589bbF53a82105107622B35EaA40",  # Bybit hot
]

_CACHE_TTL = 1800
_cache = {"ts": 0.0, "score": 0.0}
_lock = threading.Lock()


# ── pure scoring maths (unit-tested) ──────────────────────────────────────────

def score_fresh_flows(eth_in: float, eth_out: float, stable_in_usd: float,
                      min_activity_eth: float) -> float:
    """Score last-hour whale flows. eth_in = ETH moved TO exchanges (bearish),
    eth_out = ETH moved FROM exchanges (bullish), stable_in_usd = stables
    moved TO exchanges (bullish buying power, half weight)."""
    total = eth_in + eth_out
    if total < max(min_activity_eth, 1e-9):
        flow = 0.0  # too little whale activity to judge
    else:
        ratio = eth_out / max(eth_in, 1e-9)
        if ratio >= 2.0:
            flow = 3.0
        elif ratio >= 1.3:
            flow = 1.5
        elif ratio <= 0.5:
            flow = -3.0
        elif ratio <= 0.77:
            flow = -1.5
        else:
            flow = 0.0

    stable_bonus = 0.0
    if stable_in_usd >= 5_000_000:
        stable_bonus = 2.0
    elif stable_in_usd >= 1_000_000:
        stable_bonus = 1.0

    return max(-5.0, min(5.0, flow + stable_bonus))


def blend_scores(fresh: Optional[float], daily: Optional[float]) -> float:
    """0.6 x fresh + 0.4 x daily; missing components drop out; none -> 0.0."""
    if fresh is None and daily is None:
        return 0.0
    if fresh is None:
        return max(-5.0, min(5.0, daily))
    if daily is None:
        return max(-5.0, min(5.0, fresh))
    return max(-5.0, min(5.0, 0.6 * fresh + 0.4 * daily))


# ── network fetchers (thin, failure = None) ───────────────────────────────────

def _alchemy_rpc(method: str, params: list) -> Optional[dict]:
    key = os.getenv("ALCHEMY_API_KEY", "")
    if not key:
        return None
    try:
        r = requests.post(_ALCHEMY_URL.format(key=key),
                          json={"jsonrpc": "2.0", "id": 1,
                                "method": method, "params": params},
                          timeout=10)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as exc:
        logger.debug("alchemy rpc %s failed: %s", method, exc)
        return None


def _fetch_transfers(wallets: List[str], direction: str,
                     from_block_hex: str) -> Optional[list]:
    """direction 'in' = toAddress (into exchange), 'out' = fromAddress."""
    out = []
    addr_key = "toAddress" if direction == "in" else "fromAddress"
    for w in wallets:
        res = _alchemy_rpc("alchemy_getAssetTransfers", [{
            "fromBlock": from_block_hex, "toBlock": "latest",
            addr_key: w, "category": ["external", "erc20"],
            "maxCount": "0x3e8", "excludeZeroValue": True,
        }])
        if res is None:
            return None  # any failure -> whole fresh component neutral
        if "pageKey" in res:
            # Response was truncated at maxCount (1000) and Alchemy returned a
            # pageKey for the next page. We deliberately don't paginate: the
            # newest transfers within the window would be silently dropped
            # from `out`, biasing the in/out ratio the score is built on.
            # Treat as unavailable so this fetch makes the fresh component
            # neutral rather than wrong.
            return None
        out.extend(res.get("transfers", []))
    return out


def _sum_flows(transfers: list, min_whale_eth: float,
               min_whale_stable_usd: float) -> dict:
    eth = 0.0
    stable = 0.0
    for t in transfers:
        try:
            val = float(t.get("value") or 0)
            asset = (t.get("asset") or "").upper()
            addr = (t.get("rawContract", {}).get("address") or "").lower()
        except Exception:
            continue
        if asset == "ETH" and val >= min_whale_eth:
            eth += val
        elif (asset in ("USDT", "USDC") or addr in (_USDT, _USDC)) and val >= min_whale_stable_usd:
            stable += val
    return {"eth": eth, "stable_usd": stable}


def _fresh_score(cfg: dict) -> Optional[float]:
    wallets = cfg.get("exchange_wallets") or DEFAULT_EXCHANGE_WALLETS
    min_eth = float(cfg.get("min_whale_eth", 250.0))
    min_stable = float(cfg.get("min_whale_stable_usd", 500_000.0))

    blk_hex = _alchemy_rpc("eth_blockNumber", [])
    if not blk_hex:
        return None
    from_block = hex(max(0, int(blk_hex, 16) - 300))  # ~60 min of blocks

    t_in = _fetch_transfers(wallets, "in", from_block)
    t_out = _fetch_transfers(wallets, "out", from_block)
    if t_in is None or t_out is None:
        return None

    fin = _sum_flows(t_in, min_eth, min_stable)
    fout = _sum_flows(t_out, min_eth, min_stable)
    score = score_fresh_flows(eth_in=fin["eth"], eth_out=fout["eth"],
                              stable_in_usd=fin["stable_usd"],
                              min_activity_eth=2 * min_eth)
    logger.info("Whale flows fresh: in=%.0f ETH out=%.0f ETH stable_in=$%.0f -> %.2f",
                fin["eth"], fout["eth"], fin["stable_usd"], score)
    return score


def _daily_score() -> Optional[float]:
    try:
        from core.onchain_data import get_coinmetrics_exchange_flows
        sigs = []
        for asset in ("btc", "eth"):
            d = get_coinmetrics_exchange_flows(asset)
            if d.get("flow_signal") is not None:
                sigs.append(float(d["flow_signal"]))
        if not sigs:
            return None
        return max(-5.0, min(5.0, (sum(sigs) / len(sigs)) * 2.5))  # [-2,2] -> [-5,5]
    except Exception as exc:
        logger.debug("daily flow score failed: %s", exc)
        return None


def get_whale_score(cfg: dict = None) -> float:
    """Blended whale score in [-5, +5]; TTL-cached; 0.0 on any total failure."""
    with _lock:
        if time.time() - _cache["ts"] < _CACHE_TTL:
            return _cache["score"]
    try:
        fresh, daily = _fresh_score(cfg or {}), _daily_score()
        if fresh is None and daily is None:
            # Rate-limited naturally: this branch only runs once per
            # _CACHE_TTL (the fresh-cache check above short-circuits
            # otherwise), so this fires at most once per 30 min.
            logger.warning("whale flows: no data from any source")
        score = blend_scores(fresh, daily)
    except Exception as exc:
        logger.debug("whale score failed: %s", exc)
        score = 0.0
    with _lock:
        _cache["ts"] = time.time()
        _cache["score"] = score
    return score

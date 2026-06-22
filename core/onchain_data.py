"""
On-Chain Data Module
=====================
Fetches blockchain network metrics that predict short-term price moves.

Key insight: on-chain data leads price by 1-24 hours because it shows
REAL money moving before it hits order books.

Sources (all free, no key required for basic tier):
  - Blockchain.info   BTC network stats (mempool, volume, hash rate)
  - Alternative.me    Fear & Greed index (already in market_intelligence,
                      repeated here for on-chain context)

Optional (add key to .env to activate):
  - GLASSNODE_API_KEY  Exchange inflows/outflows, SOPR, NUPL
  - ETHERSCAN_API_KEY  ETH gas price, transaction count

Signal logic:
  HIGH mempool fees   → network congested → people urgently paying to transact → bullish
  HIGH BTC tx volume  → active market     → momentum/bullish bias
  HIGH exchange inflow → coins moving TO exchange → likely selling pressure → bearish
  HIGH exchange outflow→ coins moving FROM exchange → likely accumulation → bullish
  SOPR > 1           → holders in profit selling → mild bearish (distribution)
  SOPR < 1           → holders selling at loss → capitulation → bullish reversal
"""

import os
import time
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE: dict = {}
_CACHE_TTL  = 300   # 5 minutes

_BTC_STATS_URL  = "https://api.blockchain.info/stats"
_BTC_MEMPOOL_URL= "https://api.blockchain.info/mempool?format=json"
_ETHERSCAN_URL  = "https://api.etherscan.io/api"
_GLASSNODE_URL  = "https://api.glassnode.com/v1/metrics"


def _cached_get(url: str, params: dict = None, timeout: int = 8) -> Optional[dict]:
    cache_key = f"{url}:{str(sorted((params or {}).items()))}"
    now = time.time()
    if cache_key in _CACHE and now - _CACHE[cache_key]["ts"] < _CACHE_TTL:
        return _CACHE[cache_key]["data"]
    try:
        r = requests.get(url, params=params or {},
                         headers={"User-Agent": "tradingbot/1.0"}, timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            _CACHE[cache_key] = {"data": data, "ts": now}
            return data
    except Exception as exc:
        logger.debug("on-chain fetch failed [%s]: %s", url, exc)
    return None


# ── BTC on-chain (Blockchain.info — free, no key) ─────────────────────────────

def get_btc_network_stats() -> dict:
    """
    BTC network statistics from Blockchain.info.
    Returns metrics useful for trading signal generation.
    """
    data = _cached_get(_BTC_STATS_URL)
    if not data:
        return {}

    n_tx      = int(data.get("n_tx", 0))               # transactions in last 24h
    total_vol = float(data.get("estimated_btc_sent", 0)) # BTC volume (satoshis)
    hash_rate = float(data.get("hash_rate", 0))
    diff      = float(data.get("difficulty", 0))
    mempool   = int(data.get("mempool_size", 0))         # unconfirmed tx count

    # Transaction volume signal: high volume = active market = mild bullish
    # Normalise: >300k tx/day is high, <150k is low
    tx_signal = 0.0
    if n_tx > 350000:
        tx_signal = 2.0    # very active
    elif n_tx > 250000:
        tx_signal = 1.0    # active
    elif n_tx < 150000:
        tx_signal = -1.0   # quiet (often precedes drops)

    # Mempool signal: large mempool = people urgently paying fees = demand
    mempool_signal = 0.0
    if mempool > 50000:
        mempool_signal = 1.5
    elif mempool > 20000:
        mempool_signal = 0.5
    elif mempool < 5000:
        mempool_signal = -0.5

    combined = round((tx_signal + mempool_signal) / 2, 2)

    return {
        "n_tx_24h":      n_tx,
        "mempool_size":  mempool,
        "hash_rate":     round(hash_rate / 1e18, 2),  # EH/s
        "tx_signal":     tx_signal,
        "mempool_signal":mempool_signal,
        "combined_score":combined,
        "summary": (
            f"BTC on-chain: {n_tx:,} tx/24h | "
            f"mempool {mempool:,} | "
            f"hash {round(hash_rate/1e18,1)} EH/s | "
            f"signal {combined:+.1f}"
        ),
    }


# ── ETH on-chain (Etherscan — free tier, needs ETHERSCAN_API_KEY) ─────────────

def get_eth_gas_stats() -> dict:
    """
    ETH gas price from Etherscan. High gas = DeFi active = ETH demand = bullish.
    Requires ETHERSCAN_API_KEY in .env (free registration at etherscan.io).
    """
    key = os.getenv("ETHERSCAN_API_KEY", "")
    if not key:
        return {}

    data = _cached_get(_ETHERSCAN_URL, params={
        "module": "gastracker",
        "action": "gasoracle",
        "apikey": key,
    })
    if not data or data.get("status") != "1":
        return {}

    result    = data.get("result", {})
    fast_gwei = float(result.get("FastGasPrice", 0))
    base_gwei = float(result.get("suggestBaseFee", 0))

    # Gas signal: high gas = network demand = ETH bullish
    gas_signal = 0.0
    if fast_gwei > 50:
        gas_signal = 2.0   # very high demand
    elif fast_gwei > 20:
        gas_signal = 1.0
    elif fast_gwei < 5:
        gas_signal = -1.0  # very low demand

    return {
        "fast_gas_gwei": fast_gwei,
        "base_fee_gwei": base_gwei,
        "gas_signal":    gas_signal,
        "summary":       f"ETH gas: {fast_gwei:.0f} gwei (fast) | signal {gas_signal:+.1f}",
    }


# ── Glassnode (optional — add GLASSNODE_API_KEY to unlock) ────────────────────

def get_glassnode_exchange_flows(asset: str = "BTC") -> dict:
    """
    Exchange inflow/outflow from Glassnode.
    High inflow = coins heading TO exchanges = selling pressure = bearish.
    High outflow = coins leaving exchanges = accumulation = bullish.

    Requires GLASSNODE_API_KEY (free at glassnode.com).
    """
    key = os.getenv("GLASSNODE_API_KEY", "")
    if not key:
        return {}

    params = {"a": asset, "api_key": key, "i": "24h"}

    inflow  = _cached_get(f"{_GLASSNODE_URL}/transactions/transfers_volume_to_exchanges_sum", params)
    outflow = _cached_get(f"{_GLASSNODE_URL}/transactions/transfers_volume_from_exchanges_sum", params)

    if not inflow or not outflow:
        return {}

    # Glassnode returns list of {t: timestamp, v: value}
    try:
        in_val  = float(inflow[-1]["v"])  if isinstance(inflow,  list) else 0
        out_val = float(outflow[-1]["v"]) if isinstance(outflow, list) else 0
        net     = out_val - in_val        # positive = net outflow = bullish
        ratio   = out_val / max(in_val, 1)

        flow_signal = 0.0
        if ratio > 1.5:
            flow_signal = 2.0    # strong net outflow = accumulation
        elif ratio > 1.1:
            flow_signal = 1.0
        elif ratio < 0.7:
            flow_signal = -2.0   # strong net inflow = distribution
        elif ratio < 0.9:
            flow_signal = -1.0

        return {
            "exchange_inflow":  round(in_val, 2),
            "exchange_outflow": round(out_val, 2),
            "net_flow":         round(net, 2),
            "flow_ratio":       round(ratio, 3),
            "flow_signal":      flow_signal,
            "summary": (
                f"{asset} exchange flows: "
                f"in={in_val:.0f} out={out_val:.0f} "
                f"(net {'+' if net>0 else ''}{net:.0f}) | signal {flow_signal:+.1f}"
            ),
        }
    except Exception:
        return {}


# ── Combined fetch ─────────────────────────────────────────────────────────────

def fetch_all_onchain() -> dict:
    """
    Fetch all available on-chain data and return combined dict.
    Gracefully degrades: works with zero API keys, improves with each one added.
    """
    btc   = get_btc_network_stats()
    eth   = get_eth_gas_stats()
    btc_flows = get_glassnode_exchange_flows("BTC")
    eth_flows = get_glassnode_exchange_flows("ETH")

    # Combined signal: average of whatever is available
    signals = []
    if btc.get("combined_score") is not None:
        signals.append(btc["combined_score"])
    if eth.get("gas_signal") is not None:
        signals.append(eth["gas_signal"])
    if btc_flows.get("flow_signal") is not None:
        signals.append(btc_flows["flow_signal"])

    combined = round(sum(signals) / len(signals), 2) if signals else 0.0

    result = {
        "btc_network":  btc,
        "eth_gas":      eth,
        "btc_flows":    btc_flows,
        "eth_flows":    eth_flows,
        "combined_score": combined,
        "available":    bool(btc or eth or btc_flows),
    }

    if result["available"]:
        parts = []
        if btc.get("summary"):
            parts.append(btc["summary"])
        if eth.get("summary"):
            parts.append(eth["summary"])
        if btc_flows.get("summary"):
            parts.append(btc_flows["summary"])
        logger.info("On-chain: combined=%.2f | %s", combined, " | ".join(parts[:2]))

    return result

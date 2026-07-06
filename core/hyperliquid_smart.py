"""Hyperliquid top-trader positioning — per-coin smart-money bias.

Hyperliquid is a fully on-chain perp DEX: every wallet's positions and PnL
are public via a keyless API. We select a roster of PROVEN traders from the
leaderboard (positive 30d AND all-time PnL, real volume), poll their open
positions, and produce a per-coin net bias in [-5, +5].

READ-ONLY signal use. We do not trade on Hyperliquid.

Endpoints (verified live 2026-07-06):
  POST https://api.hyperliquid.xyz/info {"type":"clearinghouseState","user":addr}
  GET  https://stats-data.hyperliquid.xyz/Mainnet/leaderboard  (~33MB, daily)
"""
from __future__ import annotations
import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_INFO_URL = "https://api.hyperliquid.xyz/info"
_LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_ROSTER_PATH = os.path.join(_DATA_DIR, "hl_roster.json")

_ROSTER_TTL = 24 * 3600
_POS_TTL = 1800

_lock = threading.Lock()
_pos_cache = {"ts": 0.0, "bias": {}}   # coin -> {"bias","n_long","n_short"}

# Kraken pair -> Hyperliquid coin symbol
_PAIR_TO_COIN = {
    "XBTEUR": "BTC", "XXBTZEUR": "BTC",
    "ETHEUR": "ETH", "XETHZEUR": "ETH",
    "SOLEUR": "SOL", "XRPEUR": "XRP", "XXRPZEUR": "XRP",
    "ADAEUR": "ADA", "DOTEUR": "DOT", "LINKEUR": "LINK",
    "LTCEUR": "LTC", "XLTCZEUR": "LTC", "AVAXEUR": "AVAX",
    "XDGEUR": "DOGE", "DOGEEUR": "DOGE", "XLMEUR": "XLM",
    "UNIEUR": "UNI", "AAVEEUR": "AAVE", "NEAREUR": "NEAR",
    "SUIEUR": "SUI", "WLDEUR": "WLD", "TAOEUR": "TAO",
    "HYPEEUR": "HYPE", "ONDOEUR": "ONDO", "JUPEUR": "JUP",
    "BCHEUR": "BCH", "TRXEUR": "TRX", "POLEUR": "POL",
}


def coin_for_pair(pair: str) -> Optional[str]:
    return _PAIR_TO_COIN.get((pair or "").upper())


# ── pure maths (unit-tested) ──────────────────────────────────────────────────

def select_roster(rows: List[dict], size: int, min_volume_usd: float) -> List[dict]:
    """Filter leaderboard rows to proven traders; return top-`size` by 30d pnl.
    Row weight = 30d pnl normalised to the roster median, capped [0.5, 2.0]."""
    cands = []
    for r in rows:
        try:
            wp = dict(r.get("windowPerformances") or [])
            month = wp.get("month") or {}
            alltime = wp.get("allTime") or {}
            m_pnl = float(month.get("pnl") or 0)
            a_pnl = float(alltime.get("pnl") or 0)
            m_vlm = float(month.get("vlm") or 0)
        except Exception:
            continue
        if m_pnl <= 0 or a_pnl <= 0 or m_vlm < min_volume_usd:
            continue
        if not r.get("ethAddress"):
            continue
        cands.append({"address": r.get("ethAddress"), "month_pnl": m_pnl})
    cands.sort(key=lambda c: c["month_pnl"], reverse=True)
    top = cands[:size]
    if not top:
        return []
    med = sorted(c["month_pnl"] for c in top)[len(top) // 2] or 1.0
    for c in top:
        c["weight"] = max(0.5, min(2.0, c["month_pnl"] / med))
    return top


def score_coin_bias(positions: List[dict], min_traders: int) -> dict:
    """positions = [{"weight": w, "side": +1|-1}, ...] for ONE coin."""
    n_long = sum(1 for p in positions if p["side"] > 0)
    n_short = sum(1 for p in positions if p["side"] < 0)
    if len(positions) < min_traders:
        return {"bias": 0.0, "n_long": n_long, "n_short": n_short}
    tot_w = sum(p["weight"] for p in positions)
    net_w = sum(p["weight"] * p["side"] for p in positions)
    bias = round(5.0 * net_w / tot_w, 2) if tot_w > 0 else 0.0
    return {"bias": bias, "n_long": n_long, "n_short": n_short}


# ── fetchers (thin, failure -> stale cache -> neutral) ───────────────────────

def _load_roster_file() -> Optional[dict]:
    try:
        with open(_ROSTER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _refresh_roster(cfg: dict) -> Optional[List[dict]]:
    size = int(cfg.get("hl_roster_size", 20))
    min_vlm = float(cfg.get("hl_min_volume_usd", 50e6))
    try:
        r = requests.get(_LEADERBOARD_URL, timeout=90)
        r.raise_for_status()
        rows = r.json().get("leaderboardRows") or []
    except Exception as exc:
        logger.warning("HL leaderboard fetch failed: %s", exc)
        return None
    roster = select_roster(rows, size=size, min_volume_usd=min_vlm)
    if roster:
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(_ROSTER_PATH, "w", encoding="utf-8") as f:
                json.dump({"ts": int(time.time()), "roster": roster}, f)
        except Exception:
            pass
        logger.info("HL roster refreshed: %d traders (top month pnl $%.0f)",
                    len(roster), roster[0]["month_pnl"])
    return roster or None


def _get_roster(cfg: dict) -> List[dict]:
    cached = _load_roster_file()
    if cached and time.time() - cached.get("ts", 0) < _ROSTER_TTL:
        return cached.get("roster") or []
    fresh = _refresh_roster(cfg)
    if fresh:
        return fresh
    return (cached or {}).get("roster") or []   # stale better than nothing


def _fetch_positions(address: str) -> Optional[list]:
    try:
        r = requests.post(_INFO_URL, json={"type": "clearinghouseState",
                                           "user": address}, timeout=10)
        r.raise_for_status()
        return r.json().get("assetPositions") or []
    except Exception as exc:
        logger.debug("HL positions fetch failed %s: %s", str(address)[:10], exc)
        return None


def _refresh_bias(cfg: dict) -> Dict[str, dict]:
    roster = _get_roster(cfg)
    min_traders = int(cfg.get("hl_min_traders", 3))
    per_coin: Dict[str, List[dict]] = {}
    for tr in roster:
        pos_list = _fetch_positions(tr["address"])
        if pos_list is None:
            continue
        for ap in pos_list:
            p = ap.get("position") or {}
            coin = p.get("coin")
            try:
                szi = float(p.get("szi") or 0)
            except Exception:
                continue
            if not coin or szi == 0:
                continue
            per_coin.setdefault(coin, []).append(
                {"weight": tr.get("weight", 1.0), "side": 1 if szi > 0 else -1})
        time.sleep(0.1)  # gentle on the public API
    out = {c: score_coin_bias(ps, min_traders) for c, ps in per_coin.items()}
    if out:
        top = sorted(out.items(), key=lambda kv: abs(kv[1]["bias"]), reverse=True)[:5]
        logger.info("HL bias refreshed (%d coins): %s", len(out),
                    ", ".join(f"{c}={v['bias']:+.1f}" for c, v in top))
    return out


def get_bias(coin: str, cfg: dict = None, refresh: bool = False) -> dict:
    """Per-coin smart-trader bias; TTL-cached; neutral on failure/unmapped.

    refresh=False (the buy-gate path) NEVER fetches: cached bias — stale is
    fine — or neutral before the first refresh. Only the background warmer
    passes refresh=True (33MB leaderboard + 20 position polls must never run
    inside the trading loop).
    """
    neutral = {"bias": 0.0, "n_long": 0, "n_short": 0}
    if not coin:
        return neutral
    cfg = cfg or {}
    with _lock:
        if not refresh:
            return _pos_cache["bias"].get(coin, neutral)
        fresh = time.time() - _pos_cache["ts"] < _POS_TTL
        if fresh:
            return _pos_cache["bias"].get(coin, neutral)
    try:
        bias = _refresh_bias(cfg)
    except Exception as exc:
        logger.debug("HL bias refresh failed: %s", exc)
        bias = {}
    with _lock:
        if bias:
            _pos_cache["bias"] = bias
        _pos_cache["ts"] = time.time()
        return _pos_cache["bias"].get(coin, neutral)

# Smart-Money Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a two-component smart-money signal (whale exchange flows + Hyperliquid top-trader positioning) that vetoes buys when informed capital is bearish and boosts size/entry-bar when it is bullish.

**Architecture:** Three small new modules — `core/whale_flows.py` (market-level flow score), `core/hyperliquid_smart.py` (per-coin trader bias), `core/smart_money.py` (decision facade the bot calls) — wired into `trading_bot.py`'s `_execute_buy_gate` and `_get_dynamic_trade_amount_eur`. All scoring maths are pure functions with stdlib-assert tests; network fetchers are thin, TTL-cached, and neutral-on-failure.

**Tech Stack:** Python 3 stdlib + `requests` (already a dependency). No new packages, no new API keys (Alchemy key already in `.env`; Hyperliquid is keyless).

## Global Constraints

- Kraken main bot only; ByBitBot untouched.
- Missing/failed data is ALWAYS neutral (score 0.0) — the layer must never block trading because an API is down.
- Boost stays inside existing caps: `min(base*2, amount, available*0.95)` in `_get_dynamic_trade_amount_eur`.
- All thresholds live in `config.paper.toml [smart_money]`; absent config = code defaults; `enabled=false` disables everything.
- Tests are plain-assert scripts run with `py -3 tests/test_smart_money.py` (repo has no pytest).
- Files run on Linux in Docker — LF line endings, no UTF-8 BOM (use the Write/Edit tools, never PowerShell `WriteAllText`).
- Every commit message ends with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `core/whale_flows.py` — market-level whale flow score

**Files:**
- Create: `core/whale_flows.py`
- Modify: `core/onchain_data.py:325-327` (eth_flows bug fix)
- Test: `tests/test_smart_money.py` (new file, first test block)

**Interfaces:**
- Consumes: `core.onchain_data.get_coinmetrics_exchange_flows(asset)` (existing; returns dict with `flow_signal` ∈ [−2, +2] or `{}`).
- Produces: `whale_flows.get_whale_score() -> float` (∈ [−5, +5], 0.0 = neutral/no data; TTL-cached 1800s) and pure `score_fresh_flows(eth_in, eth_out, stable_in_usd, min_activity_eth) -> float` used by tests.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smart_money.py`:

```python
"""Smart-money layer unit tests — pure scoring maths, no network.
Run: py -3 tests/test_smart_money.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_whale_fresh_score():
    from core.whale_flows import score_fresh_flows

    # Strong outflow (accumulation) -> strongly bullish
    s = score_fresh_flows(eth_in=500, eth_out=2000, stable_in_usd=0, min_activity_eth=500)
    assert s >= 3.0, s
    # Strong inflow (distribution) -> strongly bearish
    s = score_fresh_flows(eth_in=2000, eth_out=400, stable_in_usd=0, min_activity_eth=500)
    assert s <= -3.0, s
    # Balanced flows -> neutral-ish
    s = score_fresh_flows(eth_in=1000, eth_out=1050, stable_in_usd=0, min_activity_eth=500)
    assert -1.0 <= s <= 1.0, s
    # Too little whale activity to judge -> exactly neutral
    s = score_fresh_flows(eth_in=100, eth_out=150, stable_in_usd=0, min_activity_eth=500)
    assert s == 0.0, s
    # Stablecoin inflow adds a bullish bonus on top of neutral flows
    s0 = score_fresh_flows(eth_in=1000, eth_out=1000, stable_in_usd=0, min_activity_eth=500)
    s1 = score_fresh_flows(eth_in=1000, eth_out=1000, stable_in_usd=3_000_000, min_activity_eth=500)
    assert s1 > s0, (s0, s1)
    # Clamped to [-5, +5]
    s = score_fresh_flows(eth_in=1, eth_out=100000, stable_in_usd=50_000_000, min_activity_eth=500)
    assert s <= 5.0, s
    print("test_whale_fresh_score OK")


def test_whale_blend():
    from core.whale_flows import blend_scores

    # fresh 0.6 / daily 0.4 weighting
    assert abs(blend_scores(fresh=5.0, daily=0.0) - 3.0) < 1e-9
    assert abs(blend_scores(fresh=0.0, daily=-5.0) - (-2.0)) < 1e-9
    # missing components (None) drop out
    assert blend_scores(fresh=None, daily=-5.0) == -5.0
    assert blend_scores(fresh=4.0, daily=None) == 4.0
    assert blend_scores(fresh=None, daily=None) == 0.0
    print("test_whale_blend OK")


if __name__ == "__main__":
    test_whale_fresh_score()
    test_whale_blend()
    print("ALL OK")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 tests/test_smart_money.py`
Expected: `ModuleNotFoundError: No module named 'core.whale_flows'`

- [ ] **Step 3: Write `core/whale_flows.py`**

```python
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
        score = blend_scores(_fresh_score(cfg or {}), _daily_score())
    except Exception as exc:
        logger.debug("whale score failed: %s", exc)
        score = 0.0
    with _lock:
        _cache["ts"] = time.time()
        _cache["score"] = score
    return score
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 tests/test_smart_money.py`
Expected: `test_whale_fresh_score OK`, `test_whale_blend OK`, `ALL OK`

- [ ] **Step 5: Fix the eth_flows bug in `core/onchain_data.py`**

In `fetch_all_onchain()` find:

```python
    if btc_flows.get("flow_signal") is not None:
        signals.append(btc_flows["flow_signal"])
```

Replace with:

```python
    if btc_flows.get("flow_signal") is not None:
        signals.append(btc_flows["flow_signal"])
    # eth_flows was fetched but never scored — include it (bug fix 2026-07-06)
    if eth_flows.get("flow_signal") is not None:
        signals.append(eth_flows["flow_signal"])
```

- [ ] **Step 6: Compile-check + live smoke (network, optional-pass)**

Run: `py -3 -m py_compile core/whale_flows.py core/onchain_data.py`
Expected: silent success.

Run (uses local `.env` key if present; without a key the fresh part is
skipped and only the daily CoinMetrics part scores — both outcomes acceptable):

```bash
py -3 -c "
import sys, os
sys.path.insert(0, '.')
if os.path.exists('.env'):
    for line in open('.env'):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())
from core.whale_flows import get_whale_score
print('whale_score:', get_whale_score())"
```

Expected: `whale_score: <float between -5 and 5>`

- [ ] **Step 7: Commit**

```bash
git add core/whale_flows.py core/onchain_data.py tests/test_smart_money.py
git commit -m "feat: whale exchange-flow score (Alchemy wallet watch + CoinMetrics blend)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `core/hyperliquid_smart.py` — per-coin top-trader bias

**Files:**
- Create: `core/hyperliquid_smart.py`
- Test: `tests/test_smart_money.py` (append test block)

**Interfaces:**
- Consumes: nothing from other tasks (keyless public HTTP).
- Produces: `hyperliquid_smart.get_bias(coin: str, cfg: dict) -> dict` returning `{"bias": float, "n_long": int, "n_short": int}` (bias ∈ [−5, +5], 0.0 neutral); pure `select_roster(rows, size, min_volume_usd) -> list` and `score_coin_bias(positions, min_traders) -> dict` for tests. `PAIR_BASE` mapping helper `coin_for_pair(pair: str) -> str | None` (e.g. "XBTEUR"→"BTC", "XETHZEUR"→"ETH", "SOLEUR"→"SOL"; None if unmapped).

- [ ] **Step 1: Append failing tests to `tests/test_smart_money.py`**

Add before the `if __name__ == "__main__":` block:

```python
def _lb_row(addr, month_pnl, alltime_pnl, month_vlm):
    return {"ethAddress": addr, "accountValue": "1000000",
            "windowPerformances": [
                ["day", {"pnl": "0", "roi": "0", "vlm": "0"}],
                ["week", {"pnl": "0", "roi": "0", "vlm": "0"}],
                ["month", {"pnl": str(month_pnl), "roi": "0.1", "vlm": str(month_vlm)}],
                ["allTime", {"pnl": str(alltime_pnl), "roi": "0.2", "vlm": str(month_vlm)}],
            ]}


def test_hl_roster_filter():
    from core.hyperliquid_smart import select_roster

    rows = [
        _lb_row("0xA", 900_000, 2_000_000, 200e6),   # good
        _lb_row("0xB", -50_000, 5_000_000, 500e6),   # negative month -> OUT
        _lb_row("0xC", 800_000, -100_000, 300e6),    # negative all-time -> OUT (lottery)
        _lb_row("0xD", 700_000, 1_000_000, 1e6),     # volume too small -> OUT
        _lb_row("0xE", 1_200_000, 3_000_000, 400e6), # good, higher month pnl
    ]
    roster = select_roster(rows, size=20, min_volume_usd=50e6)
    addrs = [r["address"] for r in roster]
    assert addrs == ["0xE", "0xA"], addrs           # sorted by month pnl desc
    assert all(r["weight"] > 0 for r in roster)
    print("test_hl_roster_filter OK")


def test_hl_coin_bias():
    from core.hyperliquid_smart import score_coin_bias

    # 3 quality-weighted longs, 1 short -> clearly positive
    pos = [
        {"weight": 1.0, "side": 1}, {"weight": 2.0, "side": 1},
        {"weight": 1.0, "side": 1}, {"weight": 1.0, "side": -1},
    ]
    r = score_coin_bias(pos, min_traders=3)
    assert r["bias"] > 2.0, r
    assert r["n_long"] == 3 and r["n_short"] == 1, r
    # All short -> -5
    r = score_coin_bias([{"weight": 1.0, "side": -1}] * 4, min_traders=3)
    assert r["bias"] == -5.0, r
    # Not enough traders positioned -> neutral
    r = score_coin_bias([{"weight": 1.0, "side": 1}] * 2, min_traders=3)
    assert r["bias"] == 0.0, r
    print("test_hl_coin_bias OK")


def test_pair_mapping():
    from core.hyperliquid_smart import coin_for_pair
    assert coin_for_pair("XBTEUR") == "BTC"
    assert coin_for_pair("XXBTZEUR") == "BTC"
    assert coin_for_pair("XETHZEUR") == "ETH"
    assert coin_for_pair("SOLEUR") == "SOL"
    assert coin_for_pair("XXRPZEUR") == "XRP"
    assert coin_for_pair("NOSUCHEUR") is None
    print("test_pair_mapping OK")
```

And extend the main block:

```python
if __name__ == "__main__":
    test_whale_fresh_score()
    test_whale_blend()
    test_hl_roster_filter()
    test_hl_coin_bias()
    test_pair_mapping()
    print("ALL OK")
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `py -3 tests/test_smart_money.py`
Expected: `ModuleNotFoundError: No module named 'core.hyperliquid_smart'`

- [ ] **Step 3: Write `core/hyperliquid_smart.py`**

```python
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
        logger.debug("HL leaderboard fetch failed: %s", exc)
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
        logger.debug("HL positions fetch failed %s: %s", address[:10], exc)
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


def get_bias(coin: str, cfg: dict = None) -> dict:
    """Per-coin smart-trader bias; TTL-cached; neutral on failure/unmapped."""
    neutral = {"bias": 0.0, "n_long": 0, "n_short": 0}
    if not coin:
        return neutral
    cfg = cfg or {}
    with _lock:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 tests/test_smart_money.py`
Expected: all five `... OK` lines + `ALL OK`

- [ ] **Step 5: Live smoke (keyless, network)**

Run: `py -3 -c "import sys; sys.path.insert(0,'.'); from core.hyperliquid_smart import get_bias; print('BTC bias:', get_bias('BTC', {'hl_roster_size': 10}))"`
Expected: first run takes ~1-2 min (33MB leaderboard + 10 wallets), prints e.g. `BTC bias: {'bias': 1.67, 'n_long': 4, 'n_short': 1}` (any numbers; neutral `0.0` acceptable only if the log shows fetch failures). Verify `data/hl_roster.json` now exists.

- [ ] **Step 6: Commit**

```bash
git add core/hyperliquid_smart.py tests/test_smart_money.py
git commit -m "feat: Hyperliquid top-trader per-coin bias signal

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `core/smart_money.py` — decision facade

**Files:**
- Create: `core/smart_money.py`
- Test: `tests/test_smart_money.py` (append test block)

**Interfaces:**
- Consumes: `whale_flows.get_whale_score(cfg)`, `hyperliquid_smart.get_bias(coin, cfg)`, `hyperliquid_smart.coin_for_pair(pair)`.
- Produces: `smart_money.evaluate(pair, cfg) -> dict` with keys `action` ("veto"|"boost"|"neutral"), `reason` (str), `size_mult` (float), `min_score_delta` (float), `whale_score`, `hl_bias`, `hl_n_long`, `hl_n_short`; pure `decide(whale_score, hl_bias, cfg) -> (action, reason, size_mult, min_score_delta)`; `status_snapshot() -> dict` (last evaluate per pair, for bot_status.json).

- [ ] **Step 1: Append failing tests**

Add to `tests/test_smart_money.py` before the main block:

```python
def test_decide():
    from core.smart_money import decide

    cfg = {"enabled": True, "whale_veto_score": -2.5, "whale_boost_score": 2.5,
           "hl_veto_score": -2.5, "hl_boost_score": 2.5,
           "boost_size_mult": 1.3, "boost_min_score_delta": -2.0}

    # whale veto
    a, reason, mult, delta = decide(-3.0, 0.0, cfg)
    assert a == "veto" and "whale" in reason.lower(), (a, reason)
    # HL per-pair veto
    a, reason, mult, delta = decide(0.0, -3.0, cfg)
    assert a == "veto" and "hyperliquid" in reason.lower() or "trader" in reason.lower(), (a, reason)
    # boost (either component) — no stacking, one boost
    a, reason, mult, delta = decide(3.0, 3.0, cfg)
    assert a == "boost" and mult == 1.3 and delta == -2.0, (a, mult, delta)
    # dead zone -> neutral, no effect
    a, reason, mult, delta = decide(1.0, -1.0, cfg)
    assert a == "neutral" and mult == 1.0 and delta == 0.0, (a, mult, delta)
    # veto wins over boost when components disagree
    a, _, _, _ = decide(-3.0, 4.0, cfg)
    assert a == "veto", a
    # disabled -> neutral always
    a, _, mult, _ = decide(-5.0, -5.0, {**cfg, "enabled": False})
    assert a == "neutral" and mult == 1.0, (a, mult)
    print("test_decide OK")
```

Extend the main block to call `test_decide()` before `print("ALL OK")`.

- [ ] **Step 2: Run tests to verify the new one fails**

Run: `py -3 tests/test_smart_money.py`
Expected: `ModuleNotFoundError: No module named 'core.smart_money'`

- [ ] **Step 3: Write `core/smart_money.py`**

```python
"""Smart-money decision facade — the ONLY thing trading_bot imports.

Combines the market-level whale-flow score and the per-coin Hyperliquid
top-trader bias into one action for the buy gate:
  veto    : block the buy (either component strongly bearish)
  boost   : size x boost_size_mult + entry bar lowered by boost_min_score_delta
  neutral : no effect
Missing data is neutral by construction (both sources return 0.0 on failure).
"""
from __future__ import annotations
import logging
import threading
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_last: Dict[str, dict] = {}   # pair -> last evaluate() result (for status/journal)

_DEFAULTS = {
    "enabled": True,
    "whale_veto_score": -2.5, "whale_boost_score": 2.5,
    "hl_veto_score": -2.5, "hl_boost_score": 2.5,
    "boost_size_mult": 1.3, "boost_min_score_delta": -2.0,
}


def _cfg(cfg: dict) -> dict:
    out = dict(_DEFAULTS)
    out.update(cfg or {})
    return out


def decide(whale_score: float, hl_bias: float, cfg: dict
           ) -> Tuple[str, str, float, float]:
    """Pure decision: (action, reason, size_mult, min_score_delta)."""
    c = _cfg(cfg)
    if not c.get("enabled", True):
        return "neutral", "smart_money disabled", 1.0, 0.0

    wv, wb = float(c["whale_veto_score"]), float(c["whale_boost_score"])
    hv, hb = float(c["hl_veto_score"]), float(c["hl_boost_score"])

    # Vetoes first — either component can block, veto beats boost
    if wv < 0 and whale_score <= wv:
        return ("veto", f"whale flows bearish ({whale_score:+.1f} <= {wv})", 1.0, 0.0)
    if hv < 0 and hl_bias <= hv:
        return ("veto", f"Hyperliquid top traders net short ({hl_bias:+.1f} <= {hv})", 1.0, 0.0)

    # One boost max (no stacking)
    if (wb > 0 and whale_score >= wb) or (hb > 0 and hl_bias >= hb):
        src = "whale accumulation" if whale_score >= wb else "HL smart traders long"
        return ("boost", f"{src} (whale {whale_score:+.1f} / HL {hl_bias:+.1f})",
                float(c["boost_size_mult"]), float(c["boost_min_score_delta"]))

    return "neutral", "", 1.0, 0.0


def evaluate(pair: str, cfg: dict = None) -> dict:
    """Fetch both component scores for `pair` and decide. Never raises."""
    cfg = cfg or {}
    whale_score, hl = 0.0, {"bias": 0.0, "n_long": 0, "n_short": 0}
    try:
        from core.whale_flows import get_whale_score
        whale_score = float(get_whale_score(cfg))
    except Exception as exc:
        logger.debug("whale score unavailable: %s", exc)
    try:
        from core.hyperliquid_smart import get_bias, coin_for_pair
        coin = coin_for_pair(pair)
        if coin:
            hl = get_bias(coin, cfg)
    except Exception as exc:
        logger.debug("HL bias unavailable: %s", exc)

    action, reason, size_mult, min_delta = decide(whale_score, hl["bias"], cfg)
    out = {
        "action": action, "reason": reason,
        "size_mult": size_mult, "min_score_delta": min_delta,
        "whale_score": round(whale_score, 2), "hl_bias": round(hl["bias"], 2),
        "hl_n_long": hl["n_long"], "hl_n_short": hl["n_short"],
    }
    with _lock:
        _last[pair] = out
    return out


def last_for(pair: str) -> dict:
    with _lock:
        return dict(_last.get(pair) or {})


def status_snapshot() -> dict:
    with _lock:
        return {p: {k: v[k] for k in ("action", "whale_score", "hl_bias")}
                for p, v in _last.items()}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 tests/test_smart_money.py`
Expected: all six `... OK` lines + `ALL OK`

- [ ] **Step 5: Commit**

```bash
git add core/smart_money.py tests/test_smart_money.py
git commit -m "feat: smart-money decision facade (veto/boost/neutral)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Wire into `trading_bot.py` + config + verification

**Files:**
- Modify: `trading_bot.py` — `_execute_buy_gate` (after the AI-panel veto / RSI-ceiling block, BEFORE the `_effective_min` comparison), `_get_dynamic_trade_amount_eur` (after the `min_target` floor), `_entry_signal_snapshot` (journal features), bot-status write (`smart_money` object)
- Modify: `config.paper.toml` — new `[smart_money]` block

**Interfaces:**
- Consumes: `core.smart_money.evaluate(pair, cfg)`, `.last_for(pair)`, `.status_snapshot()` (Task 3 signatures).
- Produces: journal `extra.features` keys `whale_score`, `hl_bias`, `hl_n_long`, `hl_n_short`, `smart_action`; `bot_status.json` key `smart_money`; `self._smart_boost` dict read by sizing.

- [ ] **Step 1: Add the gate logic in `_execute_buy_gate`**

Find the end of the 1h-RSI-ceiling block (added 2026-07-06):

```python
        _rsi_ceiling = float(self.config.get('technical', {}).get('max_entry_rsi_1h', 60.0))
        if _rsi_ceiling > 0 and self._pair_profile(pair).get('strategy') == 'mean_reversion':
            _rsi1h = self._rsi_1h.get(pair)
            if _rsi1h is not None and float(_rsi1h) >= _rsi_ceiling:
                self.logger.info(
                    "BUY skipped for %s: 1h RSI %.1f >= ceiling %.1f (no mean-reversion buys into overbought)",
                    pair, float(_rsi1h), _rsi_ceiling)
                return
```

Insert AFTER it (and BEFORE the `_lunar_adj` line):

```python
        # Smart-money layer: whale exchange flows (market) + Hyperliquid
        # top-trader bias (per-coin). Veto blocks; boost lowers the entry bar
        # here and scales size in _get_dynamic_trade_amount_eur. Missing data
        # is neutral. See docs/superpowers/specs/2026-07-06-smart-money-layer-design.md
        _smart_mult, _smart_delta = 1.0, 0.0
        try:
            from core import smart_money as _smart
            _sm = _smart.evaluate(pair, self.config.get('smart_money', {}))
            if _sm["action"] == "veto":
                self.logger.info("BUY skipped for %s: %s", pair, _sm["reason"])
                return
            if _sm["action"] == "boost":
                _smart_mult, _smart_delta = _sm["size_mult"], _sm["min_score_delta"]
                self.logger.info("BUY boosted for %s: %s (size x%.2f, min %+0.1f)",
                                 pair, _sm["reason"], _smart_mult, _smart_delta)
        except Exception as _sme:
            self.logger.debug("smart_money evaluate failed for %s: %s", pair, _sme)
        if not hasattr(self, '_smart_boost'):
            self._smart_boost = {}
        self._smart_boost[pair] = _smart_mult
```

Then find the effective-min line:

```python
        _effective_min  = _pair_min_score + _intel_adj + _lunar_adj + _onchain_adj
```

Replace with:

```python
        _effective_min  = _pair_min_score + _intel_adj + _lunar_adj + _onchain_adj + _smart_delta
```

- [ ] **Step 2: Apply the size boost in `_get_dynamic_trade_amount_eur`**

Find (added 2026-07-06):

```python
        min_target = float(self.config.get('risk_management', {}).get('min_target_trade_eur', 0.0))
        if min_target > 0 and sizing_base > small_account_threshold:
            amount = max(amount, min_target)
```

Insert AFTER it:

```python
        # Smart-money boost: scale up when whales/top traders are bullish.
        # Set by _execute_buy_gate this loop; stays inside the final cap below.
        amount *= float(getattr(self, '_smart_boost', {}).get(pair, 1.0))
```

- [ ] **Step 3: Journal the features in `_entry_signal_snapshot`**

`_entry_signal_snapshot(pair)` (trading_bot.py:4942) builds a dict named
`snap` inside a guarded `try:` block (`snap = { ... }` starting ~line 4972).
Insert AFTER the `snap = { ... }` literal closes, still inside the same
`try:` block:

```python
            # Smart-money layer (whale flows + HL trader bias) — journaled for
            # the 2-week evidence review of veto/boost thresholds
            try:
                from core import smart_money as _smart
                _smf = _smart.last_for(pair)
                snap["whale_score"] = _smf.get("whale_score")
                snap["hl_bias"] = _smf.get("hl_bias")
                snap["hl_n_long"] = _smf.get("hl_n_long")
                snap["hl_n_short"] = _smf.get("hl_n_short")
                snap["smart_action"] = _smf.get("action")
            except Exception:
                pass
```

- [ ] **Step 4: Expose in bot_status.json**

Find the status-write section where `'intelligence_score': _num(getattr(self, '_intelligence_score', 0.0)),` appears (~line 4927). ABOVE the dict build that contains it, add:

```python
        try:
            from core import smart_money as _smart
            _smart_status = _smart.status_snapshot()
        except Exception:
            _smart_status = {}
```

and inside that dict, next to the `intelligence_score` entry:

```python
                'smart_money': _smart_status,
```

- [ ] **Step 5: Add the config block to `config.paper.toml`**

Append after the `[intelligence]` section:

```toml
[smart_money]
# Whale exchange flows (Alchemy wallet watch + CoinMetrics daily) + Hyperliquid
# top-trader positioning. Veto blocks buys; boost = size x mult + lower entry
# bar. Missing data always neutral. Journal fields: whale_score / hl_bias /
# smart_action -> re-tune after ~2 weeks of entries.
enabled = true
whale_veto_score = -2.5     # whale_score <= this -> block ALL buys
whale_boost_score = 2.5     # whale_score >= this -> boost
hl_veto_score = -2.5        # hl_bias[pair] <= this -> block buys on that pair
hl_boost_score = 2.5        # hl_bias[pair] >= this -> boost
boost_size_mult = 1.3       # position size x1.3 (still inside EUR70/cash caps)
boost_min_score_delta = -2.0 # entry bar lowered while boosted
min_whale_eth = 250.0       # ignore transfers below this (ETH)
min_whale_stable_usd = 500000.0
hl_roster_size = 20         # top traders followed
hl_min_traders = 3          # min positioned traders before a coin bias counts
hl_min_volume_usd = 50000000.0  # 30d volume floor for roster eligibility
```

- [ ] **Step 6: Compile + full test suite + wiring harness**

Run: `py -3 -m py_compile trading_bot.py core/smart_money.py core/whale_flows.py core/hyperliquid_smart.py`
Expected: silent success.

Run: `py -3 tests/test_smart_money.py`
Expected: `ALL OK`

Run the wiring harness (checks the decide-path end to end with forced scores):

```bash
py -3 -c "
import sys; sys.path.insert(0,'.')
from core.smart_money import decide
cfg = dict(enabled=True, whale_veto_score=-2.5, whale_boost_score=2.5,
           hl_veto_score=-2.5, hl_boost_score=2.5,
           boost_size_mult=1.3, boost_min_score_delta=-2.0)
assert decide(-2.6, 0, cfg)[0] == 'veto'
assert decide(0, 2.6, cfg)[0] == 'boost'
assert decide(0, 0, cfg)[0] == 'neutral'
# boost sizing stays inside cap: min(70, 50*1.3, cash*0.95)
amount = max(19.0, 50.0) * 1.3
assert min(70.0, amount, 442*0.95) == 65.0, amount
print('wiring harness OK')"
```

Expected: `wiring harness OK`

- [ ] **Step 7: Validate the TOML parses**

Run: `py -3 -c "import tomllib; c=tomllib.load(open('config.paper.toml','rb')); print(c['smart_money']['enabled'], c['smart_money']['hl_roster_size'])"`
Expected: `True 20`

- [ ] **Step 8: Commit**

```bash
git add trading_bot.py config.paper.toml
git commit -m "feat: wire smart-money layer into buy gate, sizing, journal, status

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 9: Push + deploy + live verification**

```bash
git push origin main
```

User deploys on server: `cd /home/botuser/bobtradingbot && git pull && docker compose up --build -d`
then verifies `docker inspect tradingbot_local --format '{{.Created}}'` is seconds-ago.

Live checks (first hour):
- `docker logs tradingbot_local 2>&1 | grep -E 'HL roster refreshed|HL bias refreshed|Whale flows fresh'` — all three lines should appear within ~35 min of start.
- `docker exec tradingbot_local cat /app/data/hl_roster.json | head -c 400` — roster file exists with 20 addresses.
- Next BUY journal row contains `whale_score` / `hl_bias` / `smart_action` in `extra.features`.
- No increase in `tick error` lines vs pre-deploy.

Evaluation reminder (write to memory, not code): after ~2 weeks bucket closed
trades by `smart_action` and by would-veto score ranges; boost must outperform
neutral on net WR or `boost_*` gets pulled back.

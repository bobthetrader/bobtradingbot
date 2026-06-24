"""Ichimoku Cloud + Gaussian Channel signal module.

Cycle: Tenkan=20, Kijun=30, Senkou B=60 (non-standard, faster than 9/26/52).
Data:  1-hour OHLC candles from Kraken REST API.
Cache: 5-minute TTL per pair — no extra REST calls per trading loop.

Rules:
  BLOCK buy  — price inside or below Ichimoku cloud
  ALLOW buy  — price above cloud (existing RSI/BB/SMA signals proceed)
  BOOST buy  — price above cloud AND near Gaussian lower band (+1.5 score)
"""

import logging
import time
import math
from typing import Optional

logger = logging.getLogger(__name__)

# ── Ichimoku parameters ────────────────────────────────────────────────────────
_TENKAN   = 20
_KIJUN    = 30
_SENKOU_B = 60
_DISPLACE = 30   # Senkou spans plotted N periods forward (= Kijun period)

# ── Gaussian channel parameters ───────────────────────────────────────────────
_GAUSS_PERIOD = 30
_GAUSS_MULT   = 2.0   # standard-deviation multiplier for bands
_GAUSS_NEAR   = 0.005  # within 0.5% of lower band counts as "near"

# ── Cache ──────────────────────────────────────────────────────────────────────
_CACHE: dict = {}      # pair → {ts, result}
_CACHE_TTL   = 300     # seconds


# ── Pure maths ─────────────────────────────────────────────────────────────────

def _donchian_mid(highs: list, lows: list, period: int) -> Optional[float]:
    """(highest_high + lowest_low) / 2 over last *period* candles."""
    if len(highs) < period or len(lows) < period:
        return None
    return (max(highs[-period:]) + min(lows[-period:])) / 2


def _gaussian_weights(period: int) -> list:
    """Normalised Gaussian kernel weights for *period* candles."""
    sigma = period / 4.0
    weights = [math.exp(-0.5 * ((period - 1 - i) / sigma) ** 2) for i in range(period)]
    total = sum(weights)
    return [w / total for w in weights]


def _gaussian_ma(closes: list, period: int) -> Optional[float]:
    """Gaussian-weighted moving average of the last *period* closes."""
    if len(closes) < period:
        return None
    weights = _gaussian_weights(period)
    return sum(w * c for w, c in zip(weights, closes[-period:]))


def _std(values: list) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


# ── Main signal builder ────────────────────────────────────────────────────────

def _compute(candles: list) -> dict:
    """Run Ichimoku + Gaussian on a list of OHLC candle rows.

    Each row: [time, open, high, low, close, vwap, volume, count]
    Returns a dict with keys: trend, price_vs_cloud, gaussian_buy, score_boost.
    """
    if len(candles) < _SENKOU_B + _DISPLACE + 5:
        return {"trend": "unknown", "price_vs_cloud": "unknown",
                "gaussian_buy": False, "score_boost": 0.0}

    highs  = [float(r[2]) for r in candles]
    lows   = [float(r[3]) for r in candles]
    closes = [float(r[4]) for r in candles]
    price  = closes[-1]

    # ── Ichimoku ──────────────────────────────────────────────────────────────
    tenkan = _donchian_mid(highs, lows, _TENKAN)
    kijun  = _donchian_mid(highs, lows, _KIJUN)
    if tenkan is None or kijun is None:
        return {"trend": "unknown", "price_vs_cloud": "unknown",
                "gaussian_buy": False, "score_boost": 0.0}

    span_a = (tenkan + kijun) / 2

    # Senkou Span B uses the last _SENKOU_B candles (before displacement)
    # We use the un-displaced value to compare against current price
    span_b_val = _donchian_mid(highs[:-_DISPLACE] if len(highs) > _DISPLACE else highs,
                               lows[:-_DISPLACE]  if len(lows)  > _DISPLACE else lows,
                               _SENKOU_B)
    if span_b_val is None:
        span_b_val = span_a

    cloud_top    = max(span_a, span_b_val)
    cloud_bottom = min(span_a, span_b_val)

    if price > cloud_top:
        price_vs_cloud = "above"
        trend = "bullish"
    elif price < cloud_bottom:
        price_vs_cloud = "below"
        trend = "bearish"
    else:
        price_vs_cloud = "inside"
        trend = "neutral"

    # ── Gaussian Channel ──────────────────────────────────────────────────────
    gma = _gaussian_ma(closes, _GAUSS_PERIOD)
    if gma is None:
        gaussian_buy  = False
        score_boost   = 0.0
    else:
        recent_std    = _std(closes[-_GAUSS_PERIOD:])
        lower_band    = gma - _GAUSS_MULT * recent_std
        gaussian_buy  = price <= lower_band * (1 + _GAUSS_NEAR)
        score_boost   = 1.5 if (gaussian_buy and trend == "bullish") else 0.0

    return {
        "trend":         trend,
        "price_vs_cloud": price_vs_cloud,
        "cloud_top":     round(cloud_top, 6),
        "cloud_bottom":  round(cloud_bottom, 6),
        "tenkan":        round(tenkan, 6),
        "kijun":         round(kijun, 6),
        "span_a":        round(span_a, 6),
        "span_b":        round(span_b_val, 6),
        "gma":           round(gma, 6) if gma else None,
        "gaussian_buy":  gaussian_buy,
        "score_boost":   score_boost,
        "price":         round(price, 6),
    }


def get_signal(pair: str, api_client) -> dict:
    """Return cached Ichimoku + Gaussian signal for *pair*.

    Fetches 1-hour OHLC from Kraken REST if cache is stale (>5 min).
    Falls back to permissive defaults if data unavailable.
    """
    now = time.time()
    cached = _CACHE.get(pair)
    if cached and (now - cached["ts"]) < _CACHE_TTL:
        return cached["result"]

    _PERMISSIVE = {"trend": "unknown", "price_vs_cloud": "unknown",
                   "gaussian_buy": False, "score_boost": 0.0}
    try:
        ohlc = api_client.get_ohlc_data(pair, interval=60)
        if not ohlc:
            return _PERMISSIVE
        key = next((k for k in ohlc if k != "last"), None)
        if not key:
            return _PERMISSIVE
        candles = ohlc[key]
        result  = _compute(candles)
        _CACHE[pair] = {"ts": now, "result": result}
        logger.debug(
            "[ICHI] %s | trend=%s | vs_cloud=%s | gaussian_buy=%s | boost=%.1f",
            pair, result["trend"], result["price_vs_cloud"],
            result["gaussian_buy"], result["score_boost"],
        )
        return result
    except Exception as exc:
        logger.warning("[ICHI] Signal fetch failed for %s: %s", pair, exc)
        return _PERMISSIVE

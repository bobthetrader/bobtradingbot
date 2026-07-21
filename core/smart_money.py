"""Smart-money decision facade — the ONLY thing trading_bot imports.

Combines the market-level whale-flow score and the per-coin Hyperliquid
top-trader bias into one action for the buy gate:
  veto    : block the buy (either component strongly bearish)
  boost   : size x boost_size_mult + entry bar lowered by boost_min_score_delta
  neutral : squeezed — size x neutral_size_mult + entry bar raised by
            neutral_min_score_delta (weight shifted toward boosted entries)
Missing data is neutral by construction (both sources return 0.0 on failure),
so a data outage trades smaller and pickier, never blind at full size.
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
    # Un-boosted entries are squeezed (smaller size, higher bar) so boosted
    # trades carry more of the book — journal evidence 2026-07-21: boost WR
    # 66.7% vs neutral 44.6%. Data outages read as neutral, so an outage
    # means smaller/pickier entries by design. Set 1.0/0.0 to opt out.
    "neutral_size_mult": 0.75, "neutral_min_score_delta": 1.5,
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
    whale_fired = wb > 0 and whale_score >= wb
    if whale_fired or (hb > 0 and hl_bias >= hb):
        src = "whale accumulation" if whale_fired else "HL smart traders long"
        return ("boost", f"{src} (whale {whale_score:+.1f} / HL {hl_bias:+.1f})",
                float(c["boost_size_mult"]), float(c["boost_min_score_delta"]))

    return ("neutral", "no smart-money boost",
            float(c["neutral_size_mult"]), float(c["neutral_min_score_delta"]))


def evaluate(pair: str, cfg: dict = None, refresh: bool = False) -> dict:
    """Score `pair` and decide. Never raises.

    refresh=False (default — the buy-gate path) reads cached component scores
    only and NEVER touches the network. Only the background warmer thread
    passes refresh=True to trigger the actual (slow, paced) data refreshes.
    """
    cfg = cfg or {}
    whale_score, hl = 0.0, {"bias": 0.0, "n_long": 0, "n_short": 0}
    try:
        from core.whale_flows import get_whale_score
        whale_score = float(get_whale_score(cfg, refresh=refresh))
    except Exception as exc:
        logger.debug("whale score unavailable: %s", exc)
    try:
        from core.hyperliquid_smart import get_bias, coin_for_pair
        coin = coin_for_pair(pair)
        if coin:
            hl = get_bias(coin, cfg, refresh=refresh)
    except Exception as exc:
        logger.debug("HL bias unavailable: %s", exc)

    try:
        action, reason, size_mult, min_delta = decide(whale_score, hl["bias"], cfg)
        out = {
            "action": action, "reason": reason,
            "size_mult": size_mult, "min_score_delta": min_delta,
            "whale_score": round(whale_score, 2), "hl_bias": round(hl["bias"], 2),
            "hl_n_long": hl["n_long"], "hl_n_short": hl["n_short"],
        }
    except Exception as exc:
        logger.debug("smart_money decide failed: %s", exc)
        out = {
            "action": "neutral", "reason": "smart_money error",
            "size_mult": 1.0, "min_score_delta": 0.0,
            "whale_score": 0.0, "hl_bias": 0.0,
            "hl_n_long": 0, "hl_n_short": 0,
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


# ── event-driven shorts gate (pure, unit-tested) ─────────────────────────────

def event_short_ok(panel, hl_bias, whale, ema_bullish, n_open_event, cfg
                   ) -> Tuple[bool, str]:
    """Conviction-stacked short gate: ALL of news-panel, per-coin HL trader
    bias, whale flows and confirmed bearish 1h trend must align. Any missing
    datum (None) closes the gate — no short on missing data. Old technical
    shorts ([shorting] enabled) are a separate, still-disabled path.
    """
    c = cfg or {}
    if not c.get("event_shorts_enabled", False):
        return False, "event shorts disabled"
    try:
        panel_max = float(c.get("event_panel_max", -2.0))
        hl_max = float(c.get("event_hl_max", -2.5))
        whale_max = float(c.get("event_whale_max", -1.5))
        max_conc = int(c.get("event_max_concurrent", 2))
    except Exception:
        return False, "bad event-short config"

    if n_open_event >= max_conc:
        return False, f"event-short cap reached ({n_open_event}/{max_conc})"
    if panel is None or panel > panel_max:
        return False, f"panel {panel} > {panel_max} (no risk-off event)"
    if hl_bias is None or hl_bias > hl_max:
        return False, f"HL bias {hl_bias} > {hl_max} (top traders not short)"
    if whale is None or whale > whale_max:
        return False, f"whale {whale} > {whale_max} (flows not bearish)"
    if ema_bullish is not False:   # True or None both close the gate
        return False, "1h trend not confirmed bearish"
    return True, (f"EVENT SHORT: panel {panel:+.2f} | HL {hl_bias:+.2f} | "
                  f"whale {whale:+.2f} | trend bearish")

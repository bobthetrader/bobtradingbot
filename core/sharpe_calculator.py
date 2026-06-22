"""
Sharpe Ratio Calculator
========================
Reads the JSONL trade event log (logs/trade_events.jsonl) and computes
a per-trade Sharpe ratio over a rolling window of closed trades.

  Sharpe = mean(returns) / std(returns)

where each return_i = pnl_eur_i / capital_at_risk_i (fractional return
on the capital deployed in that single trade).

Goal thresholds:
  SUCCESS:  Sharpe >= 3.0
  FAILURE:  Sharpe <  1.0
  NEUTRAL:  1.0 <= Sharpe < 3.0

With fewer than MIN_TRADES the result is 'insufficient_data'.
"""

import json
import math
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

SHARPE_SUCCESS   = 3.0
SHARPE_FAILURE   = 1.0
MIN_TRADES       = 5     # need at least this many closed trades
ROLLING_WINDOW   = 50    # evaluate over the most recent N trades


# ── I/O helpers ───────────────────────────────────────────────────────────────

def _load_events(path: str) -> list:
    events = []
    if not os.path.exists(path):
        return events
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception as exc:
        logger.debug("Sharpe: failed to load events from %s: %s", path, exc)
    return events


# ── Return extraction ──────────────────────────────────────────────────────────

def _extract_returns(events: list) -> list:
    """
    Pair each BUY/SHORT_OPEN with its corresponding close event and compute
    the fractional return: pnl_eur / capital_deployed.
    """
    returns: list = []
    open_trades: dict = {}   # pair -> {"capital": float}

    for ev in events:
        ttype  = ev.get("type", "")
        pair   = ev.get("pair", "")
        pnl    = float(ev.get("pnl_eur", 0.0))
        price  = float(ev.get("price",   0.0))
        volume = float(ev.get("volume",  0.0))
        bal    = float(ev.get("balance_eur", 100.0))

        if ttype in ("BUY", "SHORT_OPEN"):
            capital = price * volume if price > 0 and volume > 0 else max(bal * 0.05, 1.0)
            open_trades[pair] = {"capital": capital}

        elif ttype in ("SELL", "SHORT_CLOSE"):
            entry = open_trades.pop(pair, None)
            capital = entry["capital"] if entry else max(bal * 0.05, 1.0)
            if capital > 0:
                returns.append(pnl / capital)   # include breakeven (0) trades

    return returns


# ── Maths ─────────────────────────────────────────────────────────────────────

def _sharpe(returns: list) -> Optional[float]:
    n = len(returns)
    if n < MIN_TRADES:
        return None
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / max(n - 1, 1)
    std = math.sqrt(variance)
    if std == 0.0:
        return None
    return round(mean / std, 3)


def _trend(window: list) -> str:
    """Compare the first half vs second half of the window to detect drift."""
    if len(window) < 20:
        return "stable"
    mid   = len(window) // 2
    old_s = _sharpe(window[:mid])
    new_s = _sharpe(window[mid:])
    if old_s is None or new_s is None:
        return "stable"
    if new_s > old_s + 0.2:
        return "toward_success"
    if new_s < old_s - 0.2:
        return "toward_failure"
    return "stable"


# ── Public API ─────────────────────────────────────────────────────────────────

def calculate_sharpe(journal_path: str) -> dict:
    """
    Calculate the current Sharpe ratio and return a structured result.

    Return shape::

        {
          "sharpe":    float | None,
          "n_trades":  int,
          "verdict":   "success" | "failure" | "neutral" | "insufficient_data",
          "trending":  "toward_success" | "toward_failure" | "stable",
          "window":    int,         # how many trades were in the rolling window
        }
    """
    events    = _load_events(journal_path)
    all_ret   = _extract_returns(events)
    window    = all_ret[-ROLLING_WINDOW:] if len(all_ret) > ROLLING_WINDOW else all_ret
    sharpe    = _sharpe(window)
    n_trades  = len(all_ret)

    if sharpe is None:
        verdict  = "insufficient_data"
        trending = "stable"
    elif sharpe >= SHARPE_SUCCESS:
        verdict  = "success"
        trending = "toward_success"
    elif sharpe < SHARPE_FAILURE:
        verdict  = "failure"
        trending = _trend(window)
    else:
        verdict  = "neutral"
        trending = _trend(window)

    result = {
        "sharpe":   sharpe,
        "n_trades": n_trades,
        "verdict":  verdict,
        "trending": trending,
        "window":   len(window),
    }
    logger.info(
        "Sharpe: %.3f | trades=%d | verdict=%s | trending=%s",
        sharpe or 0.0, n_trades, verdict, trending,
    )
    return result

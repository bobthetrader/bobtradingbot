"""
Scientific Method Parameter Optimizer
======================================
Applies the scientific method to bot parameter tuning:

  1. Establish a Sharpe baseline from the current closed-trade history.
  2. Change exactly ONE parameter by exactly ONE step (up or down).
  3. After MIN_EVAL_TRADES new closed trades, measure the new Sharpe.
  4. New Sharpe > baseline  →  KEEP the change; update baseline.
     New Sharpe ≤ baseline  →  REVERT the change.
  5. Advance to the next parameter in the cycle.

Sharpe thresholds (shared with sharpe_calculator):
  SUCCESS: Sharpe >= 3.0
  FAILURE: Sharpe <  1.0

State is persisted to data/optimizer_state.json so experiments survive
bot restarts. Config is written back to the active config TOML file.
"""

import copy
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests
import toml

try:
    from core.history_db import record_optimizer_decision as _record_optimizer_decision
    _HISTORY_DB_AVAILABLE = True
except ImportError:
    try:
        from history_db import record_optimizer_decision as _record_optimizer_decision
        _HISTORY_DB_AVAILABLE = True
    except ImportError:
        _HISTORY_DB_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
MIN_EVAL_TRADES = 10   # closed trades needed to evaluate an experiment
_STATE_FILE   = os.path.join(os.path.dirname(__file__), "..", "data", "optimizer_state.json")
_BT_CACHE_FILE= os.path.join(os.path.dirname(__file__), "..", "data", "backtest_cache.json")

# ── Backtest gate ──────────────────────────────────────────────────────────────

def _fetch_ohlc_kraken(pair: str = "XBTEUR", interval: int = 240, limit: int = 180) -> list:
    """Fetch OHLC closes from Kraken public API. 240m candles × 180 = 30 days."""
    cache_key = f"{pair}_{interval}"
    cache_ttl  = 3600  # refresh hourly

    # Check disk cache
    try:
        if os.path.exists(_BT_CACHE_FILE):
            with open(_BT_CACHE_FILE) as f:
                cache = json.load(f)
            entry = cache.get(cache_key, {})
            if time.time() - entry.get("ts", 0) < cache_ttl:
                return entry.get("closes", [])
    except Exception:
        pass

    try:
        r = requests.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": pair, "interval": interval},
            headers={"User-Agent": "tradingbot/1.0"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        result = r.json().get("result", {})
        key    = next((k for k in result if k != "last"), None)
        if not key:
            return []
        closes = [float(row[4]) for row in result[key][-limit:]]

        # Save to disk cache
        try:
            cache = {}
            if os.path.exists(_BT_CACHE_FILE):
                with open(_BT_CACHE_FILE) as f:
                    cache = json.load(f)
            cache[cache_key] = {"closes": closes, "ts": time.time()}
            with open(_BT_CACHE_FILE, "w") as f:
                json.dump(cache, f)
        except Exception:
            pass

        return closes
    except Exception as exc:
        logger.debug("OHLC fetch for backtest failed: %s", exc)
        return []


def _backtest_sharpe(closes: list, rsi_buy: float, rsi_sell: float,
                     tp_pct: float, sl_pct: float) -> Optional[float]:
    """
    Walk-forward simulation on OHLC closes with given parameters.
    Returns Sharpe ratio or None if insufficient data.

    Strategy: buy when RSI < rsi_buy, sell at TP or SL.
    """
    if len(closes) < 30:
        return None

    # Simple RSI calculation
    def _rsi(prices: list, period: int = 14) -> Optional[float]:
        if len(prices) < period + 1:
            return None
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains  = [max(d, 0) for d in deltas[-period:]]
        losses = [abs(min(d, 0)) for d in deltas[-period:]]
        avg_g  = sum(gains)  / period
        avg_l  = sum(losses) / period
        if avg_l == 0:
            return 100.0
        rs = avg_g / avg_l
        return 100 - (100 / (1 + rs))

    returns = []
    in_trade = False
    entry    = 0.0

    for i in range(20, len(closes)):
        price = closes[i]
        rsi   = _rsi(closes[max(0, i-20):i+1])
        if rsi is None:
            continue

        if not in_trade:
            if rsi < rsi_buy:
                entry    = price
                in_trade = True
        else:
            change_pct = (price - entry) / entry * 100
            if change_pct >= tp_pct:
                returns.append(change_pct / 100)
                in_trade = False
            elif change_pct <= -sl_pct:
                returns.append(change_pct / 100)
                in_trade = False

    if len(returns) < 3:
        return None

    mean = sum(returns) / len(returns)
    var  = sum((r - mean) ** 2 for r in returns) / max(len(returns) - 1, 1)
    std  = math.sqrt(var)
    if std == 0:
        return None
    return round(mean / std, 3)


    # Per-pair strategy profiles (mirrors trading_bot.py _PAIR_PROFILES)
    # Each pair gets its own RSI thresholds that suit its volatility profile
    _PAIR_PROFILES_BT = {
        "XBTEUR":   {"rsi_buy": 32, "rsi_sell": 68, "strategy": "trend"},
        "XXBTZEUR": {"rsi_buy": 32, "rsi_sell": 68, "strategy": "trend"},
        "XETHZEUR": {"rsi_buy": 32, "rsi_sell": 68, "strategy": "trend"},
        "ETHEUR":   {"rsi_buy": 32, "rsi_sell": 68, "strategy": "trend"},
        "SOLEUR":   {"rsi_buy": 28, "rsi_sell": 72, "strategy": "mean_reversion"},
        "XXRPZEUR": {"rsi_buy": 28, "rsi_sell": 72, "strategy": "mean_reversion"},
        "XRPEUR":   {"rsi_buy": 28, "rsi_sell": 72, "strategy": "mean_reversion"},
        "ADAEUR":   {"rsi_buy": 28, "rsi_sell": 72, "strategy": "mean_reversion"},
        "DOTEUR":   {"rsi_buy": 25, "rsi_sell": 75, "strategy": "mean_reversion"},
        "LINKEUR":  {"rsi_buy": 25, "rsi_sell": 75, "strategy": "mean_reversion"},
    }


def backtest_parameter_change(config: dict, section: str, key: str,
                               new_value: float, baseline_sharpe: Optional[float]) -> dict:
    """
    Run a targeted 30-day backtest per pair, using each pair's own strategy profile.

    Each pair is tested with:
    - Its OWN RSI thresholds (from _PAIR_PROFILES_BT)
    - The proposed parameter change applied on top
    - 30 days of THAT pair's OHLC data

    Only pairs where the strategy is relevant to the changed parameter are tested.
    e.g. RSI threshold changes only affect mean_reversion pairs.
    Returns {passed, backtest_sharpe, pair_results, reason} dict.
    """
    if baseline_sharpe is None:
        return {"passed": True, "backtest_sharpe": None, "reason": "no baseline — skip gate"}

    rm = config.get("risk_management", {})
    global_tp  = float(rm.get("take_profit_percent", 2.0))
    global_sl  = float(rm.get("stop_loss_percent",   1.0))

    raw_pairs = config.get("bot_settings", {}).get("trade_pairs", ["XXBTZEUR"])
    pairs = [p.strip('"').strip("'") for p in raw_pairs if p.strip('"').strip("'")]
    if not pairs:
        pairs = ["XBTEUR"]

    # Determine which pairs are relevant for this parameter change
    def _is_relevant(pair: str) -> bool:
        profile  = _PAIR_PROFILES_BT.get(pair, {"strategy": "mean_reversion"})
        strategy = profile.get("strategy", "mean_reversion")
        # RSI changes only matter for mean_reversion pairs
        if key in ("mr_rsi_oversold_threshold", "mr_rsi_overbought_threshold"):
            return strategy == "mean_reversion"
        # TP/SL changes matter more for volatile pairs — test all but weight small caps
        return True

    pair_results = {}
    pair_sharpes = []

    for pair in pairs:
        if not _is_relevant(pair):
            continue

        closes = _fetch_ohlc_kraken(pair, interval=240, limit=180)
        if not closes:
            logger.debug("Backtest: no OHLC for %s, skipping", pair)
            continue

        # Use this pair's own profile RSI thresholds as the base
        profile  = _PAIR_PROFILES_BT.get(pair, {"rsi_buy": 30, "rsi_sell": 70})
        rsi_buy  = float(profile["rsi_buy"])
        rsi_sell = float(profile["rsi_sell"])
        tp_pct   = global_tp
        sl_pct   = global_sl

        # Apply the proposed parameter change on top of the pair's profile
        if section == "risk_management":
            if key == "mr_rsi_oversold_threshold":   rsi_buy  = new_value
            if key == "mr_rsi_overbought_threshold": rsi_sell = new_value
            if key == "take_profit_percent":         tp_pct   = new_value
            if key == "stop_loss_percent":           sl_pct   = new_value

        bt = _backtest_sharpe(closes, rsi_buy, rsi_sell, tp_pct, sl_pct)
        pair_results[pair] = bt
        if bt is not None:
            pair_sharpes.append(bt)
            logger.debug("Backtest %s (rsi %d/%d tp %.1f%% sl %.1f%%): Sharpe %.3f",
                         pair, rsi_buy, rsi_sell, tp_pct, sl_pct, bt)
        else:
            logger.debug("Backtest %s: insufficient trades", pair)

    if not pair_sharpes:
        return {"passed": True, "backtest_sharpe": None,
                "pair_results": pair_results, "reason": "no OHLC data or trades — skip gate"}

    bt_sharpe = round(sum(pair_sharpes) / len(pair_sharpes), 3)
    logger.info("Backtest gate: %d pairs tested | avg Sharpe %.3f | per pair: %s",
                len(pair_sharpes), bt_sharpe,
                {p: f"{v:.3f}" if v else "n/a" for p, v in pair_results.items()})

    # Gate: reject if backtest average is meaningfully worse than baseline
    threshold = baseline_sharpe - 0.2
    passed    = bt_sharpe >= threshold
    pairs_helped  = [p for p, v in pair_results.items() if v is not None and v >= baseline_sharpe]
    pairs_hurt    = [p for p, v in pair_results.items() if v is not None and v < baseline_sharpe - 0.1]
    reason = (
        f"avg Sharpe {bt_sharpe:.3f} vs baseline {baseline_sharpe:.3f} "
        f"({'PASS' if passed else 'FAIL'}) | "
        f"helped: {pairs_helped or 'none'} | hurt: {pairs_hurt or 'none'}"
    )
    logger.info("Backtest gate %s->%.2f: %s", key, new_value, reason)
    return {
        "passed":           passed,
        "backtest_sharpe":  bt_sharpe,
        "pair_results":     pair_results,
        "pairs_helped":     pairs_helped,
        "pairs_hurt":       pairs_hurt,
        "reason":           reason,
    }

# Each entry: (config_section, key, step_size, min_allowed, max_allowed)
TUNABLE_PARAMS: list[tuple] = [
    ("risk_management", "min_buy_score",               2.0,  5.0,  50.0),
    ("risk_management", "take_profit_percent",         0.25, 1.0,   6.0),
    ("risk_management", "stop_loss_percent",           0.5,  1.5,  10.0),
    ("risk_management", "mr_rsi_oversold_threshold",   2.0, 20.0,  45.0),
    ("risk_management", "mr_rsi_overbought_threshold", 2.0, 50.0,  75.0),
    ("risk_management", "volume_filter_min_ratio",     0.1,  0.2,   2.0),
    ("risk_management", "allocation_per_trade_percent",5.0,  5.0,  70.0),
]


# ── State helpers ──────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
        "baseline_sharpe": None,
        "trades_at_start": 0,
        "param_index": 0,
        "current_experiment": None,
        "history": [],
        "last_updated": None,
    }


def _load_state() -> dict:
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r") as fh:
                on_disk = json.load(fh)
                return {**_default_state(), **on_disk}
    except Exception as exc:
        logger.debug("Optimizer: state load failed: %s", exc)
    return _default_state()


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        with open(_STATE_FILE, "w") as fh:
            json.dump(state, fh, indent=2)
    except Exception as exc:
        logger.warning("Optimizer: state save failed: %s", exc)


# ── Config helpers ─────────────────────────────────────────────────────────────

def _read_config(path: str) -> dict:
    try:
        with open(path, "r") as fh:
            return toml.load(fh)
    except Exception as exc:
        logger.warning("Optimizer: cannot read config %s: %s", path, exc)
        return {}


def _write_config(path: str, cfg: dict) -> None:
    try:
        with open(path, "w") as fh:
            toml.dump(cfg, fh)
        logger.info("Optimizer: config written to %s", path)
    except Exception as exc:
        logger.warning("Optimizer: cannot write config %s: %s", path, exc)


def _get(cfg: dict, section: str, key: str) -> Optional[float]:
    try:
        return float(cfg[section][key])
    except (KeyError, TypeError, ValueError):
        return None


def _set(cfg: dict, section: str, key: str, value: float) -> dict:
    cfg = copy.deepcopy(cfg)
    cfg.setdefault(section, {})[key] = value
    return cfg


# ── Main entry ─────────────────────────────────────────────────────────────────

def run_optimizer(config_path: str, sharpe_result: dict) -> dict:
    """
    Called after every MIN_EVAL_TRADES closed trades (or at session end).

    Parameters
    ----------
    config_path   : active TOML config file path
    sharpe_result : dict returned by sharpe_calculator.calculate_sharpe()

    Returns
    -------
    summary dict with keys: action, detail, [new_experiment]
    """
    state         = _load_state()
    cfg           = _read_config(config_path)
    curr_sharpe   = sharpe_result.get("sharpe")
    n_trades      = sharpe_result.get("n_trades", 0)
    summary       = {"action": "none", "detail": ""}

    # ── Step 1: no baseline yet ───────────────────────────────────────────────
    if state["baseline_sharpe"] is None:
        if curr_sharpe is not None:
            state["baseline_sharpe"] = curr_sharpe
            state["trades_at_start"] = n_trades
            _save_state(state)
            msg = f"Optimizer: baseline Sharpe set to {curr_sharpe:.3f} over {n_trades} trades"
            logger.info(msg)
            return {"action": "baseline_set", "detail": msg}
        return {"action": "waiting_for_baseline", "detail": "Not enough trades yet"}

    # ── Step 2: experiment in progress — check if enough new trades ───────────
    exp = state.get("current_experiment")
    if exp is not None:
        new_trades = n_trades - state["trades_at_start"]

        if new_trades < MIN_EVAL_TRADES:
            return {
                "action": "waiting",
                "detail": (
                    f"Experiment [{exp['key']}: {exp['original']} → {exp['tested']}] — "
                    f"{new_trades}/{MIN_EVAL_TRADES} evaluation trades collected"
                ),
            }

        # Enough data — evaluate the experiment
        improved = curr_sharpe is not None and curr_sharpe > state["baseline_sharpe"]

        if improved:
            state["baseline_sharpe"] = curr_sharpe
            verdict = "kept"
            logger.info(
                "Optimizer KEPT   : %s %s → %s | Sharpe %.3f → %.3f",
                exp["key"], exp["original"], exp["tested"],
                exp["sharpe_at_start"], curr_sharpe,
            )
        else:
            # Revert the parameter
            cfg = _set(cfg, exp["section"], exp["key"], exp["original"])
            _write_config(config_path, cfg)
            verdict = "reverted"
            logger.info(
                "Optimizer REVERTED: %s %s → %s | Sharpe %.3f → %s",
                exp["key"], exp["original"], exp["tested"],
                exp["sharpe_at_start"], curr_sharpe,
            )

        # Write to persistent history DB
        if _HISTORY_DB_AVAILABLE:
            try:
                _record_optimizer_decision(
                    ts=datetime.now(timezone.utc).isoformat(),
                    param_key=exp["key"],
                    section=exp["section"],
                    old_val=exp["original"],
                    new_val=exp["tested"],
                    sharpe_before=exp["sharpe_at_start"],
                    sharpe_after=curr_sharpe,
                    verdict=verdict,
                )
            except Exception:
                pass

        # Record in history (cap at 200 entries)
        state["history"].append({
            "ts":           datetime.now(timezone.utc).isoformat(),
            "key":          exp["key"],
            "original":     exp["original"],
            "tested":       exp["tested"],
            "sharpe_before":exp["sharpe_at_start"],
            "sharpe_after": curr_sharpe,
            "verdict":      verdict,
        })
        state["history"] = state["history"][-200:]

        # Advance to the next parameter slot
        state["param_index"] = (state.get("param_index", 0) + 1) % len(TUNABLE_PARAMS)
        state["current_experiment"] = None
        state["trades_at_start"] = n_trades

        summary = {
            "action": verdict,
            "detail": f"{exp['key']}: {exp['original']} → {exp['tested']} ({verdict})",
        }

        # Reload (may have been reverted)
        cfg = _read_config(config_path)

    # ── Step 3: start a new experiment ────────────────────────────────────────
    if state.get("current_experiment") is None:
        idx = state.get("param_index", 0) % len(TUNABLE_PARAMS)

        for _ in range(len(TUNABLE_PARAMS)):
            section, key, step, lo, hi = TUNABLE_PARAMS[idx]
            cur = _get(cfg, section, key)
            if cur is None:
                idx = (idx + 1) % len(TUNABLE_PARAMS)
                continue

            if cur + step <= hi:
                new_val   = round(cur + step, 4)
                direction = "up"
            elif cur - step >= lo:
                new_val   = round(cur - step, 4)
                direction = "down"
            else:
                idx = (idx + 1) % len(TUNABLE_PARAMS)
                continue

            # ── Backtest gate: pre-screen before deploying live ───────────────
            gate = backtest_parameter_change(
                cfg, section, key, new_val, state["baseline_sharpe"]
            )
            if not gate["passed"]:
                logger.info(
                    "Optimizer SKIPPED %s %s->%.2f: backtest gate failed (%s)",
                    key, cur, new_val, gate["reason"]
                )
                idx = (idx + 1) % len(TUNABLE_PARAMS)
                continue   # try next parameter

            # Gate passed — apply the change live
            cfg = _set(cfg, section, key, new_val)
            _write_config(config_path, cfg)

            state["current_experiment"] = {
                "section":          section,
                "key":              key,
                "original":         cur,
                "tested":           new_val,
                "direction":        direction,
                "sharpe_at_start":  state["baseline_sharpe"],
                "backtest_sharpe":  gate.get("backtest_sharpe"),
                "backtest_reason":  gate.get("reason", ""),
                "pairs_helped":     gate.get("pairs_helped", []),
                "pairs_hurt":       gate.get("pairs_hurt", []),
                "pair_results":     gate.get("pair_results", {}),
            }
            state["param_index"]     = idx
            state["trades_at_start"] = n_trades

            exp_msg = (
                f"{key}: {cur} → {new_val} ({direction}) "
                f"| backtest Sharpe: {gate.get('backtest_sharpe') or '—'}"
            )
            logger.info("Optimizer NEW EXPERIMENT: %s", exp_msg)
            summary.setdefault("action", "experiment_started")
            summary["new_experiment"] = exp_msg
            break

    _save_state(state)
    return summary


def get_optimizer_history(n: int = 20) -> list:
    """Return the last N optimizer decisions for logging/reporting."""
    state = _load_state()
    return state.get("history", [])[-n:]

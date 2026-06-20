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
import os
from datetime import datetime, timezone
from typing import Optional

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
_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "optimizer_state.json")

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

            cfg = _set(cfg, section, key, new_val)
            _write_config(config_path, cfg)

            state["current_experiment"] = {
                "section":        section,
                "key":            key,
                "original":       cur,
                "tested":         new_val,
                "direction":      direction,
                "sharpe_at_start":state["baseline_sharpe"],
            }
            state["param_index"]   = idx
            state["trades_at_start"] = n_trades

            exp_msg = f"{key}: {cur} → {new_val} ({direction})"
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

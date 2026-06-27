"""Scalper AI Tuner — single-parameter A/B experiment system.

Each 25-trade trigger:
  1. Evaluate the previous experiment (keep if win rate neutral/better, revert if worse).
  2. Propose the next single-parameter change to test over the next 25 trades.

State is persisted in data/scalper_ai_state.json so experiments survive restarts.
Failed changes are remembered for 48 hours to avoid flip-flopping.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_MODELS = [
    "meta-llama/llama-3.1-8b-instruct",
    "deepseek/deepseek-chat",
    "nousresearch/hermes-3-llama-3.1-70b",
]

_BOUNDS = {
    "rsi_buy":      (25.0, 40.0),
    "rsi_sell":     (60.0, 75.0),
    "vwap_thresh":  (0.001, 0.006),
    "score_thresh": (1.5, 3.0),
    "sl_pct":       (0.15, 0.35),
}

_DEFAULTS = {
    "rsi_buy":      35.0,
    "rsi_sell":     65.0,
    "vwap_thresh":  0.003,
    "score_thresh": 2.5,
    "sl_pct":       0.20,
}

_MIN_TRADES        = 20    # minimum trades in window before AI will run
_NEUTRAL_TOLERANCE = 5.0   # pp — within 5 percentage points of baseline = neutral (keep)
_MAX_FAILED        = 10    # sliding window of remembered failed changes
_FAILED_EXPIRY_H   = 48    # hours before a failed change can be retried


class ScalperAI:
    def __init__(self, data_dir: str = "data"):
        self._data_dir         = Path(data_dir)
        self._trades_path      = self._data_dir / "scalper_trades.jsonl"
        self._params_path      = self._data_dir / "scalper_ai_params.json"
        self._adjustments_path = self._data_dir / "scalper_ai_adjustments.jsonl"
        self._state_path       = self._data_dir / "scalper_ai_state.json"
        self._api_key          = os.environ.get("OPENROUTER_API_KEY", "")

    # ── Public ────────────────────────────────────────────────────────────────

    def analyze(self) -> dict:
        """Evaluate previous experiment then propose next one. Returns new params or {}."""
        state      = self._load_state()
        all_trades = self._load_all_trades()

        if not self._api_key:
            logger.warning("[SCALP-AI] OPENROUTER_API_KEY not set — skipping")
            self._log_adjustment([], state["current_params"], state["current_params"],
                                 [], "OPENROUTER_API_KEY not set — add it to .env", success=False)
            return {}

        # Step 1: evaluate any pending experiment
        if state.get("pending_experiment"):
            self._evaluate_experiment(state, all_trades)

        # Step 2: propose next single-param experiment
        window = all_trades[-50:]
        if len(window) < _MIN_TRADES:
            logger.info("[SCALP-AI] Only %d trades in window — need %d", len(window), _MIN_TRADES)
            return {}

        return self._propose_experiment(state, all_trades, window)

    # ── Experiment evaluation ─────────────────────────────────────────────────

    def _evaluate_experiment(self, state: dict, all_trades: list):
        exp    = state["pending_experiment"]
        start  = exp["trades_at_start"]
        window = all_trades[start : start + 25]

        if len(window) < 25:
            # Shouldn't happen (we fire every 25 trades), but guard against it
            logger.info("[SCALP-AI] Experiment incomplete (%d/25 trades) — deferring", len(window))
            return

        param    = exp["param"]
        exp_wr   = self._win_rate(window)
        baseline = exp["baseline_win_rate"]
        kept     = exp_wr >= baseline - _NEUTRAL_TOLERANCE

        if kept:
            outcome = "kept"
            reason  = (f"win rate {exp_wr:.1f}% vs baseline {baseline:.1f}% "
                       f"— neutral or better, keeping change")
            logger.info("[SCALP-AI] Experiment KEPT: %s %.4f→%.4f  (%s)",
                        param, exp["old_value"], exp["new_value"], reason)
            changes = []
        else:
            outcome = "reverted"
            reason  = (f"win rate dropped to {exp_wr:.1f}% from baseline {baseline:.1f}% "
                       f"(>{_NEUTRAL_TOLERANCE:.0f}pp worse) — reverting")
            logger.info("[SCALP-AI] Experiment REVERTED: %s  (%s)", param, reason)

            # Restore old value and record failure
            state["current_params"][param] = exp["old_value"]
            direction = "increase" if exp["new_value"] > exp["old_value"] else "decrease"
            state["failed_changes"].append({
                "param":           param,
                "direction":       direction,
                "old_value":       exp["old_value"],
                "new_value":       exp["new_value"],
                "win_rate_before": baseline,
                "win_rate_after":  exp_wr,
                "tried_at":        datetime.now(timezone.utc).isoformat(),
            })
            if len(state["failed_changes"]) > _MAX_FAILED:
                state["failed_changes"] = state["failed_changes"][-_MAX_FAILED:]

            # Revert param in file so scalper picks up old value
            self._write_params(state["current_params"], state.get("pairs_blacklist", []))

            # Show reversion as a change in the dashboard
            changes = [{"param": param, "old": exp["new_value"], "new": exp["old_value"]}]

        self._log_adjustment(
            window,
            {param: exp["old_value"] if not kept else exp["new_value"]},
            state["current_params"],
            changes,
            f"[{outcome.upper()}] {param} {exp['old_value']}→{exp['new_value']} | {reason}",
            success=True,
            entry_type=f"evaluate_{outcome}",
        )

        state["pending_experiment"] = None
        self._save_state(state)

    # ── Experiment proposal ───────────────────────────────────────────────────

    def _propose_experiment(self, state: dict, all_trades: list, window: list) -> dict:
        prompt = self._build_prompt(window, state)
        raw    = self._call_openrouter(prompt)

        if not raw:
            self._log_adjustment(window, state["current_params"], state["current_params"],
                                 [], "All OpenRouter models failed — will retry next trigger",
                                 success=False)
            return {}

        suggestion = self._parse_response(raw)
        if not suggestion or "param" not in suggestion or "new_value" not in suggestion:
            self._log_adjustment(window, state["current_params"], state["current_params"],
                                 [], "Could not parse AI response — will retry next trigger",
                                 success=False)
            return {}

        param     = suggestion["param"]
        reasoning = suggestion.get("reasoning", "")
        blacklist = suggestion.get("pairs_blacklist", state.get("pairs_blacklist", []))

        if param not in _BOUNDS:
            logger.warning("[SCALP-AI] AI returned unknown param=%s — skipping", param)
            return {}

        lo, hi    = _BOUNDS[param]
        new_value = round(max(lo, min(hi, float(suggestion["new_value"]))), 4)
        old_value = float(state["current_params"].get(param, _DEFAULTS[param]))

        # Always update blacklist (independent of param experiment)
        state["pairs_blacklist"] = blacklist

        # Skip if this param+direction recently failed
        if self._is_blocked(param, new_value, old_value, state["failed_changes"]):
            logger.info("[SCALP-AI] %s %s direction recently failed — skipping param change "
                        "but still updating blacklist", param,
                        "increase" if new_value > old_value else "decrease")
            self._save_state(state)
            self._write_params(state["current_params"], blacklist)
            self._log_adjustment(
                window, state["current_params"], state["current_params"], [],
                f"Skipped: {param} {old_value}→{new_value} direction recently failed. "
                f"Blacklist updated. {reasoning}",
                success=False,
            )
            return state["current_params"]

        # No meaningful numeric change — still apply blacklist
        if abs(new_value - old_value) < 1e-6:
            logger.info("[SCALP-AI] No numeric change for %s — blacklist updated", param)
            self._save_state(state)
            self._write_params(state["current_params"], blacklist)
            return state["current_params"]

        # Calculate baseline win rate from the last 25 trades (what we're improving on)
        baseline_wr = self._win_rate(all_trades[-25:])

        # Apply experiment
        state["current_params"][param] = new_value
        state["pending_experiment"] = {
            "param":             param,
            "old_value":         old_value,
            "new_value":         new_value,
            "baseline_win_rate": baseline_wr,
            "trades_at_start":   len(all_trades),
            "started_at":        datetime.now(timezone.utc).isoformat(),
        }
        self._save_state(state)
        self._write_params(state["current_params"], blacklist)

        prev_blacklist = state.get("pairs_blacklist", [])
        changes = [{"param": param, "old": old_value, "new": new_value}]
        new_bl  = [p for p in blacklist if p not in prev_blacklist]
        if new_bl:
            changes.append({"param": "pairs_blacklist", "old": [], "new": new_bl})

        self._log_adjustment(window, {param: old_value}, state["current_params"],
                             changes, f"[PROPOSE] {reasoning}", success=True,
                             entry_type="propose")

        logger.info("[SCALP-AI] Experiment proposed: %s %.4f→%.4f  baseline_wr=%.1f%%",
                    param, old_value, new_value, baseline_wr)

        return state["current_params"]

    # ── Prompt building ───────────────────────────────────────────────────────

    def _build_prompt(self, trades: list, state: dict) -> str:
        wins     = [t for t in trades if t.get("pnl_eur", 0) > 0]
        losses   = [t for t in trades if t.get("pnl_eur", 0) < 0]
        win_rate = round(len(wins) / len(trades) * 100, 1) if trades else 0
        current  = state["current_params"]
        now_utc  = datetime.now(timezone.utc)

        # Per-pair win rates
        pair_stats = self._pair_stats(trades)
        pair_lines = []
        for p, s in sorted(pair_stats.items()):
            total = s["w"] + s["l"]
            wr    = round(s["w"] / total * 100) if total else 0
            pair_lines.append(f"  {p}: {s['w']}W/{s['l']}L ({wr}%)")

        # Per-hour win rates (UTC) — time-of-day signal
        hour_stats: dict = {}
        for t in trades:
            try:
                ts = t.get("ts", "")
                h  = datetime.fromisoformat(ts.replace("Z", "+00:00")).hour
            except Exception:
                continue
            if h not in hour_stats:
                hour_stats[h] = {"w": 0, "l": 0}
            if t.get("pnl_eur", 0) > 0:
                hour_stats[h]["w"] += 1
            else:
                hour_stats[h]["l"] += 1
        hour_lines = []
        for h in sorted(hour_stats):
            s     = hour_stats[h]
            total = s["w"] + s["l"]
            wr    = round(s["w"] / total * 100) if total else 0
            hour_lines.append(
                f"  {h:02d}:00 UTC: {s['w']}W/{s['l']}L ({wr}%) [{total} trades]"
            )

        # Failed changes — what not to retry
        failed_lines = []
        for f in state.get("failed_changes", []):
            failed_lines.append(
                f"  {f['param']} {f['direction']} "
                f"({f['old_value']}→{f['new_value']}): "
                f"WR dropped {f['win_rate_before']:.1f}% → {f['win_rate_after']:.1f}%"
            )

        # Blocked directions for the prompt
        blocked = set()
        for f in state.get("failed_changes", []):
            blocked.add(f"{f['param']} {f['direction']}")
        blocked_str = ", ".join(sorted(blocked)) if blocked else "none"

        # Pending experiment context
        pending = state.get("pending_experiment")
        pending_str = "None"
        if pending:
            pending_str = (
                f"Testing {pending['param']} {pending['old_value']}→{pending['new_value']} "
                f"(baseline WR={pending['baseline_win_rate']:.1f}%)"
            )

        # Trade log (compact)
        rows = []
        for t in trades:
            rows.append(
                f"  {t.get('pair','?'):12s} | "
                f"RSI={str(t.get('entry_rsi') or '?'):>6} | "
                f"VWAP%={str(t.get('entry_vwap_dev') or '?'):>7} | "
                f"score={str(t.get('entry_score') or '?'):>4} | "
                f"held={t.get('held_min', 0):>5.1f}m | "
                f"reason={t.get('reason', '?'):12s} | "
                f"pnl={t.get('pnl_pct', 0):>+6.3f}%"
            )

        failed_block  = "\n".join(failed_lines) if failed_lines else "  None"
        hour_block    = "\n".join(hour_lines)   if hour_lines   else "  No data"
        pair_block    = "\n".join(pair_lines)   if pair_lines   else "  No data"

        return f"""You are a crypto scalper parameter optimizer running a controlled A/B experiment system.

Each cycle you suggest ONE parameter change. It runs for 25 trades, then gets evaluated.
If win rate improves or stays neutral (within 5%), the change is kept. Otherwise it's reverted.
You then propose the next single-parameter change based on what you learn.

CURRENT TIME: {now_utc.strftime('%H:%M UTC, %A')}

CURRENT PARAMETERS (active right now):
  rsi_buy (enter long when RSI below this):  {current['rsi_buy']}
  rsi_sell (exit when RSI above this):       {current['rsi_sell']}
  vwap_thresh (% price deviation from VWAP): {current['vwap_thresh']}
  score_thresh (minimum combined score):     {current['score_thresh']}
  sl_pct (stop-loss %):                      {current['sl_pct']}

PARAMETER BOUNDS (must stay within):
  rsi_buy: 25–40  |  rsi_sell: 60–75  |  vwap_thresh: 0.001–0.006
  score_thresh: 1.5–3.0  |  sl_pct: 0.15–0.35

PENDING EXPERIMENT: {pending_str}

RECENTLY FAILED CHANGES (do NOT suggest these — they hurt performance):
{failed_block}
BLOCKED DIRECTIONS: {blocked_str}

OVERALL (last {len(trades)} trades): {len(wins)}W / {len(losses)}L — Win rate: {win_rate}%

PER-PAIR WIN RATES:
{pair_block}

WIN RATES BY HOUR OF DAY (UTC) — spot time-of-day patterns here:
{hour_block}

TRADE LOG (pair | RSI at entry | VWAP% | score | held | exit reason | P&L%):
{chr(10).join(rows)}

TASK:
1. Study the trade log and time-of-day patterns.
2. Identify the single parameter change most likely to improve win rate.
3. Consider the current time ({now_utc.strftime('%H:%M UTC')}) — if certain hours perform better/worse,
   factor that into which direction to move a parameter right now.
4. Do NOT suggest any direction listed in BLOCKED DIRECTIONS.
5. Also suggest pairs_blacklist: pairs with ≤33% win rate that should be temporarily excluded.

Respond ONLY with valid JSON (no markdown, no extra text):
{{
  "param": "<one of: rsi_buy, rsi_sell, vwap_thresh, score_thresh, sl_pct>",
  "new_value": <number within the param's bounds>,
  "pairs_blacklist": [<pair strings to blacklist, or empty list>],
  "reasoning": "<1-2 sentences: what pattern you saw and why this single change should help>"
}}"""

    # ── State management ──────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        """Load persistent experiment state, seeding from params file on first run."""
        if self._state_path.exists():
            try:
                state = json.loads(self._state_path.read_text(encoding="utf-8"))
                # Prune expired failed changes
                cutoff = datetime.now(timezone.utc).timestamp() - _FAILED_EXPIRY_H * 3600
                state["failed_changes"] = [
                    f for f in state.get("failed_changes", [])
                    if self._parse_ts(f.get("tried_at", "")) > cutoff
                ]
                return state
            except Exception as exc:
                logger.warning("[SCALP-AI] State file corrupt — resetting: %s", exc)

        # First run — seed current_params from existing params file
        current_params  = dict(_DEFAULTS)
        pairs_blacklist = []
        try:
            if self._params_path.exists():
                p = json.loads(self._params_path.read_text(encoding="utf-8"))
                for k in _DEFAULTS:
                    if k in p:
                        lo, hi = _BOUNDS[k]
                        current_params[k] = max(lo, min(hi, float(p[k])))
                pairs_blacklist = p.get("pairs_blacklist", [])
        except Exception:
            pass

        return {
            "current_params":    current_params,
            "pairs_blacklist":   pairs_blacklist,
            "pending_experiment": None,
            "failed_changes":    [],
        }

    def _save_state(self, state: dict):
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self._state_path)
        except Exception as exc:
            logger.warning("[SCALP-AI] Could not save state: %s", exc)

    # ── Data helpers ──────────────────────────────────────────────────────────

    def _load_all_trades(self) -> list:
        trades = []
        try:
            with open(self._trades_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            trades.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass
        return trades

    def _win_rate(self, trades: list) -> float:
        if not trades:
            return 0.0
        wins = sum(1 for t in trades if t.get("pnl_eur", 0) > 0)
        return round(wins / len(trades) * 100, 1)

    def _pair_stats(self, trades: list) -> dict:
        stats: dict = {}
        for t in trades:
            p = t.get("pair", "?")
            if p not in stats:
                stats[p] = {"w": 0, "l": 0}
            if t.get("pnl_eur", 0) > 0:
                stats[p]["w"] += 1
            else:
                stats[p]["l"] += 1
        return stats

    def _is_blocked(self, param: str, new_val: float, old_val: float,
                    failed: list) -> bool:
        direction = "increase" if new_val > old_val else "decrease"
        return any(
            f.get("param") == param and f.get("direction") == direction
            for f in failed
        )

    def _parse_ts(self, ts_str: str) -> float:
        try:
            return datetime.fromisoformat(
                ts_str.replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            return 0.0

    # ── File I/O ──────────────────────────────────────────────────────────────

    def _write_params(self, params: dict, blacklist: list):
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            out = dict(params)
            out["pairs_blacklist"] = blacklist
            out["updated_at"]      = datetime.now(timezone.utc).isoformat()
            tmp = self._params_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self._params_path)
        except Exception as exc:
            logger.warning("[SCALP-AI] Could not write params: %s", exc)

    def _log_adjustment(self, trades: list, old: dict, new: dict,
                        changes: list, reasoning: str,
                        success: bool = True, entry_type: str = "propose"):
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            wins     = sum(1 for t in trades if t.get("pnl_eur", 0) > 0)
            win_rate = round(wins / len(trades) * 100, 1) if trades else 0
            entry = {
                "ts":              datetime.now(timezone.utc).isoformat(),
                "type":            entry_type,
                "trades_analyzed": len(trades),
                "win_rate":        win_rate,
                "changes":         changes,
                "params_before":   old,
                "params_after":    new,
                "pair_stats":      self._pair_stats(trades),
                "reasoning":       reasoning,
                "success":         success,
            }
            with open(self._adjustments_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as exc:
            logger.warning("[SCALP-AI] Could not log adjustment: %s", exc)

    def _call_openrouter(self, prompt: str) -> str:
        try:
            import openai
        except ImportError:
            logger.warning("[SCALP-AI] openai package not installed")
            return ""
        for model in _MODELS:
            try:
                client = openai.OpenAI(
                    api_key=self._api_key,
                    base_url="https://openrouter.ai/api/v1",
                    default_headers={"HTTP-Referer": "https://github.com/tradingbot"},
                )
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=512,
                )
                content = resp.choices[0].message.content.strip()
                logger.info("[SCALP-AI] Got response from %s (%d chars)", model, len(content))
                return content
            except Exception as exc:
                logger.warning("[SCALP-AI] Model %s failed: %s", model, exc)
        return ""

    def _parse_response(self, raw: str) -> dict:
        try:
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())
        except Exception as exc:
            logger.warning("[SCALP-AI] Parse error: %s | raw: %.200s", exc, raw)
            return {}

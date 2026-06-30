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
    "rsi_recovery_thresh": (25.0, 40.0),
    "rsi_sell":            (60.0, 75.0),
    "vol_mult":            (1.1,  3.0),
    "score_thresh":        (2.0,  5.0),
    "sl_pct":              (0.30, 0.80),
    "max_hold_min":        (30.0, 180.0),
}

_DEFAULTS = {
    "rsi_recovery_thresh": 35.0,
    "rsi_sell":            65.0,
    "vol_mult":            1.5,
    "score_thresh":        4.0,
    "sl_pct":              0.50,
    "max_hold_min":        120.0,
}

_MIN_TRADES        = 20    # minimum trades in window before AI will run
_NEUTRAL_TOLERANCE = 5.0   # pp — within 5 percentage points of baseline = neutral (keep)
_MAX_FAILED        = 10    # sliding window of remembered failed changes
_FAILED_EXPIRY_H   = 24    # hours before a failed change can be retried
_MAX_BLACKLIST     = 5     # never blacklist more than this many pairs at once


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
        window = all_trades[-75:]  # 3 full cycles for broader context
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
        # Always rebuild blacklist fresh from current window — never inherit stale state.
        # Pairs that were bad under old params may be fine under new ones.
        blacklist = suggestion.get("pairs_blacklist", [])

        if param not in _BOUNDS:
            logger.warning("[SCALP-AI] AI returned unknown param=%s — skipping", param)
            return {}

        lo, hi    = _BOUNDS[param]
        new_value = round(max(lo, min(hi, float(suggestion["new_value"]))), 4)
        old_value = float(state["current_params"].get(param, _DEFAULTS[param]))

        # Strip any pair the AI wants to blacklist that actually has decent recent WR.
        # AI only sees a text summary — it can misjudge pairs it hasn't seen much data for.
        if blacklist:
            pair_wrs = self._pair_stats(window)
            filtered = []
            for p in blacklist:
                s = pair_wrs.get(p)
                if s and (s["w"] + s["l"]) >= 3:
                    wr = s["w"] / (s["w"] + s["l"])
                    if wr > 0.50:
                        logger.info("[SCALP-AI] Dropping %s from blacklist: WR=%.0f%% in window", p, wr * 100)
                        continue
                filtered.append(p)
            blacklist = filtered

        # Cap blacklist — if overall WR is low it's a market condition, not pair-specific
        if len(blacklist) > _MAX_BLACKLIST:
            pair_wrs = self._pair_stats(window)
            ranked   = sorted(pair_wrs.items(),
                              key=lambda x: x[1]["w"] / (x[1]["w"] + x[1]["l"])
                              if (x[1]["w"] + x[1]["l"]) >= 3 else 1.0)
            blacklist = [p for p, _ in ranked[:_MAX_BLACKLIST]]
            logger.info("[SCALP-AI] Blacklist capped at %d pairs (was %d suggested)",
                        _MAX_BLACKLIST, len(suggestion.get("pairs_blacklist", [])))

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

    def _load_backtest_recs(self) -> dict:
        """Load backtest recommendations if they exist and are under 7 days old."""
        recs_path = self._data_dir / "backtest_recommendations.json"
        try:
            if not recs_path.exists():
                return {}
            recs     = json.loads(recs_path.read_text(encoding="utf-8"))
            age_secs = datetime.now(timezone.utc).timestamp() - self._parse_ts(recs.get("generated_at", ""))
            if age_secs > 7 * 24 * 3600:
                return {}
            return recs
        except Exception:
            return {}

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

        # Last 3 experiment cycles — trajectory context
        recent_cycles = self._load_recent_cycles(3)
        cycle_lines = []
        for c in recent_cycles:
            ctype   = c.get("type", "?")
            changes = c.get("changes", [])
            change_str = ", ".join(
                f"{ch['param']} {ch['old']}→{ch['new']}" for ch in changes
            ) if changes else "no param change"
            cycle_lines.append(
                f"  [{ctype.upper()}] WR={c.get('win_rate','?')}% | "
                f"{change_str} | {c.get('reasoning','')[:120]}"
            )
        cycle_block = "\n".join(cycle_lines) if cycle_lines else "  No history yet"

        # Trade log (compact)
        rows = []
        for t in trades:
            rows.append(
                f"  {t.get('pair','?'):12s} | "
                f"bounce={str(t.get('entry_vwap_bounce') or '?'):>5} | "
                f"RSI={str(t.get('entry_rsi') or '?'):>5} | "
                f"rising={str(t.get('entry_rsi_delta') or '?'):>5} | "
                f"vol_r={str(t.get('entry_volume_ratio') or '?'):>5} | "
                f"score={str(t.get('entry_score') or '?'):>4} | "
                f"held={t.get('held_min', 0):>5.1f}m | "
                f"reason={t.get('reason', '?'):12s} | "
                f"pnl={t.get('pnl_pct', 0):>+6.3f}%"
            )

        failed_block = "\n".join(failed_lines) if failed_lines else "  None"
        hour_block   = "\n".join(hour_lines)   if hour_lines   else "  No data"
        pair_block   = "\n".join(pair_lines)   if pair_lines   else "  No data"

        # Backtest recommendations block — prefer new-signal data if available
        recs = self._load_backtest_recs()
        if recs:
            age_h = (datetime.now(timezone.utc).timestamp()
                     - self._parse_ts(recs.get("generated_at", ""))) / 3600
            rec_lines = [f"  Based on {recs['trade_count']} real trades, generated {age_h:.0f}h ago."]
            # Walk-forward validated params take highest priority
            if recs.get("wf_best"):
                wf = recs["wf_best"]
                rec_lines += [
                    f"  WALK-FORWARD VALIDATED (out-of-sample, most reliable):",
                    f"    Stable combo: vol_mult≥{wf['vol_mult_min']}, score_thresh≥{wf['score_min']}",
                    f"    Won {wf['windows_won']}/{wf['windows_total']} windows ({wf['stability_pct']}% stability)",
                    f"    OOS win rate: {wf['oos_win_rate']}% [{wf['oos_wilson_lo']}%–{wf['oos_wilson_hi']}%] "
                    f"P(>50%)={wf['oos_prob_above_50']}% n={wf['oos_n_trades']}",
                    f"    These params held up on UNSEEN data — weight them heavily.",
                ]
            # New-signal full-history grid
            if recs.get("new_signal_best"):
                best = recs["new_signal_best"]
                rec_lines += [
                    f"  NEW SIGNAL (VWAP Reclaim) best combo: vol_mult ≥ {best['vol_mult_min']}, Score ≥ {best['score_min']}",
                    f"    Win rate: {best['win_rate']}%  Wilson CI: [{best['wilson_lo']}%–{best['wilson_hi']}%]",
                    f"    P(true WR > 50%): {best['prob_above_50']}%  |  n={best['n_trades']}  |  BH-significant: {'YES ★' if best['bh_significant'] else 'NO'}",
                    f"  Top 3 new-signal combinations:",
                ]
                for i, c in enumerate(recs.get("new_signal_top_combinations", [])[:3], 1):
                    sig = "★" if c["bh_significant"] else " "
                    rec_lines.append(
                        f"    {i}.{sig} vol_mult ≥ {c['vol_mult_min']}, Score ≥ {c['score_min']} → "
                        f"{c['win_rate']}% WR [{c['wilson_lo']}%–{c['wilson_hi']}%], "
                        f"P(>50%)={c['prob_above_50']}%, n={c['n_trades']}"
                    )
            elif recs.get("top_combinations"):
                # Fall back to legacy RSI-grid recs for context
                best = recs["best"]
                rec_lines += [
                    f"  Legacy RSI-grid best (old mean-reversion signal, for context only):",
                    f"    RSI ≤ {best['rsi_buy_max']}, Score ≥ {best['score_min']} → "
                    f"{best['win_rate']}% WR, n={best['n_trades']}",
                ]
            ts = recs.get("timeout_stats")
            if ts:
                rec_lines.append(
                    f"  TIMEOUT STATS (max_hold_min={ts.get('current_max_hold_min', '?')}m): "
                    f"{ts['count']} timeouts ({ts['pct_of_trades']}% of trades), "
                    f"WR={ts['win_rate']}%, avg P&L={ts['avg_pnl_pct']:+.3f}%, avg hold={ts['avg_hold_min']:.0f}m. "
                    f"If timeout rate is high and avg P&L is near zero, consider reducing max_hold_min."
                )
            bad_hours = recs.get("bad_hours_utc", [])
            if bad_hours:
                rec_lines.append(
                    f"  GATED HOURS (entries blocked by backtest, already applied): UTC {sorted(bad_hours)}"
                )
            backtest_block = "\n".join(rec_lines)
        else:
            backtest_block = "  Not available yet — run the local backtest tool first."

        return f"""You are a crypto scalper parameter optimizer running a controlled A/B experiment system.

Each cycle you suggest ONE parameter change. It runs for 25 trades, then gets evaluated.
If win rate improves or stays neutral (within 5%), the change is kept. Otherwise it's reverted.
You then propose the next single-parameter change based on what you learn.

SIGNAL DESIGN (VWAP Reclaim + Momentum — max score 6, entry threshold = score_thresh):
  VWAP Bounce  (+2): price crossed from below VWAP to above in last 3 bars
  RSI Turning  (+2): RSI < rsi_recovery_thresh AND rising vs 3 bars ago
  Volume Spike (+1): current bar volume > vol_mult × 20-bar average
  OB Bid-Heavy (+1): bid volume > ask volume by 20%+
  RSI Overbought(-2): RSI > rsi_sell (exit signal only)

CURRENT TIME: {now_utc.strftime('%H:%M UTC, %A')}

CURRENT PARAMETERS (active right now):
  rsi_recovery_thresh (RSI must be below this AND rising to score +2): {current['rsi_recovery_thresh']}
  rsi_sell (RSI overbought exit threshold):   {current['rsi_sell']}
  vol_mult (volume spike multiplier vs 20-bar avg): {current['vol_mult']}
  score_thresh (minimum combined score to enter, max=6): {current['score_thresh']}
  sl_pct (stop-loss %):                       {current['sl_pct']}
  max_hold_min (force-exit after N minutes):  {current['max_hold_min']}

PARAMETER BOUNDS (must stay within):
  rsi_recovery_thresh: 30–55  |  rsi_sell: 60–75  |  vol_mult: 1.1–3.0
  score_thresh: 2.0–5.0  |  sl_pct: 0.30–0.80  |  max_hold_min: 30–180

PENDING EXPERIMENT: {pending_str}

RECENTLY FAILED CHANGES (do NOT suggest these — they hurt performance):
{failed_block}
BLOCKED DIRECTIONS: {blocked_str}

BACKTEST INSIGHTS (statistically validated from full trade history):
{backtest_block}
NOTE: Prioritise WALK-FORWARD params over full-history grid (walk-forward proved robust on unseen data).
If current vol_mult or score_thresh differ from walk-forward validated values, move toward them
unless that direction is blocked by recent live experiment failures.

LAST 3 EXPERIMENT CYCLES (most recent last):
{cycle_block}
Use this trajectory to understand where the market is heading — not just where it was.
If a direction was tried and reverted, consider whether market conditions have since changed.

OVERALL (last {len(trades)} trades): {len(wins)}W / {len(losses)}L — Win rate: {win_rate}%

PER-PAIR WIN RATES:
{pair_block}

WIN RATES BY HOUR OF DAY (UTC) — spot time-of-day patterns here:
{hour_block}

TRADE LOG (pair | RSI at entry | VWAP% | score | held | exit reason | P&L%):
{chr(10).join(rows)}

TASK:
1. Study the last 3 experiment cycles to understand the TRAJECTORY — what has been tried, what worked, what didn't.
2. Based on where the market is NOW (not where it was 75 trades ago), identify the single parameter change most likely to improve win rate.
3. Consider the current time ({now_utc.strftime('%H:%M UTC')}) — factor time-of-day patterns into your suggestion.
4. Do NOT suggest any direction listed in BLOCKED DIRECTIONS (unless market conditions have clearly shifted).
5. For pairs_blacklist: only blacklist pairs with ≤33% WR AND at least 3 trades in this 75-trade window.
   A pair that was bad under old parameters may be fine now — only blacklist based on CURRENT window data.
   If overall win rate is below 40%, the problem is market conditions not specific pairs, so return an empty list.

Respond ONLY with valid JSON (no markdown, no extra text):
{{
  "param": "<one of: rsi_recovery_thresh, rsi_sell, vol_mult, score_thresh, sl_pct, max_hold_min>",
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
                # Migrate current_params to new signal design.
                # If the state was written by the old signal (has rsi_buy), reset
                # ALL params to defaults — old score_thresh=2.0 / sl_pct=0.3 are
                # wrong for the new 6-point scoring system even though in-bounds.
                raw = state.get("current_params", {})
                if "rsi_buy" in raw:
                    logger.info("[SCALP-AI] Old-signal state detected — resetting all params to new defaults")
                    state["current_params"] = dict(_DEFAULTS)
                else:
                    state["current_params"] = {
                        k: max(lo, min(hi, float(raw[k]))) if k in raw else _DEFAULTS[k]
                        for k, (lo, hi) in _BOUNDS.items()
                    }
                # Also drop any pending experiment that references a now-unknown param
                pe = state.get("pending_experiment")
                if pe and pe.get("param") not in _BOUNDS:
                    logger.info("[SCALP-AI] Dropping stale pending_experiment for unknown param %s",
                                pe.get("param"))
                    state["pending_experiment"] = None
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

    def _load_recent_cycles(self, n: int = 3) -> list:
        """Return the last n completed AI cycles from the adjustment log."""
        try:
            if not self._adjustments_path.exists():
                return []
            lines = self._adjustments_path.read_text(encoding="utf-8").strip().split("\n")
            entries = []
            for line in reversed(lines):
                try:
                    e = json.loads(line)
                    if e.get("type", "").startswith(("propose", "evaluate")):
                        entries.append(e)
                        if len(entries) >= n:
                            break
                except Exception:
                    pass
            return list(reversed(entries))
        except Exception:
            return []

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
            # Propagate bad hours from latest backtest — these come from data, not AI
            recs = self._load_backtest_recs()
            out["skip_hours_utc"] = recs.get("bad_hours_utc", [])
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

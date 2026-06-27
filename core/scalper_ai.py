"""Scalper AI Tuner — analyzes closed trade history and adjusts scalper params.

Triggered every 25 trades by ScalperEngine._run_ai_review().
Calls a free OpenRouter model, validates suggestions against hard bounds,
writes data/scalper_ai_params.json (picked up by scalper on next loop),
and appends to data/scalper_ai_adjustments.jsonl (read by dashboard).
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Models tried in order — first success wins (same pool as main AI panel)
_FREE_MODELS = [
    "meta-llama/llama-3.1-8b-instruct",
    "deepseek/deepseek-chat",
    "nousresearch/hermes-3-llama-3.1-70b",
]

# Hard bounds — identical to scalper.py _AI_BOUNDS (duplication intentional for safety)
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
    "score_thresh": 1.5,
    "sl_pct":       0.20,
}

_MIN_TRADES = 20   # won't run analysis with fewer trades than this


class ScalperAI:
    def __init__(self, data_dir: str = "data"):
        self._data_dir        = Path(data_dir)
        self._trades_path     = self._data_dir / "scalper_trades.jsonl"
        self._params_path     = self._data_dir / "scalper_ai_params.json"
        self._adjustments_path= self._data_dir / "scalper_ai_adjustments.jsonl"
        self._api_key         = os.environ.get("OPENROUTER_API_KEY", "")

    # ── Public ─────────────────────────────────────────────────────────────────

    def analyze(self) -> dict:
        """Run analysis, write params file. Returns new params dict or {}."""
        trades  = self._load_trades(n=50)
        current = self._load_current_params()

        if not self._api_key:
            logger.warning("[SCALP-AI] OPENROUTER_API_KEY not set — skipping AI review")
            self._log_adjustment(trades, current, current, [],
                                 "OPENROUTER_API_KEY not set — add it to .env to enable AI tuning",
                                 success=False)
            return {}

        if len(trades) < _MIN_TRADES:
            logger.info("[SCALP-AI] Only %d trades — need %d to analyze", len(trades), _MIN_TRADES)
            self._log_adjustment(trades, current, current, [],
                                 f"Insufficient data: {len(trades)} trades (need {_MIN_TRADES})",
                                 success=False)
            return {}

        prompt = self._build_prompt(trades, current)
        raw    = self._call_openrouter(prompt)
        if not raw:
            self._log_adjustment(trades, current, current, [],
                                 "All OpenRouter models failed to respond — will retry next trigger",
                                 success=False)
            return {}

        suggested = self._parse_response(raw)
        if not suggested:
            self._log_adjustment(trades, current, current, [],
                                 "Could not parse AI response — will retry next trigger",
                                 success=False)
            return {}

        validated = self._validate(suggested, current)
        changes   = self._diff(current, validated, suggested.get("pairs_blacklist", []))

        self._write_params(validated, suggested.get("pairs_blacklist", []))
        self._log_adjustment(trades, current, validated, changes, suggested.get("reasoning", ""),
                             success=True)

        if changes:
            logger.info("[SCALP-AI] Params updated: %s", changes)
        else:
            logger.info("[SCALP-AI] Analysis complete — no param changes needed")

        return validated

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _load_trades(self, n: int = 50) -> list:
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
        return trades[-n:]

    def _load_current_params(self) -> dict:
        try:
            if self._params_path.exists():
                p = json.loads(self._params_path.read_text())
                return {k: float(p.get(k, v)) for k, v in _DEFAULTS.items()}
        except Exception:
            pass
        return dict(_DEFAULTS)

    def _build_prompt(self, trades: list, current: dict) -> str:
        wins   = [t for t in trades if t.get("pnl_eur", 0) > 0]
        losses = [t for t in trades if t.get("pnl_eur", 0) < 0]
        win_rate = round(len(wins) / len(trades) * 100, 1) if trades else 0

        # Per-pair win rates
        pair_stats: dict = {}
        for t in trades:
            p = t.get("pair", "?")
            if p not in pair_stats:
                pair_stats[p] = {"w": 0, "l": 0}
            if t.get("pnl_eur", 0) > 0:
                pair_stats[p]["w"] += 1
            else:
                pair_stats[p]["l"] += 1
        pair_lines = []
        for p, s in sorted(pair_stats.items()):
            total = s["w"] + s["l"]
            wr    = round(s["w"] / total * 100) if total else 0
            pair_lines.append(f"  {p}: {s['w']}W/{s['l']}L ({wr}%)")

        # Trade table (last 50, compact)
        rows = []
        for t in trades:
            rows.append(
                f"  {t.get('pair','?'):12s} | "
                f"RSI={t.get('entry_rsi') or '?':>6} | "
                f"VWAP%={t.get('entry_vwap_dev') or '?':>7} | "
                f"OB={t.get('entry_ob_imbalance') or '?':>6} | "
                f"score={t.get('entry_score') or '?':>4} | "
                f"held={t.get('held_min',0):>5.1f}m | "
                f"reason={t.get('reason','?'):12s} | "
                f"pnl={t.get('pnl_pct',0):>+6.3f}%"
            )

        return f"""You are a crypto scalper parameter optimizer. Analyze the trade history below and suggest parameter adjustments to improve the win rate above {win_rate}%.

CURRENT PARAMETERS:
  rsi_buy (buy when RSI below this):  {current['rsi_buy']}
  rsi_sell (sell when RSI above this): {current['rsi_sell']}
  vwap_thresh (% deviation to signal): {current['vwap_thresh']}
  score_thresh (min score to enter):   {current['score_thresh']}
  sl_pct (stop-loss %):               {current['sl_pct']}

OVERALL: {len(trades)} trades | {len(wins)}W / {len(losses)}L | Win rate: {win_rate}%

PER-PAIR WIN RATES:
{chr(10).join(pair_lines)}

TRADE LOG (pair | RSI at entry | VWAP deviation % | OB imbalance | score | held | exit reason | P&L%):
{chr(10).join(rows)}

PARAMETER BOUNDS (hard limits — stay within these):
  rsi_buy:      25.0 – 40.0
  rsi_sell:     60.0 – 75.0
  vwap_thresh:  0.001 – 0.006
  score_thresh: 1.5 – 3.0
  sl_pct:       0.15 – 0.35
  pairs_blacklist: any subset of the pairs above (temporarily disable worst performers)

Respond ONLY with valid JSON — no markdown, no explanation outside the JSON:
{{
  "rsi_buy": <number>,
  "rsi_sell": <number>,
  "vwap_thresh": <number>,
  "score_thresh": <number>,
  "sl_pct": <number>,
  "pairs_blacklist": [<pair strings>],
  "reasoning": "<1-2 sentences explaining the key changes>"
}}"""

    def _call_openrouter(self, prompt: str) -> str:
        try:
            import openai
        except ImportError:
            logger.warning("[SCALP-AI] openai package not installed")
            return ""
        for model in _FREE_MODELS:
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
            # Strip markdown fences if present
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())
        except Exception as exc:
            logger.warning("[SCALP-AI] Could not parse response: %s | raw: %.200s", exc, raw)
            return {}

    def _validate(self, suggested: dict, current: dict) -> dict:
        """Clamp all values to hard bounds. Fall back to current if missing."""
        result = {}
        for key, (lo, hi) in _BOUNDS.items():
            raw = suggested.get(key, current.get(key, _DEFAULTS[key]))
            try:
                result[key] = round(max(lo, min(hi, float(raw))), 4)
            except Exception:
                result[key] = current.get(key, _DEFAULTS[key])
        return result

    def _diff(self, current: dict, validated: dict, blacklist: list) -> list:
        changes = []
        for key in _BOUNDS:
            old = current.get(key, _DEFAULTS[key])
            new = validated.get(key)
            if new is not None and abs(new - old) > 1e-6:
                changes.append({"param": key, "old": old, "new": new})
        if blacklist:
            changes.append({"param": "pairs_blacklist", "old": [], "new": blacklist})
        return changes

    def _write_params(self, validated: dict, blacklist: list):
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            out = dict(validated)
            out["pairs_blacklist"] = blacklist
            out["updated_at"] = datetime.now(timezone.utc).isoformat()
            tmp = self._params_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, separators=(",", ":")))
            tmp.replace(self._params_path)
        except Exception as exc:
            logger.warning("[SCALP-AI] Could not write params: %s", exc)

    def _log_adjustment(self, trades: list, old: dict, new: dict,
                        changes: list, reasoning: str, success: bool = True):
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            wins     = sum(1 for t in trades if t.get("pnl_eur", 0) > 0)
            win_rate = round(wins / len(trades) * 100, 1) if trades else 0

            # Per-pair breakdown for display
            pair_stats: dict = {}
            for t in trades:
                p = t.get("pair", "?")
                if p not in pair_stats:
                    pair_stats[p] = {"w": 0, "l": 0}
                if t.get("pnl_eur", 0) > 0:
                    pair_stats[p]["w"] += 1
                else:
                    pair_stats[p]["l"] += 1

            entry = {
                "ts":              datetime.now(timezone.utc).isoformat(),
                "trades_analyzed": len(trades),
                "win_rate":        win_rate,
                "changes":         changes,
                "params_before":   old,
                "params_after":    new,
                "pair_stats":      pair_stats,
                "reasoning":       reasoning,
                "success":         success,
            }
            with open(self._adjustments_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as exc:
            logger.warning("[SCALP-AI] Could not log adjustment: %s", exc)

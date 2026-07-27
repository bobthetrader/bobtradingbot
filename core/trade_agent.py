"""AI Trade Desk — real-time buy/skip decisions via the Claude Code CLI.

The final decision maker for buy candidates that survive every deterministic
gate. Runs `claude -p` (print mode) authenticated with the user's Claude Max
subscription (`claude setup-token` -> CLAUDE_CODE_OAUTH_TOKEN env var), so
there is NO metered API billing — the worst failure mode is a rate limit.

Every failure path (CLI missing, timeout, malformed output, daily call cap)
returns None, which tells the caller to fall back to the bot's existing
rule-based behaviour. The agent can therefore only ever REFINE the rules,
never break the bot.

Decisions are journaled to data/agent_decisions.jsonl for the nightly tuner
(core/agent_tuner.py) and the dashboard.
"""

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DECISIONS_FILE = "agent_decisions.jsonl"
POLICY_FILE = "agent_policy.json"

# Hard rails — the model's output can never leave these ranges
SIZE_MULT_MIN, SIZE_MULT_MAX = 0.5, 1.3
DEFAULT_MAX_CALLS_PER_DAY = 40
DEFAULT_TIMEOUT_SECONDS = 25
FAILURE_COOLDOWN_SECONDS = 3600   # hard CLI failure -> agent off for 1h

_PROMPT_TEMPLATE = """You are the trade desk for a small crypto paper-trading bot on Kraken (EUR pairs, long-only spot, ~EUR50-70 per position, round-trip fees ~0.8% taker / ~0.5% maker). Your ONLY objective is net profit after fees. Marginal edges lose money to fees — when the edge is unclear, skip.

The bot's rule gates (RSI bands, trading hours, trend filters, smart-money veto) have already passed this candidate. You make the final call and size it.

PLAYBOOK (lessons learned from this bot's own closed trades — follow unless clearly inapplicable):
{playbook}

CANDIDATE:
{candidate}

PORTFOLIO STATE:
{portfolio}

Respond with ONLY a single JSON object on one line, no markdown fences, no other text:
{{"decision": "buy" or "skip", "size_mult": number 0.5-1.3, "confidence": number 0.0-1.0, "reason": "max 300 chars — name the specific signals that drove the call, this is shown on the trading dashboard"}}

Guidance: size_mult above 1.0 only with real confluence (multiple independent bullish signals); below 1.0 when taking a defensible but weaker setup; skip beats a low-conviction buy. Do not use any tools."""


class TradeAgent:
    """Real-time buy/skip decision maker backed by the Claude Code CLI."""

    def __init__(self, data_dir: str, config_getter, model: str = "sonnet"):
        """config_getter: zero-arg callable returning the live [trade_agent]
        config dict — lets the bot's 60s config hot-reload toggle the agent."""
        self._data_dir = data_dir
        self._config = config_getter
        self._model = model
        self._cli = shutil.which("claude") or shutil.which("claude.cmd")
        self._calls_date = None      # UTC date string the counter belongs to
        self._calls_today = 0
        self._disabled_until = 0.0   # monotonic-ish cooldown after hard failures
        self._policy_cache = None
        self._policy_mtime = 0.0
        if self._cli is None:
            logger.warning("TradeAgent: claude CLI not found on PATH — agent will fall back to rules")

    # ── public API ────────────────────────────────────────────────────────────

    def enabled(self) -> bool:
        try:
            return bool(self._config().get("enabled", False))
        except Exception:
            return False

    def decide(self, ctx: dict):
        """Return {"decision","size_mult","confidence","reason"} or None.

        None means: fall back to the bot's existing rule behaviour. Never
        raises. ctx must contain at least pair and price; everything else is
        best-effort context for the model.
        """
        if not self.enabled():
            return None
        pair = ctx.get("pair", "?")
        if self._cli is None:
            self._journal(pair, ctx, None, source="no_cli")
            return None
        if time.time() < self._disabled_until:
            self._journal(pair, ctx, None, source="cooldown")
            return None
        if not self._under_call_cap():
            self._journal(pair, ctx, None, source="call_cap")
            return None

        prompt = self._build_prompt(ctx)
        self._calls_today += 1        # count attempts, not successes
        started = time.time()
        raw = self._call_cli(prompt)
        latency_ms = int((time.time() - started) * 1000)

        if raw is None:
            self._journal(pair, ctx, None, source="cli_error", latency_ms=latency_ms)
            return None

        decision = self._parse_decision(raw)
        if decision is None:
            self._journal(pair, ctx, None, source="bad_output",
                          latency_ms=latency_ms, raw=raw[:500])
            return None

        # Tuner-controlled confidence floor: low-conviction buys become skips
        floor = float(self._policy().get("knobs", {}).get("min_confidence", 0.0) or 0.0)
        if decision["decision"] == "buy" and decision["confidence"] < floor:
            decision["decision"] = "skip"
            decision["reason"] = (decision["reason"][:150]
                                  + f" [conf {decision['confidence']:.2f} < floor {floor:.2f}]")

        self._journal(pair, ctx, decision, source="agent", latency_ms=latency_ms)
        return decision

    # ── internals ─────────────────────────────────────────────────────────────

    def _under_call_cap(self) -> bool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._calls_date != today:
            self._calls_date = today
            self._calls_today = 0
        cap = int(self._config().get("max_calls_per_day", DEFAULT_MAX_CALLS_PER_DAY))
        return self._calls_today < cap

    def _policy(self) -> dict:
        """agent_policy.json (playbook + knobs), cached by mtime."""
        path = os.path.join(self._data_dir, POLICY_FILE)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return {}
        if self._policy_cache is None or mtime != self._policy_mtime:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._policy_cache = json.load(f) or {}
                self._policy_mtime = mtime
            except Exception as exc:
                logger.debug("TradeAgent: policy load failed: %s", exc)
                return self._policy_cache or {}
        return self._policy_cache or {}

    def _build_prompt(self, ctx: dict) -> str:
        playbook = (self._policy().get("playbook") or
                    "(no playbook yet — first trades still being gathered; be conservative)")
        candidate = {k: ctx.get(k) for k in (
            "pair", "price", "score", "strategy", "rsi_1h", "smart_action",
            "whale_score", "hl_bias", "panel_score", "hour_utc",
            # new-listing candidates carry these instead of signal features
            "setup", "listing_source", "detected_price",
            "move_since_detection_pct", "minutes_since_detection", "cap_eur",
        ) if ctx.get(k) is not None}
        portfolio = {k: ctx.get(k) for k in (
            "open_positions", "open_count", "max_positions",
            "portfolio_eur", "consecutive_losses",
        ) if ctx.get(k) is not None}
        return _PROMPT_TEMPLATE.format(
            playbook=playbook[:1500],
            candidate=json.dumps(candidate, default=str),
            portfolio=json.dumps(portfolio, default=str),
        )

    def _call_cli(self, prompt: str):
        """One `claude -p` round trip. Returns the result text or None.

        The prompt goes in via STDIN, not argv — multi-line prompts passed as
        an argument get mangled by the Windows .CMD shim (observed hang) and
        risk the cmd.exe 8191-char limit."""
        timeout = float(self._config().get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        cmd = [self._cli, "-p",
               "--output-format", "json",
               "--model", self._model,
               "--strict-mcp-config"]
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                cwd=self._data_dir,   # avoid loading the repo CLAUDE.md as context
            )
        except subprocess.TimeoutExpired:
            logger.warning("TradeAgent: CLI timed out after %.0fs", timeout)
            return None
        except Exception as exc:
            logger.warning("TradeAgent: CLI launch failed: %s — cooling down 1h", exc)
            self._disabled_until = time.time() + FAILURE_COOLDOWN_SECONDS
            return None
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:300]
            logger.warning("TradeAgent: CLI exit %d: %s", proc.returncode, err)
            # Auth/rate-limit style failures: cool down so we don't hammer
            if any(t in err.lower() for t in ("auth", "login", "credit", "rate", "limit")):
                self._disabled_until = time.time() + FAILURE_COOLDOWN_SECONDS
            return None
        try:
            envelope = json.loads(proc.stdout)
            if isinstance(envelope, dict):
                if envelope.get("is_error"):
                    logger.warning("TradeAgent: CLI reported error result")
                    return None
                return envelope.get("result") or ""
        except Exception:
            pass
        return proc.stdout  # non-envelope output; let the parser try

    @staticmethod
    def _parse_decision(text: str):
        """Extract + validate the decision JSON. None if anything is off."""
        if not text:
            return None
        try:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                return None
            obj = json.loads(text[start:end + 1])
            decision = str(obj.get("decision", "")).lower().strip()
            if decision not in ("buy", "skip"):
                return None
            size = float(obj.get("size_mult", 1.0))
            size = max(SIZE_MULT_MIN, min(SIZE_MULT_MAX, size))
            conf = float(obj.get("confidence", 0.5))
            conf = max(0.0, min(1.0, conf))
            reason = str(obj.get("reason", ""))[:300]
            return {"decision": decision, "size_mult": round(size, 2),
                    "confidence": round(conf, 2), "reason": reason}
        except Exception:
            return None

    def _journal(self, pair: str, ctx: dict, decision, source: str,
                 latency_ms: int = 0, raw: str = None):
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pair": pair,
            "price": ctx.get("price"),
            "source": source,           # agent | no_cli | cooldown | call_cap | cli_error | bad_output
            "latency_ms": latency_ms,
            "ctx": {k: v for k, v in ctx.items() if k != "open_positions"},
        }
        if decision is not None:
            rec.update(decision)
        if raw:
            rec["raw"] = raw
        try:
            path = os.path.join(self._data_dir, DECISIONS_FILE)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except Exception as exc:
            logger.debug("TradeAgent: journal write failed: %s", exc)

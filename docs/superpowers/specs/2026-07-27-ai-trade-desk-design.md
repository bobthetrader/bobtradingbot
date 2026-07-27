# AI Trade Desk — design (2026-07-27)

## Goal
A real-time AI agent makes the final call on every buy candidate and self-tunes
nightly from trade outcomes — replacing "set parameters only" decision making.
Absolute driver: profit. Hard constraint: **no metered API billing** — all AI
calls run through the Claude Code CLI (`claude -p`) authenticated with the
user's Claude Max subscription (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`),
so cost is capped at the existing subscription and the worst failure mode is a
rate limit, never a bill.

## Decisions (agreed with Rob)
- **Authority:** deterministic gates (RSI floor, trading hours, pair excludes,
  smart-money/panel vetoes) pre-filter; the agent decides only on survivors.
- **Learning:** nightly self-tuning only (no per-trade parameter churn).
- **Model:** Sonnet (`--model sonnet` via CLI = Claude Sonnet 5 on Max plan).
- **Scope v1:** agent returns buy/skip + size multiplier + reason. Exit
  geometry (SL/TP per trade) stays rule-managed — v2 once v1 proves out.

## Components

### 1. `core/trade_agent.py` — decision agent
- `TradeAgent.decide(ctx) -> dict | None` called from the buy path AFTER all
  existing gates pass, BEFORE order execution.
- Input ctx: pair, strategy, entry score, 1h RSI, smart-money action/scores,
  AI-panel score, hour UTC, open positions + exposure, recent performance
  stats, current playbook text.
- CLI call: `claude -p <prompt> --output-format json --model sonnet
  --max-turns 1` with a 25s timeout. Response text must be pure JSON:
  `{"decision":"buy"|"skip","size_mult":0.5-1.3,"confidence":0-1,"reason":str}`.
- Hard rails in code: size_mult clamped to [0.5, 1.3]; malformed output,
  timeout, non-zero exit, or daily call cap (default 40) → return None →
  caller falls back to existing rule behaviour (trade proceeds at normal size)
  and the fallback is journaled.
- Every decision (buy/skip/fallback) appended to `data/agent_decisions.jsonl`
  with the full input snapshot, price at decision time, and reason.
- Config `[trade_agent] enabled` (default false; true in paper config),
  `max_calls_per_day`, `timeout_seconds`.

### 2. `core/agent_tuner.py` — nightly self-tuning
- Runs once per UTC day from the main loop (same pattern as the daily report).
- Input: last 14 days of closed trades joined to agent decisions, bucket
  stats (WR/net by strategy, RSI band, hour, pair, smart action), and skip
  counterfactuals (price now vs price at skip).
- One CLI call (same auth). Output JSON:
  `{"playbook": str<=1200 chars, "knobs": {"min_confidence": 0.3-0.8,
  "default_size_mult": 0.6-1.2}, "reasoning": str}`.
- Knobs are clamped to hard bounds in code; result written to
  `data/agent_policy.json`; every run appended to `data/agent_tuner_log.jsonl`.
- The playbook text is injected into every next-day decision prompt — this is
  how learning from each trade reaches real-time decisions.

### 3. Wiring
- `trading_bot.py`: agent consult inserted at the end of the buy-gate chain;
  size_mult applied to planned notional (inside existing €70/cash caps).
- Dashboard: "AI Trade Desk" card — last 5 decisions with reasons, calls
  today, last tune time + playbook summary.
- `Dockerfile`: install Node 20 + `@anthropic-ai/claude-code`; server `.env`
  gets `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token` on Rob's PC).
- Kill switch: `[trade_agent] enabled=false` reverts to pure rules instantly.

## Failure modes
| Failure | Behaviour |
|---|---|
| CLI missing / not authed | decide() returns None once, logs warning, agent auto-disables for 1h |
| Timeout / rate limit | fallback to rules, journaled as `fallback` |
| Malformed JSON | fallback to rules, raw output logged |
| Call cap reached | fallback to rules for the rest of the UTC day |

## Success criteria
Paper only until ≥50 agent-decided closed trades; compare net P&L and WR of
agent-decided vs fallback/rule trades in the journal before any live use.

# Journal Analysis Pipeline (replaces scalper daily analysis) — Design

**Date:** 2026-07-15
**Scope:** Kraken main bot analytics only. No trading-logic changes.

## Background

The daily 09:35 pipeline still analyses `scalper_trades.jsonl` for a strategy
paused since 2026-07-02 (probe verdict: no edge). Meanwhile the main bot's
long/short journal (`trade_events_paper.jsonl`) has carried full entry
feature-vectors since 2026-07-04 and is what the ~2026-07-20 smart-money /
event-shorts review needs. Switch the pipeline's subject; keep the cadence.

## Component 1 — `backtest/journal_analysis.py` (new)

Input: `backtest/data/trade_events_paper.jsonl` (BUY rows carry
`extra.features`; SELL rows carry `pnl_eur` (net), `reason`,
`extra.{entry_price,pnl_pct,entry_ts,hold_minutes,exit_hour_utc}`;
SHORT_OPEN/SHORT_CLOSE analogous with `short_type`).

Method: pair BUY→SELL per pair FIFO (SELL `entry_ts` matches BUY when
present); orphan SELLs (pre-feature era) analysed in outcome-only buckets.

Output — console summary + `backtest/journal_report.html` (standalone file,
NOT committed, NOT pushed anywhere):

- **Overview**: total closed longs/shorts, net WR, net P&L, by ISO week.
- **Long buckets** (each: n, net WR, total net €, avg net %):
  `smart_action` (boost/neutral/none-recorded), `hl_bias` bands
  (≤−2.5 / −2.5..−1 / −1..+1 / +1..+2.5 / ≥+2.5), `whale_score` same bands,
  `rsi_1h` bands (<40 / 40-50 / 50-60 / ≥60), entry `hour_utc` in 4h bands,
  pair, `strategy` profile, exit reason.
- **Short section**: same buckets where data exists; EVENT shorts broken out
  by `short_type == "EVENT"` with their journalled gate stack.
- Min-sample guards: buckets n<5 hidden; 5≤n<20 marked "low confidence".
- Pure stdlib (no pandas dependency) so it runs anywhere `py -3` does.

## Component 2 — pipeline rewiring

**Server (manual one-time, exact commands provided in the plan):**
- `/home/botuser/bot_auto.sh` gains a `journal)` case streaming the extracted
  `trade_events_paper.jsonl` (same pattern as the existing `extract)` case).
- The 09:30 cron additionally extracts `trade_events_paper.jsonl` from the
  Docker volume to `/home/botuser/backup/`.

**Local `scripts/daily_backtest.ps1`:**
- Step 1 pulls via ssh command `journal` → `backtest/data/trade_events_paper.jsonl`.
- Step 2 runs `py backtest/journal_analysis.py`.
- Step 3 (recommendations push-back + server `pull` trigger) REMOVED — the
  scalper-AI feedback loop is retired; this pipeline is now report-only.
- Scalper pull/backtest steps removed. Log format unchanged.

## Retirement (kept dormant, not deleted)

- `backtest/scalper_backtest.py`, `scalper_ai` recs loop, and the server
  09:45 recs-copy cron remain in place but unused (recs file simply stops
  updating). The scalper probe analyzer (`analyze_observations.py`) is
  untouched — still needed for the vwap_dev re-check.

## Error handling

- Missing/empty journal file → analyzer exits 0 with a clear "no data"
  message (the scheduled task must not accumulate failure states).
- Malformed journal lines skipped (count reported).
- ps1: ssh failure logs and exits 1 (same as today).

## Testing

- Analyzer unit-tested against a synthetic journal fixture (known trades →
  known bucket numbers), plain-assert style.
- Live run against the real pulled journal before switching the schedule.

## Out of scope

- Any trading-logic change; ByBitBot; weekly cadence (stays daily 09:35).

# Event-Driven Shorts — Design

**Date:** 2026-07-08
**Scope:** Kraken main bot (paper). ByBitBot untouched (long-only by design).

## Background / constraints

Technical shorts were disabled 2026-07-02 as a persistent net loser (83 trades,
26% WR, −€3.59; 7.7% WR even after the trend_bearish gate). They stay disabled.
This feature is a NEW, far stricter entry path re-using the existing short
mechanics (open/close, hard-stop sweep) for rare, news-driven risk-off moves —
"USA bombs Iran" nights. The event detector is the existing AI panel (its
context includes live news headlines each 30-min refresh; it scored −1.5..−2.95
throughout both July crash nights). No new APIs, no new cost.

## Entry gate — `_event_short_gate(pair)` in trading_bot.py

ALL conditions must hold simultaneously (any missing datum = gate closed):

1. `intelligence_score <= event_panel_max` (default **−2.0**) — the "event":
   news-driven, strongly bearish panel (stricter than the −1.5 buy veto).
2. `hl_bias[pair] <= event_hl_max` (default **−2.5**) — top Hyperliquid
   traders are net short THIS coin (read from `core.smart_money.last_for` /
   the cached scores; cache-only, never fetches).
3. `whale_score <= event_whale_max` (default **−1.5**) — flows bearish.
4. Existing 1h `trend_bearish` check is True (EMA9 < EMA21 confirmed).
5. Open EVENT shorts < `event_max_concurrent` (default **2**).
6. No open long on the pair; pair is in the core liquid set (`trade_pairs`).

Signals 1–3 are read from in-memory caches (panel score, smart-money layer) —
the gate adds zero network calls and zero loop latency.

## Execution & exits

- Entry: `execute_open_short_order(pair, price, short_type="EVENT")`, notional
  capped by existing `max_short_notional_eur` (30.0).
- Exits (all existing mechanics, already decoupled from the entry gate since
  2026-07-02): short TP 1.4% (`short_take_profit_percent`), soft SL 0.5%,
  hard-stop sweep at 1.5% (`short_hard_stop_percent`) every loop.
- NEW: EVENT shorts get a time-stop — force-close after
  `event_time_stop_hours` (default **12**) regardless of P&L. Event moves are
  fast; a stale event short is a thesis failure. Implemented in the existing
  short-management sweep using the position's `entry_ts` + `short_type`.

## Config (`config.paper.toml [shorting]`)

```toml
enabled = false               # OLD technical shorts stay dead — unchanged
event_shorts_enabled = true   # NEW gate only
event_panel_max = -2.0
event_hl_max = -2.5
event_whale_max = -1.5
event_max_concurrent = 2
event_time_stop_hours = 12
```

`event_shorts_enabled = false` (or absent config) disables the entire path.
The gate logs one line when it fires and one (debug) when close-but-blocked
(panel qualifies but another condition fails) for tuning.

## Journal & evaluation

- Open rows: `short_type: "EVENT"` plus `extra.features` carrying
  {intelligence_score, hl_bias, whale_score, hour_utc} at entry.
- Close rows: existing reasons (SHORT_TAKE_PROFIT / SHORT_HARD_STOP /
  SHORT_CLOSE) + new `EVENT_SHORT_TIME_STOP`.
- Review with the smart-money buckets (~2026-07-20): EVENT shorts must be
  net-positive or the experiment ends (set `event_shorts_enabled = false`).
- Expected frequency: ~2-3 fires in a bad week; ~0 in a calm week. Max
  exposure 2 × €30 = €60.

## Error handling

- Missing/None panel score, hl_bias, or whale_score → condition fails →
  no short (mirror of the long-side missing-data-is-neutral rule).
- The gate must never raise into the loop (guarded like the smart-money gate).

## Testing

- Pure gate-decision function unit-tested (threshold boundaries, missing-data
  closure, concurrency cap) in `tests/test_smart_money.py` style.
- Wiring verified via py_compile + harness with forced scores.
- Live verification post-deploy: `EVENT SHORT` log lines only during genuine
  risk-off; journal rows carry the feature stack.

## Out of scope

- Re-enabling technical shorts (`enabled` stays false).
- Dedicated news/event pipeline (panel is the detector).
- Risk-off long-closing on the same stack (possible future flag).
- ByBitBot.

# Smart-Money Neutral Squeeze — Design

**Date:** 2026-07-21
**Status:** Approved (user, this date). Re-assess ~2026-08-04 from the daily journal analysis.

## Problem

Journal evidence through 2026-07-21 (179 closed longs): entries with a
smart-money **boost** win 66.7% (n=18, net −2.14 EUR); **neutral** entries win
44.6% (n=56, net −21.83 EUR) and are the main bleeder alongside mean-reversion
(n=82, 40.2% WR, −31.16 EUR). The boost signal separates winners from losers,
but neutral entries dominate the book.

## Decision

Shift entry weight toward boosted trades by **squeezing neutral (un-boosted)
long entries** — higher entry bar, smaller size. Boost treatment is unchanged
(×1.3 size, −2.0 bar): the evidence does not yet support amplifying boost
(its net is still slightly negative). Applies to **all long entries** through
the buy gate (mean-reversion and trend — trend is also net-negative, so
nothing profitable is squeezed). New-listing and FORCE_BUY paths are
unaffected (they bypass the gate and default to ×1.0).

## Mechanics

1. `core/smart_money.py::decide()` — the neutral branch returns two new
   config values instead of hardcoded `(1.0, 0.0)`:
   - `neutral_size_mult` (default **0.75**; ≈ €50 → €37)
   - `neutral_min_score_delta` (default **+1.5**; effective bar gap between a
     boosted and a neutral entry becomes 3.5 points)
   `enabled = false` and the internal error path still return `(1.0, 0.0)`.
2. `trading_bot.py` buy gate — applies `size_mult` / `min_score_delta` from
   the smart-money result for any non-veto action (today: boost only), with a
   log line when the squeeze bites and `smart_adj` added to the skip log.
   The consume-once `_smart_boost` dict carries the multiplier as today.
3. Config: new keys documented in `[smart_money]` of `config.paper.toml`.
   `config.toml` (live) has no `[smart_money]` section and inherits the code
   defaults — intended.

## Missing-data behavior (deliberate change)

Data outages surface as "neutral", so during an outage all gate entries are
squeezed: the bot trades smaller and pickier when flying blind. Previously an
outage meant no effect. Conservative by design; accepted by user.

## Testing

Extend `tests/test_smart_money.py::test_decide`: neutral returns the squeeze
values (defaults and explicit config), `neutral_size_mult=1.0` +
`neutral_min_score_delta=0.0` restores old behavior, disabled/veto/boost
branches unchanged.

## Evaluation (~2026-08-04)

The journal features (`smart_action`) already feed the daily analysis, so no
instrumentation changes. Success criteria:
- neutral bucket's share of new entries and its net loss rate shrink;
- overall long net stops deteriorating (was −10.23 → −22.78 EUR over 07-16→07-21);
- if boost net is not positive by ~n=30, the next lever is boost signal
  quality (thresholds/sources), not more weight.

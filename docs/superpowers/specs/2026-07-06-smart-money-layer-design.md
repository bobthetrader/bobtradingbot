# Smart-Money Layer — Design

**Date:** 2026-07-06
**Scope:** Kraken main bot only (ByBitBot stays lean by design)
**Mode:** "Full lean" — the signal vetoes AND boosts from day one; every threshold config-tunable, journaled for evidence-based re-tuning after ~2 weeks.

## Goal

Bias the bot's entries toward what informed capital is doing: block buys when big
money is exiting or top traders are short; enter bigger/easier when they are
accumulating or long. Two independent components feed one `[smart_money]` action
layer in the buy gate.

## Component 1 — Whale exchange flows (`core/whale_flows.py`, new)

Market-level signal: *is big money entering or fleeing crypto right now?*

**Fresh part (hourly-grade):** Alchemy `alchemy_getAssetTransfers` (existing
`ALCHEMY_API_KEY`, JSON-RPC) against a curated, config-listed set of ~8-12 known
exchange hot wallets (Binance, Coinbase, Kraken, OKX, Bybit). Two calls per
wallet per refresh (transfers TO and FROM, categories external+erc20 combined),
window = last 60 minutes, counted only when ≥ `min_whale_eth` (default 250 ETH
≈ €390k) or stablecoin equivalent:

- ETH → exchange: sell pressure (bearish)
- ETH ← exchange: accumulation (bullish)
- USDT/USDC → exchange: dry powder arriving (bullish, weight 0.5)

**Slow part (context):** existing CoinMetrics daily BTC+ETH exchange flows from
`core/onchain_data.py` — including fixing the current bug where `eth_flows` is
fetched but never enters the combined score.

**Output:** `whale_score` ∈ [−5, +5] = 0.6 × fresh + 0.4 × daily. Cache TTL
1800s. Any component failing drops out of the blend; nothing available → 0.0
(neutral).

## Component 2 — Hyperliquid smart-trader positioning (`core/hyperliquid_smart.py`, new)

Per-coin signal: *are PROVEN traders net long or short this specific coin?*
All data public and keyless (Hyperliquid is fully on-chain).

**Roster selection (daily, cached to `data/hl_roster.json`):**
1. Fetch the Hyperliquid leaderboard
   (`https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` — verified live
   2026-07-06: ~33MB JSON, 40,204 rows of `{ethAddress, accountValue,
   windowPerformances[day|week|month|allTime]{pnl, roi, vlm}}`; fetch daily,
   stream-parse, keep only the roster).
2. Filter against lottery winners: positive PnL on BOTH 30-day AND all-time
   windows, volume ≥ configurable floor, then top `roster_size` (default 20)
   by 30-day PnL.
3. If the leaderboard endpoint is unavailable, keep the last cached roster; if
   none, component is neutral.

**Position polling (every 30 min):** `POST https://api.hyperliquid.xyz/info`
`{"type":"clearinghouseState","user":<wallet>}` per roster wallet → open
positions (coin, signed size, notional). ~40 requests/hour, public rate limits.

**Per-coin bias:** for each coin, weight each positioned trader by normalized
30-day PnL (capped so no single wallet dominates), sum signed weights, scale to
`hl_bias[coin]` ∈ [−5, +5]. Require ≥ `min_traders` (default 3) positioned in
the coin, else 0.0 (neutral). HL coin symbols map to bot pairs (BTC→XBTEUR /
XXBTZEUR, ETH→ETHEUR / XETHZEUR, SOL/XRP/ADA/DOT/LINK + any dynamic pair whose
base trades on HL; unmapped pairs are neutral).

## Action wiring (`trading_bot.py` buy gate + sizing)

New `[smart_money]` config block (all knobs; 0/false disables that piece):

| Knob | Default | Effect |
|---|---|---|
| `enabled` | true | master switch |
| `whale_veto_score` | −2.5 | whale_score ≤ this → block ALL buys |
| `whale_boost_score` | +2.5 | whale_score ≥ this → boost eligible |
| `hl_veto_score` | −2.5 | hl_bias[pair] ≤ this → block buys on THAT pair |
| `hl_boost_score` | +2.5 | hl_bias[pair] ≥ this → boost eligible |
| `boost_size_mult` | 1.3 | position size ×1.3, still inside existing caps (base×2 = €70, 95% cash) |
| `boost_min_score_delta` | −2.0 | effective_min lowered by 2 pts while boosted |

Rules:
- Either veto blocks (checked in `_execute_buy_gate` after the AI-panel veto and
  RSI ceiling, with a specific log line each: `BUY skipped: whale flows bearish
  (…)` / `BUY skipped: HL smart traders net short PAIR (…)`).
- Boosts do NOT stack: one boost applies if either component clears its boost
  threshold (`BUY boosted: smart money long (…)`).
- Size boost is applied in `_get_dynamic_trade_amount_eur` AFTER the
  `min_target_trade_eur` floor, INSIDE the final `min(base*2, amount,
  available*0.95)` cap.
- Missing/failed data is ALWAYS neutral — the layer can never block trading
  because an API is down.

## Journaling & evaluation

Every BUY's `extra.features` gains: `whale_score`, `hl_bias`, `hl_n_long`,
`hl_n_short`, `smart_action` ("veto" never appears on executed rows; "boost" |
"neutral"). Success criterion after ~2 weeks: bucket closed trades by signal
range (would-veto / neutral / boosted) and compare net WR + net P&L. If the
boosted bucket does not outperform neutral, pull `boost_*` back (veto-only). If
vetoed-range signals would have been profitable, widen thresholds.

`bot_status.json` gains a `smart_money` object (scores per pair) for dashboard
visibility.

## Error handling

- All fetches: try/except → debug log → neutral. TTL caches (1800s positions /
  24h roster) with stale-on-error fallback for the roster.
- Score maths guarded so a malformed API response can never throw inside the
  buy gate (wrapper returns cached-or-neutral).

## Testing

1. Unit harness (offline): synthetic transfers / synthetic clearinghouse
   responses → assert score maths, veto/boost decisions at thresholds, and
   neutral-on-missing behaviour.
2. Live smoke: Hyperliquid leaderboard + one clearinghouseState call (keyless,
   works from dev PC); Alchemy transfers if local `.env` has the key.
3. Gate wiring harness (same style as the 2026-07-06 trigger fixes): replay
   entries with forced scores, assert block/boost/log outcomes.
4. `py_compile` all touched files; deploy via standard server rebuild.

## Out of scope

- Trading ON Hyperliquid (perps, UK-restricted) — read-only signal use.
- Mirroring individual trades / trade-copying.
- ByBitBot changes.
- Paid sources (Nansen, Whale Alert, CoinGlass API).

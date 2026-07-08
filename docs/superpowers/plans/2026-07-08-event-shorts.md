# Event-Driven Shorts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rare, conviction-stacked shorts that fire only when the AI panel (news), Hyperliquid top-trader bias, whale flows, and the 1h trend all say "down" at once — reusing the existing (disabled) short mechanics.

**Architecture:** A pure decision function `event_short_ok(...)` in `core/smart_money.py` (unit-tested); a per-loop `_check_event_short_entries()` phase in `trading_bot.py` that feeds it cached in-memory signals and calls `execute_open_short_order(pair, price, short_type="EVENT")`; an unconditional 12h time-stop for EVENT shorts in the existing exit sweep. Old technical shorts stay disabled (`[shorting] enabled = false` unchanged).

**Tech Stack:** Python 3 stdlib only. No new network calls — every gate input is already cached in memory (panel score, smart-money layer, EMA state).

## Global Constraints

- `[shorting] enabled` stays **false**; the event path is gated ONLY by new `event_shorts_enabled`.
- Any missing gate datum (None) = gate closed — no short on missing data.
- The gate adds zero network calls and must never raise into the loop.
- Caps: `event_max_concurrent = 2` EVENT shorts, notional = `max_short_notional_eur` (30.0 existing).
- EVENT shorts force-close after `event_time_stop_hours = 12` regardless of P&L (`EVENT_SHORT_TIME_STOP`).
- Journal SHORT_OPEN rows carry `short_type: "EVENT"` + `features` {intelligence_score, hl_bias, whale_score, hour_utc}.
- Tests are plain-assert scripts: `py -3 tests/test_smart_money.py`. LF endings, no BOM.
- Commits end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Pure gate decision `event_short_ok` + tests

**Files:**
- Modify: `core/smart_money.py` (append function at end of file)
- Test: `tests/test_smart_money.py` (append test + register in main block)

**Interfaces:**
- Consumes: nothing new (pure function).
- Produces: `smart_money.event_short_ok(panel, hl_bias, whale, ema_bullish, n_open_event, cfg) -> tuple[bool, str]` — `(True, "reason string for the log")` when ALL conditions pass, `(False, "<which condition failed>")` otherwise. `cfg` is the `[shorting]` config dict; defaults: `event_panel_max=-2.0`, `event_hl_max=-2.5`, `event_whale_max=-1.5`, `event_max_concurrent=2`, `event_shorts_enabled=False`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_smart_money.py` before the `if __name__` block:

```python
def test_event_short_gate():
    from core.smart_money import event_short_ok

    cfg = {"event_shorts_enabled": True, "event_panel_max": -2.0,
           "event_hl_max": -2.5, "event_whale_max": -1.5,
           "event_max_concurrent": 2}

    # All four conditions met -> fire
    ok, why = event_short_ok(panel=-2.5, hl_bias=-3.0, whale=-2.0,
                             ema_bullish=False, n_open_event=0, cfg=cfg)
    assert ok, why
    # Each condition individually failing -> closed
    assert not event_short_ok(-1.9, -3.0, -2.0, False, 0, cfg)[0]  # panel too mild
    assert not event_short_ok(-2.5, -2.4, -2.0, False, 0, cfg)[0]  # HL not short enough
    assert not event_short_ok(-2.5, -3.0, -1.4, False, 0, cfg)[0]  # whale too mild
    assert not event_short_ok(-2.5, -3.0, -2.0, True, 0, cfg)[0]   # trend bullish
    # Missing data (None) -> closed, every slot
    assert not event_short_ok(None, -3.0, -2.0, False, 0, cfg)[0]
    assert not event_short_ok(-2.5, None, -2.0, False, 0, cfg)[0]
    assert not event_short_ok(-2.5, -3.0, None, False, 0, cfg)[0]
    assert not event_short_ok(-2.5, -3.0, -2.0, None, 0, cfg)[0]   # EMA unknown
    # Concurrency cap
    assert not event_short_ok(-2.5, -3.0, -2.0, False, 2, cfg)[0]
    # Feature disabled -> closed even on perfect signals
    assert not event_short_ok(-5.0, -5.0, -5.0, False, 0,
                              {**cfg, "event_shorts_enabled": False})[0]
    # Absent config -> disabled by default
    assert not event_short_ok(-5.0, -5.0, -5.0, False, 0, {})[0]
    print("test_event_short_gate OK")
```

Register in the main block (add `test_event_short_gate()` before `print("ALL OK")`).

- [ ] **Step 2: Run to verify it fails**

Run: `py -3 tests/test_smart_money.py`
Expected: `ImportError: cannot import name 'event_short_ok'`

- [ ] **Step 3: Implement** — append to `core/smart_money.py`:

```python
# ── event-driven shorts gate (pure, unit-tested) ─────────────────────────────

def event_short_ok(panel, hl_bias, whale, ema_bullish, n_open_event, cfg
                   ) -> Tuple[bool, str]:
    """Conviction-stacked short gate: ALL of news-panel, per-coin HL trader
    bias, whale flows and confirmed bearish 1h trend must align. Any missing
    datum (None) closes the gate — no short on missing data. Old technical
    shorts ([shorting] enabled) are a separate, still-disabled path.
    """
    c = cfg or {}
    if not c.get("event_shorts_enabled", False):
        return False, "event shorts disabled"
    try:
        panel_max = float(c.get("event_panel_max", -2.0))
        hl_max = float(c.get("event_hl_max", -2.5))
        whale_max = float(c.get("event_whale_max", -1.5))
        max_conc = int(c.get("event_max_concurrent", 2))
    except Exception:
        return False, "bad event-short config"

    if n_open_event >= max_conc:
        return False, f"event-short cap reached ({n_open_event}/{max_conc})"
    if panel is None or panel > panel_max:
        return False, f"panel {panel} > {panel_max} (no risk-off event)"
    if hl_bias is None or hl_bias > hl_max:
        return False, f"HL bias {hl_bias} > {hl_max} (top traders not short)"
    if whale is None or whale > whale_max:
        return False, f"whale {whale} > {whale_max} (flows not bearish)"
    if ema_bullish is not False:   # True or None both close the gate
        return False, "1h trend not confirmed bearish"
    return True, (f"EVENT SHORT: panel {panel:+.2f} | HL {hl_bias:+.2f} | "
                  f"whale {whale:+.2f} | trend bearish")
```

(`Tuple` is already imported in this module's `typing` import; if not, extend it.)

- [ ] **Step 4: Run tests to verify pass**

Run: `py -3 tests/test_smart_money.py`
Expected: all previous tests + `test_event_short_gate OK` + `ALL OK`

- [ ] **Step 5: Commit**

```bash
git add core/smart_money.py tests/test_smart_money.py
git commit -m "feat: pure conviction-stacked gate for event-driven shorts

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Wire the event-short path into trading_bot.py + config

**Files:**
- Modify: `trading_bot.py` — `execute_open_short_order` (line ~5363), short-exit sweep (~2872-2892), exit dispatch (~4403), main loop (new phase call), `__init__` (`self._short_type = {}`)
- Modify: `config.paper.toml` — `[shorting]` block

**Interfaces:**
- Consumes: `smart_money.event_short_ok(...)` (Task 1 signature), `smart_money.last_for(pair)` (existing: dict with `whale_score`, `hl_bias`), `self._intelligence_score`, `self._ema_bullish` (dict pair->bool|None), existing short mechanics.
- Produces: journal `SHORT_OPEN` rows with `short_type: "EVENT"` + `features`; exit reason `EVENT_SHORT_TIME_STOP`; config keys per Global Constraints.

- [ ] **Step 1: Track short types.** In `__init__`, next to the other short state dicts (search `self.short_qty` initialisation), add:

```python
        self._short_type: dict = {}   # pair -> "EVENT" | "BEAR" | "HEDGE" (in-memory; lost on restart)
```

- [ ] **Step 2: Extend `execute_open_short_order` for the EVENT path.** Current signature (trading_bot.py:5363): `def execute_open_short_order(self, pair, price):`. Replace the signature and the guard block:

```python
    def execute_open_short_order(self, pair, price, short_type=None):
```

and replace:

```python
        try:
            if not self.enable_live_shorts:
                return
            if self.short_qty.get(pair, 0.0) > 0:
                return
```

with:

```python
        try:
            # EVENT shorts are gated by _check_event_short_entries (their own
            # config flag); everything else still requires enable_live_shorts.
            if short_type != "EVENT" and not self.enable_live_shorts:
                return
            if self.short_qty.get(pair, 0.0) > 0:
                return
```

Then replace the sizing block:

```python
            if self._btc_downtrend:
                short_type = "BEAR"
                notional = _nav * 0.05   # 5% of NAV â€” BTC regime confirms downtrend
            else:
                short_type = "HEDGE"
                notional = _nav * 0.03   # 3% of NAV â€” defensive hedge short
            notional = min(self.max_short_notional_eur, max(notional, self._get_trade_amount_eur() * 0.3))
```

with:

```python
            if short_type == "EVENT":
                # Fixed small size for event shorts — cap only, no NAV scaling
                notional = float(self.max_short_notional_eur)
            elif self._btc_downtrend:
                short_type = "BEAR"
                notional = _nav * 0.05   # 5% of NAV — BTC regime confirms downtrend
            else:
                short_type = "HEDGE"
                notional = _nav * 0.03   # 3% of NAV — defensive hedge short
            notional = min(self.max_short_notional_eur, max(notional, self._get_trade_amount_eur() * 0.3))
```

After the successful-fill block's `self.entry_timestamps[pair] = int(now_ts)` add:

```python
                self._short_type[pair] = short_type
```

And extend the `_finalise_trade('SHORT_OPEN', ...)` call's extra to carry the gate features:

```python
                _evt_features = {}
                if short_type == "EVENT":
                    try:
                        from core import smart_money as _smart
                        _smf = _smart.last_for(pair)
                        _evt_features = {
                            "intelligence_score": round(float(self._intelligence_score), 2),
                            "hl_bias": _smf.get("hl_bias"),
                            "whale_score": _smf.get("whale_score"),
                            "hour_utc": datetime.utcnow().hour,
                        }
                    except Exception:
                        _evt_features = {}
                self._finalise_trade('SHORT_OPEN', pair, volume, price, 0.0,
                                     'SHORT_OPEN_EXECUTED',
                                     extra={'notional': notional, 'short_type': short_type,
                                            'features': _evt_features})
```

(This replaces the existing `_finalise_trade('SHORT_OPEN', ...)` call at ~5414-5416.)

- [ ] **Step 3: Add the entry-scan phase.** New method next to `_enforce_short_hard_stops` (search for `def _enforce_short_hard_stops`):

```python
    def _check_event_short_entries(self):
        """Conviction-stacked event shorts: panel + HL bias + whale flows +
        bearish 1h trend must ALL align (see core.smart_money.event_short_ok).
        All inputs are in-memory caches — zero network calls, never raises."""
        try:
            _scfg = self.config.get('shorting', {})
            if not _scfg.get('event_shorts_enabled', False):
                return
            from core import smart_money as _smart
            n_open_event = sum(1 for p, t in self._short_type.items()
                               if t == "EVENT" and self.short_qty.get(p, 0) > 0)
            for pair in list(self._core_trade_pairs):
                if self.short_qty.get(pair, 0) > 0:
                    continue
                if (self.position_qty.get(pair, 0) or self.holdings.get(pair, 0)) > 0:
                    continue   # never short against an open long
                price = self.pair_prices.get(pair, 0)
                if price <= 0:
                    continue
                _smf = _smart.last_for(pair)
                ok, why = _smart.event_short_ok(
                    panel=getattr(self, '_intelligence_score', None),
                    hl_bias=_smf.get('hl_bias'),
                    whale=_smf.get('whale_score'),
                    ema_bullish=self._ema_bullish.get(pair),
                    n_open_event=n_open_event,
                    cfg=_scfg,
                )
                if ok:
                    self.logger.warning("%s -> opening EVENT short on %s @ %.4f",
                                        why, pair, price)
                    self.execute_open_short_order(pair, price, short_type="EVENT")
                    n_open_event += 1
        except Exception as exc:
            self.logger.debug("event-short scan failed: %s", exc)
```

Call it once per loop. The anchor is at trading_bot.py:4395-4399:

```python
                    # ── Phase 5: TP/SL exits and partial exits ─────────────
                    # Backstop sweep first: cap any runaway shorts the single-exit
                    # check below can only close one-per-loop.
                    self._enforce_short_hard_stops()
```

Insert immediately after the `self._enforce_short_hard_stops()` line:

```python
                    self._check_event_short_entries()
```

- [ ] **Step 4: Unconditional 12h time-stop for EVENT shorts.** In the short-exit section of `check_take_profit_or_stop_loss` (trading_bot.py ~2882, the block starting `# Time review: after 12h close if net P&L`), insert BEFORE the existing time-review:

```python
                # EVENT shorts: unconditional time-stop — an event thesis that
                # hasn't paid within N hours is dead; close regardless of P&L.
                if self._short_type.get(pair) == "EVENT":
                    _evt_hours = float(self.config.get('shorting', {}).get('event_time_stop_hours', 12))
                    open_ts_evt = self.entry_timestamps.get(pair) or 0
                    if open_ts_evt and (time.time() - open_ts_evt) / 3600 >= _evt_hours:
                        return pair, "EVENT_SHORT_TIME_STOP", short_change_percent
```

Then route the new reason to the short-close executor: at the dispatch (trading_bot.py ~4403) change:

```python
                        if risk_type in ("SHORT_TAKE_PROFIT", "SHORT_STOP_LOSS", "SHORT_TIME_REVIEW"):
```

to:

```python
                        if risk_type in ("SHORT_TAKE_PROFIT", "SHORT_STOP_LOSS", "SHORT_TIME_REVIEW", "EVENT_SHORT_TIME_STOP"):
```

Also clear the type on close: in `execute_close_short_order`, after `self.short_entry_prices[pair] = 0.0` add:

```python
                self._short_type.pop(pair, None)
```

Restart caveat (accepted): `_short_type` is in-memory, so an EVENT short open across a restart degrades to the existing SHORT_TIME_REVIEW behaviour — harmless, bounded by the hard-stop sweep either way.

- [ ] **Step 5: Config.** In `config.paper.toml` `[shorting]` section, after `enabled = false`, add:

```toml
# EVENT-driven shorts (2026-07-08): a separate, far stricter entry path that
# does NOT depend on `enabled` above. Fires only when news panel + Hyperliquid
# top-trader bias + whale flows + bearish 1h trend ALL align (see
# docs/superpowers/specs/2026-07-08-event-shorts-design.md). Max 2 x EUR30.
event_shorts_enabled = true
event_panel_max = -2.0        # AI panel must be <= this (news risk-off)
event_hl_max = -2.5           # HL top traders net short the pair
event_whale_max = -1.5        # whale flows bearish
event_max_concurrent = 2
event_time_stop_hours = 12    # force-close EVENT shorts after this, any P&L
```

- [ ] **Step 6: Verify**

Run: `py -3 -m py_compile trading_bot.py core/smart_money.py`
Expected: silent success.

Run: `py -3 tests/test_smart_money.py`
Expected: `ALL OK` (incl. `test_event_short_gate OK`)

Run: `py -3 -c "import tomllib; c=tomllib.load(open('config.paper.toml','rb'))['shorting']; print(c['enabled'], c['event_shorts_enabled'], c['event_panel_max'])"`
Expected: `False True -2.0`

Wiring harness (forced-signal walk of the pure gate exactly as the scan calls it):

```bash
py -3 -c "
import sys; sys.path.insert(0,'.')
from core.smart_money import event_short_ok
cfg = dict(event_shorts_enabled=True, event_panel_max=-2.0, event_hl_max=-2.5,
           event_whale_max=-1.5, event_max_concurrent=2)
assert event_short_ok(-2.6, -2.6, -1.6, False, 0, cfg)[0]
assert not event_short_ok(-2.6, -2.6, -1.6, False, 2, cfg)[0]
assert not event_short_ok(None, -2.6, -1.6, False, 0, cfg)[0]
print('event-short wiring harness OK')"
```

Expected: `event-short wiring harness OK`

- [ ] **Step 7: Commit + push**

```bash
git add trading_bot.py config.paper.toml
git commit -m "feat: event-driven shorts behind conviction-stacked gate

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

Deploy is the human's standard command. Post-deploy checks: no `EVENT SHORT` lines during calm markets; on a genuine risk-off night expect at most 2 opens with the full signal stack logged; journal rows carry `short_type: "EVENT"` + features.

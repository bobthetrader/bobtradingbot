"""Smart-money layer unit tests — pure scoring maths, no network.
Run: py -3 tests/test_smart_money.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_whale_fresh_score():
    from core.whale_flows import score_fresh_flows

    # Strong outflow (accumulation) -> strongly bullish
    s = score_fresh_flows(eth_in=500, eth_out=2000, stable_in_usd=0, min_activity_eth=500)
    assert s >= 3.0, s
    # Strong inflow (distribution) -> strongly bearish
    s = score_fresh_flows(eth_in=2000, eth_out=400, stable_in_usd=0, min_activity_eth=500)
    assert s <= -3.0, s
    # Balanced flows -> neutral-ish
    s = score_fresh_flows(eth_in=1000, eth_out=1050, stable_in_usd=0, min_activity_eth=500)
    assert -1.0 <= s <= 1.0, s
    # Too little whale activity to judge -> exactly neutral
    s = score_fresh_flows(eth_in=100, eth_out=150, stable_in_usd=0, min_activity_eth=500)
    assert s == 0.0, s
    # Stablecoin inflow adds a bullish bonus on top of neutral flows
    s0 = score_fresh_flows(eth_in=1000, eth_out=1000, stable_in_usd=0, min_activity_eth=500)
    s1 = score_fresh_flows(eth_in=1000, eth_out=1000, stable_in_usd=3_000_000, min_activity_eth=500)
    assert s1 > s0, (s0, s1)
    # Clamped to [-5, +5]
    s = score_fresh_flows(eth_in=1, eth_out=100000, stable_in_usd=50_000_000, min_activity_eth=500)
    assert s <= 5.0, s
    print("test_whale_fresh_score OK")


def test_whale_blend():
    from core.whale_flows import blend_scores

    # fresh 0.6 / daily 0.4 weighting
    assert abs(blend_scores(fresh=5.0, daily=0.0) - 3.0) < 1e-9
    assert abs(blend_scores(fresh=0.0, daily=-5.0) - (-2.0)) < 1e-9
    # FIXED weights: a missing component contributes 0 — the survivor keeps
    # its own weight and can NEVER reach the +/-2.5 veto/boost band alone
    # when it's the 0.4-weighted daily (regression: 2026-07-07 blackout).
    assert abs(blend_scores(fresh=None, daily=-5.0) - (-2.0)) < 1e-9
    assert abs(blend_scores(fresh=4.0, daily=None) - 2.4) < 1e-9
    assert blend_scores(fresh=None, daily=None) == 0.0
    # daily alone at its most bearish stays above the -2.5 veto line
    assert blend_scores(fresh=None, daily=-5.0) > -2.5
    print("test_whale_blend OK")


def _lb_row(addr, month_pnl, alltime_pnl, month_vlm):
    return {"ethAddress": addr, "accountValue": "1000000",
            "windowPerformances": [
                ["day", {"pnl": "0", "roi": "0", "vlm": "0"}],
                ["week", {"pnl": "0", "roi": "0", "vlm": "0"}],
                ["month", {"pnl": str(month_pnl), "roi": "0.1", "vlm": str(month_vlm)}],
                ["allTime", {"pnl": str(alltime_pnl), "roi": "0.2", "vlm": str(month_vlm)}],
            ]}


def test_hl_roster_filter():
    from core.hyperliquid_smart import select_roster

    rows = [
        _lb_row("0xA", 900_000, 2_000_000, 200e6),   # good
        _lb_row("0xB", -50_000, 5_000_000, 500e6),   # negative month -> OUT
        _lb_row("0xC", 800_000, -100_000, 300e6),    # negative all-time -> OUT (lottery)
        _lb_row("0xD", 700_000, 1_000_000, 1e6),     # volume too small -> OUT
        _lb_row("0xE", 1_200_000, 3_000_000, 400e6), # good, higher month pnl
        _lb_row(None, 950_000, 2_500_000, 250e6),    # otherwise qualifies, no address -> OUT
    ]
    roster = select_roster(rows, size=20, min_volume_usd=50e6)
    addrs = [r["address"] for r in roster]
    assert addrs == ["0xE", "0xA"], addrs           # sorted by month pnl desc
    assert None not in addrs, addrs                 # missing ethAddress must be skipped
    assert all(r["weight"] > 0 for r in roster)
    print("test_hl_roster_filter OK")


def test_hl_coin_bias():
    from core.hyperliquid_smart import score_coin_bias

    # 3 quality-weighted longs, 1 short -> clearly positive
    pos = [
        {"weight": 1.0, "side": 1}, {"weight": 2.0, "side": 1},
        {"weight": 1.0, "side": 1}, {"weight": 1.0, "side": -1},
    ]
    r = score_coin_bias(pos, min_traders=3)
    assert r["bias"] > 2.0, r
    assert r["n_long"] == 3 and r["n_short"] == 1, r
    # All short -> -5
    r = score_coin_bias([{"weight": 1.0, "side": -1}] * 4, min_traders=3)
    assert r["bias"] == -5.0, r
    # Not enough traders positioned -> neutral
    r = score_coin_bias([{"weight": 1.0, "side": 1}] * 2, min_traders=3)
    assert r["bias"] == 0.0, r
    print("test_hl_coin_bias OK")


def test_pair_mapping():
    from core.hyperliquid_smart import coin_for_pair
    assert coin_for_pair("XBTEUR") == "BTC"
    assert coin_for_pair("XXBTZEUR") == "BTC"
    assert coin_for_pair("XETHZEUR") == "ETH"
    assert coin_for_pair("SOLEUR") == "SOL"
    assert coin_for_pair("XXRPZEUR") == "XRP"
    assert coin_for_pair("NOSUCHEUR") is None
    print("test_pair_mapping OK")


def test_gate_path_never_fetches():
    """refresh=False (the buy-gate path) must return instantly from cache
    without any network call — even when caches are cold."""
    import core.whale_flows as wf
    import core.hyperliquid_smart as hl

    # Poison the network layer: any attempted fetch would blow up loudly
    def _boom(*a, **k):
        raise AssertionError("gate path attempted a network call")
    orig_rpc, orig_req = wf._alchemy_rpc, hl.requests.post
    wf._alchemy_rpc = _boom
    try:
        # cold whale cache -> 0.0, no fetch
        wf._cache["ts"] = 0.0
        wf._cache["score"] = 0.0
        assert wf.get_whale_score({}, refresh=False) == 0.0
        # warm-but-stale cache -> stale value served, still no fetch
        wf._cache["ts"] = 1.0          # ancient
        wf._cache["score"] = -1.75
        assert wf.get_whale_score({}, refresh=False) == -1.75
        # HL cold cache -> neutral, no fetch (refresh path never reached)
        hl._pos_cache["bias"] = {}
        hl._pos_cache["ts"] = 0.0
        r = hl.get_bias("BTC", {}, refresh=False)
        assert r == {"bias": 0.0, "n_long": 0, "n_short": 0}, r
    finally:
        wf._alchemy_rpc = orig_rpc
        wf._cache["ts"] = 0.0
        wf._cache["score"] = 0.0
    print("test_gate_path_never_fetches OK")


def test_decide():
    from core.smart_money import decide

    cfg = {"enabled": True, "whale_veto_score": -2.5, "whale_boost_score": 2.5,
           "hl_veto_score": -2.5, "hl_boost_score": 2.5,
           "boost_size_mult": 1.3, "boost_min_score_delta": -2.0}

    # whale veto
    a, reason, mult, delta = decide(-3.0, 0.0, cfg)
    assert a == "veto" and "whale" in reason.lower(), (a, reason)
    # HL per-pair veto
    a, reason, mult, delta = decide(0.0, -3.0, cfg)
    assert a == "veto" and "hyperliquid" in reason.lower() or "trader" in reason.lower(), (a, reason)
    # boost (either component) — no stacking, one boost
    a, reason, mult, delta = decide(3.0, 3.0, cfg)
    assert a == "boost" and mult == 1.3 and delta == -2.0, (a, mult, delta)
    # dead zone -> neutral, no effect
    a, reason, mult, delta = decide(1.0, -1.0, cfg)
    assert a == "neutral" and mult == 1.0 and delta == 0.0, (a, mult, delta)
    # veto wins over boost when components disagree
    a, _, _, _ = decide(-3.0, 4.0, cfg)
    assert a == "veto", a
    # disabled -> neutral always
    a, _, mult, _ = decide(-5.0, -5.0, {**cfg, "enabled": False})
    assert a == "neutral" and mult == 1.0, (a, mult)
    # whale boost disabled (whale_boost_score <= 0) -> boost reason must credit HL, not whale
    a, reason, mult, delta = decide(0.0, 3.0, {**cfg, "whale_boost_score": -1})
    assert a == "boost", (a, reason)
    assert reason.lower().startswith("hl") or "trader" in reason.lower().split("(")[0], reason
    assert "whale accumulation" not in reason.lower(), reason
    print("test_decide OK")


def test_evaluate_never_raises():
    import core.whale_flows as wf
    import core.hyperliquid_smart as hl
    from core.smart_money import evaluate

    # Stay OFFLINE: stub out the component fetchers.
    orig_whale, orig_bias = wf.get_whale_score, hl.get_bias
    wf.get_whale_score = lambda cfg=None: 0.0
    hl.get_bias = lambda coin, cfg=None: {"bias": 0.0, "n_long": 0, "n_short": 0}
    try:
        # Malformed config value would make decide()'s float() raise ValueError;
        # evaluate() must swallow it and return a neutral fallback with all keys.
        r = evaluate("XBTEUR", {"whale_veto_score": "oops"})
        assert r["action"] == "neutral", r
        assert r["size_mult"] == 1.0, r
        assert r["min_score_delta"] == 0.0, r
        assert r["whale_score"] == 0.0 and r["hl_bias"] == 0.0, r
        assert r["hl_n_long"] == 0 and r["hl_n_short"] == 0, r
        assert "reason" in r
        # Normal path still works after the failure.
        r2 = evaluate("XBTEUR", {})
        assert r2["action"] == "neutral", r2
    finally:
        wf.get_whale_score = orig_whale
        hl.get_bias = orig_bias
    print("test_evaluate_never_raises OK")


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


if __name__ == "__main__":
    test_whale_fresh_score()
    test_whale_blend()
    test_hl_roster_filter()
    test_hl_coin_bias()
    test_pair_mapping()
    test_decide()
    test_evaluate_never_raises()
    test_gate_path_never_fetches()
    test_event_short_gate()
    print("ALL OK")

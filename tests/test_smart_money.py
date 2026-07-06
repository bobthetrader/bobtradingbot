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
    # missing components (None) drop out
    assert blend_scores(fresh=None, daily=-5.0) == -5.0
    assert blend_scores(fresh=4.0, daily=None) == 4.0
    assert blend_scores(fresh=None, daily=None) == 0.0
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
    ]
    roster = select_roster(rows, size=20, min_volume_usd=50e6)
    addrs = [r["address"] for r in roster]
    assert addrs == ["0xE", "0xA"], addrs           # sorted by month pnl desc
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


if __name__ == "__main__":
    test_whale_fresh_score()
    test_whale_blend()
    test_hl_roster_filter()
    test_hl_coin_bias()
    test_pair_mapping()
    print("ALL OK")

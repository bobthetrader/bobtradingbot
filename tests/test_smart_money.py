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


if __name__ == "__main__":
    test_whale_fresh_score()
    test_whale_blend()
    print("ALL OK")

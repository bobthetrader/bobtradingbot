"""Journal analyzer tests against a synthetic fixture.
Run: py -3 tests/test_journal_analysis.py"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mk_fixture(path):
    rows = [
        # BUY with features -> SELL win (+1.0 net)
        {"ts": "2026-07-10T10:00:00", "type": "BUY", "pair": "ADAEUR", "price": 0.10,
         "reason": "BUY_EXECUTED",
         "extra": {"features": {"rsi_1h": 45.0, "hour_utc": 10, "smart_action": "boost",
                                 "hl_bias": 3.0, "whale_score": 1.0, "strategy": "mean_reversion"}}},
        {"ts": "2026-07-10T12:00:00", "type": "SELL", "pair": "ADAEUR", "price": 0.103,
         "pnl_eur": 1.0, "reason": "TAKE_PROFIT",
         "extra": {"entry_price": 0.10, "pnl_pct": 3.0, "hold_minutes": 120.0}},
        # BUY with features -> SELL loss (-0.5 net)
        {"ts": "2026-07-11T21:00:00", "type": "BUY", "pair": "XRPEUR", "price": 1.0,
         "reason": "BUY_EXECUTED",
         "extra": {"features": {"rsi_1h": 62.0, "hour_utc": 21, "smart_action": "neutral",
                                 "hl_bias": -2.0, "whale_score": -1.0, "strategy": "mean_reversion"}}},
        {"ts": "2026-07-11T23:00:00", "type": "SELL", "pair": "XRPEUR", "price": 0.99,
         "pnl_eur": -0.5, "reason": "STOP_LOSS",
         "extra": {"entry_price": 1.0, "pnl_pct": -1.0, "hold_minutes": 120.0}},
        # Orphan SELL (pre-feature era) -> still counted, no feature buckets
        {"ts": "2026-07-09T05:00:00", "type": "SELL", "pair": "LTCEUR", "price": 39.0,
         "pnl_eur": -0.2, "reason": "TRAILING_STOP", "extra": {}},
        # Short open+close (EVENT)
        {"ts": "2026-07-12T02:00:00", "type": "SHORT_OPEN", "pair": "SOLEUR", "price": 70.0,
         "pnl_eur": 0.0, "reason": "SHORT_OPEN_EXECUTED",
         "extra": {"short_type": "EVENT", "features": {"intelligence_score": -2.4,
                                                        "hl_bias": -3.0, "whale_score": -2.0,
                                                        "hour_utc": 2}}},
        {"ts": "2026-07-12T05:00:00", "type": "SHORT_CLOSE", "pair": "SOLEUR", "price": 68.9,
         "pnl_eur": 0.4, "reason": "SHORT_TAKE_PROFIT", "extra": {}},
    ]
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.write("this is not json\n")


def test_load_and_buckets():
    from backtest.journal_analysis import load_trades, bucket_stats

    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "j.jsonl")
        _mk_fixture(p)
        trades = load_trades(p)

    longs = [t for t in trades if t["side"] == "long"]
    shorts = [t for t in trades if t["side"] == "short"]
    assert len(longs) == 3, longs          # 2 feature-paired + 1 orphan
    assert len(shorts) == 1, shorts
    assert shorts[0].get("short_type") == "EVENT"

    # Feature pairing: the ADA win must carry its BUY features
    ada = next(t for t in longs if t["pair"] == "ADAEUR")
    assert ada["features"]["smart_action"] == "boost"
    assert ada["pnl_eur"] == 1.0

    # Buckets by smart_action
    stats = bucket_stats(longs, lambda t: (t.get("features") or {}).get("smart_action") or "none-recorded")
    assert stats["boost"]["n"] == 1 and stats["boost"]["wins"] == 1
    assert stats["neutral"]["n"] == 1 and stats["neutral"]["wins"] == 0
    assert stats["none-recorded"]["n"] == 1
    assert abs(stats["boost"]["net_eur"] - 1.0) < 1e-9
    print("test_load_and_buckets OK")


def test_empty_and_missing():
    from backtest.journal_analysis import load_trades
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "empty.jsonl")
        open(p, "w").close()
        assert load_trades(p) == []
        assert load_trades(os.path.join(td, "nope.jsonl")) == []
    print("test_empty_and_missing OK")


if __name__ == "__main__":
    test_load_and_buckets()
    test_empty_and_missing()
    print("ALL OK")

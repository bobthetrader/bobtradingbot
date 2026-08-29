"""Tests for backtest/exit_replay.py.

These guard the two things the replay must get right for its verdicts to mean
anything: the exit ladder must fire in the same ORDER the bot fires it, and the
live config must be translated into a policy faithfully (a mistranslation would
silently compare the bot against something it is not running).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtest"))

import exit_replay  # noqa: E402


def _trade(path, entry=100.0, notional=65.0):
    """path = [(high, low, close), ...] in absolute price."""
    return {"entry": entry, "path": path, "notional": notional}


BASE = dict(stop=3.0, tp=5.0, trail_min=1.5, trail_dist=2.0, hours=48)


def test_stop_loss_fires_at_the_configured_level():
    t = _trade([(100.5, 96.0, 97.0)])
    gross, reason = exit_replay.simulate(t, **BASE)
    assert reason == "STOP_LOSS"
    assert gross == -3.0


def test_take_profit_fires_at_the_configured_level():
    t = _trade([(105.5, 100.0, 105.0)])
    gross, reason = exit_replay.simulate(t, **BASE)
    assert reason == "TAKE_PROFIT"
    assert gross == 5.0


def test_adverse_level_wins_when_a_candle_spans_both():
    """High and low ordering inside a candle is unknown, so the replay must take
    the pessimistic branch - otherwise every wide candle prints a fake win."""
    t = _trade([(106.0, 96.0, 100.0)])
    _, reason = exit_replay.simulate(t, **BASE)
    assert reason == "STOP_LOSS"


def test_trailing_needs_peak_above_min_plus_distance():
    # peaks at +3.0%, which is below trail_min (1.5) + trail_dist (2.0) = 3.5,
    # so the give-back must NOT trigger a trailing exit yet.
    t = _trade([(103.0, 100.0, 103.0), (103.0, 100.5, 100.5)])
    _, reason = exit_replay.simulate(t, **BASE)
    assert reason == "TIME_STOP"

    # peaks at +4.0% (>= 3.5), then gives back 2.0% -> trailing exit at +2.0%
    t = _trade([(104.0, 100.0, 104.0), (104.0, 101.9, 101.9)])
    gross, reason = exit_replay.simulate(t, **BASE)
    assert reason == "TRAILING_STOP"
    assert abs(gross - 2.0) < 1e-9


def test_break_even_fires_only_after_its_trigger():
    policy = dict(BASE, break_even_trigger=1.0, break_even_offset=0.7)
    # never reaches +1.0%, so the break-even stop is not armed
    t = _trade([(100.5, 100.2, 100.4), (100.6, 100.5, 100.5)])
    _, reason = exit_replay.simulate(t, **policy)
    assert reason == "TIME_STOP"
    # reaches +1.5%, then falls back through entry+0.7%
    t = _trade([(101.5, 100.0, 101.5), (101.5, 100.5, 100.6)])
    gross, reason = exit_replay.simulate(t, **policy)
    assert reason == "BREAK_EVEN"
    assert gross == 0.7


def test_time_stop_exits_on_the_close_of_the_last_allowed_bar():
    path = [(100.2, 99.9, 100.1)] * 24 + [(100.2, 99.9, 101.0)]
    gross, reason = exit_replay.simulate(_trade(path), **dict(BASE, hours=2.0))
    assert reason == "TIME_STOP"
    assert abs(gross - 0.1) < 1e-9  # bar 24 close, not the later +1.0% bar


def test_policy_from_config_uses_the_higher_of_stop_and_its_floor():
    cfg = {"risk_management": {"stop_loss_percent": 1.5, "min_stop_loss_percent": 3.0}}
    assert exit_replay.policy_from_config(cfg)["stop"] == 3.0


def test_policy_from_config_disables_break_even_when_flag_is_false():
    cfg = {"risk_management": {"enable_break_even": False, "break_even_trigger_percent": 1.0}}
    assert exit_replay.policy_from_config(cfg)["break_even_trigger"] is None

    cfg["risk_management"]["enable_break_even"] = True
    assert exit_replay.policy_from_config(cfg)["break_even_trigger"] == 1.0


def test_live_config_matches_the_shipped_paper_config():
    """The exit rebuild of 2026-08-29 - if someone reverts a value, this fails."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = exit_replay.load_config(os.path.join(root, "config.paper.toml"))
    policy = exit_replay.policy_from_config(cfg)
    assert policy["break_even_trigger"] is None, "break-even must stay disabled"
    assert policy["stop"] == 3.0
    assert policy["tp"] == 5.0
    assert policy["hours"] == 48


def test_effective_fee_recovers_the_charged_round_trip():
    sells = [{"pair": "ETHEUR", "volume": 1.0, "pnl_eur": 0.48,
              "extra": {"pnl_pct": 1.0}}]
    notional = {("ETHEUR", 1.0): 100.0}
    assert abs(exit_replay.effective_fee_pct(sells, notional) - 0.52) < 1e-9

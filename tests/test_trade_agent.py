"""AI Trade Desk unit tests — decision parsing/clamping, fallback paths,
policy knobs, and tuner bound enforcement. No real CLI calls."""

import json
import os
import sys
import importlib.util

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.trade_agent import TradeAgent, SIZE_MULT_MIN, SIZE_MULT_MAX
from core import agent_tuner


def make_agent(tmp_path, cfg=None):
    cfg = cfg if cfg is not None else {"enabled": True}
    agent = TradeAgent(data_dir=str(tmp_path), config_getter=lambda: cfg)
    agent._cli = "claude"  # pretend the CLI exists; _call_cli is mocked per-test
    return agent


CTX = {"pair": "XBTEUR", "price": 100000.0, "score": 14.2, "rsi_1h": 55.0}


# ── parsing / clamping ────────────────────────────────────────────────────────

def test_parse_valid_decision():
    out = TradeAgent._parse_decision(
        '{"decision":"buy","size_mult":1.1,"confidence":0.8,"reason":"confluence"}')
    assert out == {"decision": "buy", "size_mult": 1.1, "confidence": 0.8,
                   "reason": "confluence"}


def test_parse_clamps_size_and_confidence():
    out = TradeAgent._parse_decision(
        '{"decision":"buy","size_mult":9.0,"confidence":1.7,"reason":"x"}')
    assert out["size_mult"] == SIZE_MULT_MAX
    assert out["confidence"] == 1.0
    out = TradeAgent._parse_decision(
        '{"decision":"buy","size_mult":0.01,"confidence":-3,"reason":"x"}')
    assert out["size_mult"] == SIZE_MULT_MIN
    assert out["confidence"] == 0.0


def test_parse_tolerates_surrounding_text():
    out = TradeAgent._parse_decision(
        'Here you go:\n```json\n{"decision":"skip","size_mult":1.0,'
        '"confidence":0.4,"reason":"weak"}\n```')
    assert out["decision"] == "skip"


def test_parse_rejects_garbage():
    assert TradeAgent._parse_decision("not json at all") is None
    assert TradeAgent._parse_decision('{"decision":"hold"}') is None
    assert TradeAgent._parse_decision("") is None
    assert TradeAgent._parse_decision(None) is None


# ── decide() paths ────────────────────────────────────────────────────────────

def test_decide_buy_journals(tmp_path):
    agent = make_agent(tmp_path)
    agent._call_cli = lambda p: ('{"decision":"buy","size_mult":1.2,'
                                 '"confidence":0.9,"reason":"strong"}')
    out = agent.decide(dict(CTX))
    assert out["decision"] == "buy" and out["size_mult"] == 1.2
    rows = [json.loads(l) for l in
            open(tmp_path / "agent_decisions.jsonl", encoding="utf-8")]
    assert rows[-1]["source"] == "agent" and rows[-1]["pair"] == "XBTEUR"


def test_decide_disabled_returns_none(tmp_path):
    agent = make_agent(tmp_path, cfg={"enabled": False})
    assert agent.decide(dict(CTX)) is None


def test_decide_cli_error_falls_back(tmp_path):
    agent = make_agent(tmp_path)
    agent._call_cli = lambda p: None
    assert agent.decide(dict(CTX)) is None
    rows = [json.loads(l) for l in
            open(tmp_path / "agent_decisions.jsonl", encoding="utf-8")]
    assert rows[-1]["source"] == "cli_error"


def test_decide_bad_output_falls_back(tmp_path):
    agent = make_agent(tmp_path)
    agent._call_cli = lambda p: "I think you should probably buy this one"
    assert agent.decide(dict(CTX)) is None
    rows = [json.loads(l) for l in
            open(tmp_path / "agent_decisions.jsonl", encoding="utf-8")]
    assert rows[-1]["source"] == "bad_output"


def test_decide_call_cap(tmp_path):
    agent = make_agent(tmp_path, cfg={"enabled": True, "max_calls_per_day": 1})
    agent._call_cli = lambda p: ('{"decision":"buy","size_mult":1.0,'
                                 '"confidence":0.9,"reason":"ok"}')
    assert agent.decide(dict(CTX)) is not None
    assert agent.decide(dict(CTX)) is None   # over cap -> fallback
    rows = [json.loads(l) for l in
            open(tmp_path / "agent_decisions.jsonl", encoding="utf-8")]
    assert rows[-1]["source"] == "call_cap"


def test_confidence_floor_converts_buy_to_skip(tmp_path):
    (tmp_path / "agent_policy.json").write_text(json.dumps(
        {"playbook": "x", "knobs": {"min_confidence": 0.6}}), encoding="utf-8")
    agent = make_agent(tmp_path)
    agent._call_cli = lambda p: ('{"decision":"buy","size_mult":1.0,'
                                 '"confidence":0.4,"reason":"meh"}')
    out = agent.decide(dict(CTX))
    assert out["decision"] == "skip"
    assert "floor" in out["reason"]


def test_playbook_reaches_prompt(tmp_path):
    (tmp_path / "agent_policy.json").write_text(json.dumps(
        {"playbook": "AVOID-DOT-LESSON", "knobs": {}}), encoding="utf-8")
    agent = make_agent(tmp_path)
    prompts = []
    agent._call_cli = lambda p: (prompts.append(p) or
                                 '{"decision":"skip","size_mult":1.0,'
                                 '"confidence":0.5,"reason":"x"}')
    agent.decide(dict(CTX))
    assert "AVOID-DOT-LESSON" in prompts[0]


# ── tuner bounds ──────────────────────────────────────────────────────────────

def test_tuner_clamps_knobs_and_writes_policy(tmp_path, monkeypatch):
    result = json.dumps({"type": "result", "is_error": False, "result": json.dumps({
        "playbook": "Only take mean-reversion above 1h RSI 50.",
        "knobs": {"min_confidence": 5.0, "default_size_mult": 0.1},
        "reasoning": "test",
    })})

    class FakeProc:
        returncode = 0
        stdout = result
        stderr = ""

    monkeypatch.setattr(agent_tuner.shutil, "which", lambda n: "claude")
    monkeypatch.setattr(agent_tuner.subprocess, "run", lambda *a, **k: FakeProc())

    ok = agent_tuner.run_nightly_tune(str(tmp_path), config={}, paper_mode=True)
    assert ok
    policy = json.loads((tmp_path / "agent_policy.json").read_text(encoding="utf-8"))
    assert policy["knobs"]["min_confidence"] == 0.8       # clamped from 5.0
    assert policy["knobs"]["default_size_mult"] == 0.6    # clamped from 0.1
    log = [json.loads(l) for l in
           open(tmp_path / "agent_tuner_log.jsonl", encoding="utf-8")]
    assert log[-1]["status"] == "ok" and log[-1]["clamped"]


def test_tuner_bad_output_keeps_old_policy(tmp_path, monkeypatch):
    (tmp_path / "agent_policy.json").write_text(json.dumps(
        {"playbook": "KEEP-ME", "knobs": {}}), encoding="utf-8")

    class FakeProc:
        returncode = 0
        stdout = json.dumps({"type": "result", "is_error": False,
                             "result": "sorry, no json today"})
        stderr = ""

    monkeypatch.setattr(agent_tuner.shutil, "which", lambda n: "claude")
    monkeypatch.setattr(agent_tuner.subprocess, "run", lambda *a, **k: FakeProc())

    ok = agent_tuner.run_nightly_tune(str(tmp_path), config={}, paper_mode=True)
    assert not ok
    policy = json.loads((tmp_path / "agent_policy.json").read_text(encoding="utf-8"))
    assert policy["playbook"] == "KEEP-ME"

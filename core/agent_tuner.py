"""Nightly self-tuning for the AI Trade Desk.

Once per UTC day the bot calls this with its own trade journal + the agent's
decision journal. One Claude Code CLI call (Max subscription — no metered
billing) reviews the last 14 days of outcomes and rewrites:

  - the PLAYBOOK: <=1200 chars of plain-text lessons injected into every
    next-day decision prompt (this is how learning reaches real-time trades)
  - bounded KNOBS: min_confidence and default_size_mult, hard-clamped in code

Output goes to data/agent_policy.json; every run (including failures) is
appended to data/agent_tuner_log.jsonl for auditing. A failed tune leaves the
previous policy untouched.
"""

import json
import logging
import os
import shutil
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

POLICY_FILE = "agent_policy.json"
TUNER_LOG_FILE = "agent_tuner_log.jsonl"
DECISIONS_FILE = "agent_decisions.jsonl"

LOOKBACK_DAYS = 14
TUNE_TIMEOUT_SECONDS = 120

# Hard bounds for tuner-adjustable knobs
KNOB_BOUNDS = {
    "min_confidence": (0.0, 0.8),
    "default_size_mult": (0.6, 1.2),
}

_TUNE_PROMPT = """You are the nightly reviewer for a small crypto paper-trading bot's AI trade desk (Kraken EUR pairs, long-only spot, ~EUR50-70 positions, ~0.8% round-trip taker fees). Your ONLY objective is to make the bot net-profitable after fees.

Below are the last {days} days of CLOSED TRADES (net P&L after fees) and the trade desk's DECISIONS (including skips, with price move since skipping where known).

CLOSED TRADE BUCKETS:
{buckets}

RECENT CLOSED TRADES (newest last):
{trades}

TRADE DESK DECISIONS:
{decisions}

CURRENT PLAYBOOK:
{playbook}

Write an improved policy. Be specific and evidence-based: name pairs, RSI bands, hours, setups that are winning or losing IN THIS DATA — not generic trading advice. Keep lessons that are still supported by the data; drop ones that are not. If the data is too thin to conclude something, say the playbook should stay conservative.

Respond with ONLY a single JSON object, no markdown fences:
{{"playbook": "max 1200 chars of concrete lessons for per-trade decisions", "knobs": {{"min_confidence": 0.0-0.8, "default_size_mult": 0.6-1.2}}, "reasoning": "max 500 chars on what you changed and why"}}

Do not use any tools."""


# ── data loading ──────────────────────────────────────────────────────────────

def _load_jsonl(path, since_ts=None):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if since_ts is not None:
                ts = str(row.get("ts", ""))[:19]
                if ts and ts < since_ts:
                    continue
            rows.append(row)
    return rows


def _closed_trades(data_dir, paper_mode, since_ts):
    """SELL rows with features joined FIFO from preceding BUYs (same logic as
    backtest/journal_analysis.py, trimmed)."""
    fname = "trade_events_paper.jsonl" if paper_mode else "trade_events_live.jsonl"
    events = _load_jsonl(os.path.join(data_dir, fname))
    open_buys = defaultdict(list)
    out = []
    for e in events:
        t, pair = e.get("type"), e.get("pair")
        extra = e.get("extra") or {}
        if t == "BUY":
            open_buys[pair].append(extra.get("features") or {})
        elif t == "SELL":
            feats = open_buys[pair].pop(0) if open_buys[pair] else {}
            ts = str(e.get("ts", ""))[:19]
            if ts and ts < since_ts:
                continue
            out.append({
                "pair": pair, "ts": ts,
                "pnl_eur": round(float(e.get("pnl_eur") or 0), 2),
                "reason": e.get("reason", ""),
                "strategy": feats.get("strategy"),
                "rsi_1h": feats.get("rsi_1h"),
                "hour_utc": feats.get("hour_utc"),
                "smart_action": feats.get("smart_action"),
                "agent_decided": feats.get("agent_decided"),
            })
    return out


def _bucket_lines(trades):
    """Compact WR/net text table per bucket dimension."""
    def band(v, edges, labels):
        if v is None:
            return "n/a"
        for e, lab in zip(edges, labels):
            if float(v) <= e:
                return lab
        return labels[-1]

    dims = {
        "strategy": lambda t: t.get("strategy") or "n/a",
        "rsi_band": lambda t: band(t.get("rsi_1h"), [40, 50, 60], ["<40", "40-50", "50-60", ">=60"]),
        "hour": lambda t: band(t.get("hour_utc"), [7, 11, 15, 19], ["00-07", "08-11", "12-15", "16-19", "20-23"]),
        "pair": lambda t: t.get("pair") or "?",
        "smart": lambda t: t.get("smart_action") or "n/a",
        "exit": lambda t: t.get("reason") or "?",
        "agent": lambda t: "agent" if t.get("agent_decided") else "rules",
    }
    lines = []
    for name, key in dims.items():
        agg = defaultdict(lambda: [0, 0, 0.0])   # n, wins, net
        for t in trades:
            a = agg[key(t)]
            a[0] += 1
            a[1] += 1 if t["pnl_eur"] > 0 else 0
            a[2] += t["pnl_eur"]
        parts = [f"{k}: n={v[0]} WR={100 * v[1] / v[0]:.0f}% net={v[2]:+.2f}"
                 for k, v in sorted(agg.items(), key=lambda kv: -kv[1][0]) if v[0] >= 2]
        if parts:
            lines.append(f"[{name}] " + " | ".join(parts))
    return "\n".join(lines) or "(no closed trades in window)"


def _decision_lines(decisions, price_lookup):
    """Compact decision log; for skips, annotate price move since skip."""
    lines = []
    for d in decisions[-60:]:
        if d.get("source") != "agent":
            continue
        pair, verdict = d.get("pair", "?"), d.get("decision", "?")
        line = (f"{str(d.get('ts', ''))[:16]} {pair} {verdict}"
                f" conf={d.get('confidence')} size={d.get('size_mult')}"
                f" — {str(d.get('reason', ''))[:80]}")
        if verdict == "skip":
            try:
                then = float(d.get("price") or 0)
                now = float(price_lookup(pair) or 0)
                if then > 0 and now > 0:
                    line += f" [since skip: {100 * (now - then) / then:+.1f}%]"
            except Exception:
                pass
        lines.append(line)
    return "\n".join(lines) or "(no agent decisions yet)"


# ── the nightly run ───────────────────────────────────────────────────────────

def run_nightly_tune(data_dir, config, paper_mode=True, price_lookup=lambda p: None,
                     model="sonnet"):
    """Build the evidence pack, call the CLI once, clamp + persist the policy.
    Returns True when a new policy was written. Never raises."""
    log_path = os.path.join(data_dir, TUNER_LOG_FILE)

    def audit(status, detail):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "status": status, **detail}, default=str) + "\n")
        except Exception:
            pass

    try:
        cli = shutil.which("claude") or shutil.which("claude.cmd")
        if cli is None:
            audit("skipped", {"error": "claude CLI not found"})
            return False

        since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
                 ).strftime("%Y-%m-%dT%H:%M:%S")
        trades = _closed_trades(data_dir, paper_mode, since)
        decisions = _load_jsonl(os.path.join(data_dir, DECISIONS_FILE), since_ts=since)

        policy_path = os.path.join(data_dir, POLICY_FILE)
        current_playbook = "(none yet)"
        try:
            with open(policy_path, "r", encoding="utf-8") as f:
                current_playbook = (json.load(f) or {}).get("playbook") or current_playbook
        except Exception:
            pass

        trade_lines = "\n".join(
            f"{t['ts'][:16]} {t['pair']} {t['pnl_eur']:+.2f} {t['reason']}"
            f" strat={t.get('strategy')} rsi={t.get('rsi_1h')} h={t.get('hour_utc')}"
            for t in trades[-80:]) or "(none)"

        prompt = _TUNE_PROMPT.format(
            days=LOOKBACK_DAYS,
            buckets=_bucket_lines(trades),
            trades=trade_lines,
            decisions=_decision_lines(decisions, price_lookup),
            playbook=current_playbook[:1500],
        )

        # Prompt via STDIN (argv mangles multi-line prompts through the
        # Windows .CMD shim and risks the cmd.exe length limit)
        proc = subprocess.run(
            [cli, "-p", "--output-format", "json",
             "--model", model, "--strict-mcp-config"],
            input=prompt, capture_output=True, text=True,
            timeout=float(config.get("tune_timeout_seconds", TUNE_TIMEOUT_SECONDS)),
            cwd=data_dir,
        )
        if proc.returncode != 0:
            audit("cli_error", {"exit": proc.returncode,
                                "stderr": (proc.stderr or "")[:300]})
            return False
        try:
            envelope = json.loads(proc.stdout)
            text = envelope.get("result", "") if isinstance(envelope, dict) else proc.stdout
        except Exception:
            text = proc.stdout

        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            audit("bad_output", {"raw": text[:400]})
            return False
        obj = json.loads(text[start:end + 1])

        playbook = str(obj.get("playbook", ""))[:1200]
        if not playbook.strip():
            audit("bad_output", {"error": "empty playbook"})
            return False

        knobs, clamped = {}, []
        for name, (lo, hi) in KNOB_BOUNDS.items():
            try:
                raw = float((obj.get("knobs") or {}).get(name))
            except (TypeError, ValueError):
                continue
            val = max(lo, min(hi, raw))
            if val != raw:
                clamped.append(f"{name}: {raw} -> {val}")
            knobs[name] = round(val, 2)

        policy = {
            "playbook": playbook,
            "knobs": knobs,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        tmp = policy_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(policy, f, indent=2)
        os.replace(tmp, policy_path)

        audit("ok", {"trades": len(trades), "decisions": len(decisions),
                     "knobs": knobs, "clamped": clamped,
                     "reasoning": str(obj.get("reasoning", ""))[:500],
                     "playbook": playbook})
        logger.info("Agent tuner: policy updated (%d trades, %d decisions reviewed)",
                    len(trades), len(decisions))
        return True
    except subprocess.TimeoutExpired:
        audit("timeout", {})
        return False
    except Exception as exc:
        audit("error", {"error": str(exc)[:300]})
        logger.warning("Agent tuner failed: %s", exc)
        return False

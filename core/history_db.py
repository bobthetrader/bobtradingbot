"""
Bot History Database
=====================
SQLite-backed persistent store for all bot data points. Survives Railway
redeploys because the DB file lives on the mounted volume at /app/data/.

Tables
------
  trades              Every trade executed (buy/sell/short open/close)
  ai_panels           Every AI panel result with per-model scores
  sharpe_snapshots    Funding rates + insider signal per intelligence refresh
  bot_snapshots       Balance, signals, Sharpe per loop (sampled every N loops)
  optimizer_decisions Every parameter experiment outcome

On restart the context builder reads recent history and injects it into
every AI model prompt so they calibrate against actual past performance.
"""

import os
import sqlite3
import threading
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "bot_history.db")
_lock    = threading.Lock()
_MAX_ROWS_PER_TABLE = 5000   # keep last N rows, prune older ones


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    type         TEXT,
    pair         TEXT,
    qty          REAL,
    price        REAL,
    pnl_eur      REAL,
    balance_after REAL,
    paper_mode   INTEGER,
    reason       TEXT
);

CREATE TABLE IF NOT EXISTS ai_panels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    combined_score  REAL,
    hermes_score    REAL,
    sonar_score     REAL,
    deepseek_score  REAL,
    llama_score     REAL,
    gpt_score       REAL,
    hermes_text     TEXT,
    sonar_text      TEXT,
    sharpe_at_time  REAL,
    market_outcome  TEXT
);

CREATE TABLE IF NOT EXISTS sharpe_snapshots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                   TEXT NOT NULL,
    btc_funding_score    REAL,
    eth_funding_score    REAL,
    sol_funding_score    REAL,
    xrp_funding_score    REAL,
    insider_signal       REAL,
    total_oi_usd         REAL,
    oi_weighted_funding  REAL
);

CREATE TABLE IF NOT EXISTS bot_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,
    loop                INTEGER,
    balance_eur         REAL,
    trade_count         INTEGER,
    sharpe_score        REAL,
    sharpe_verdict      TEXT,
    regime              TEXT,
    intelligence_score  REAL,
    btc_signal          TEXT,
    eth_signal          TEXT,
    sol_signal          TEXT,
    xrp_signal          TEXT,
    paper_mode          INTEGER
);

CREATE TABLE IF NOT EXISTS optimizer_decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    param_key    TEXT,
    section      TEXT,
    old_value    REAL,
    new_value    REAL,
    sharpe_before REAL,
    sharpe_after  REAL,
    verdict      TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_ts        ON trades (ts);
CREATE INDEX IF NOT EXISTS idx_ai_panels_ts     ON ai_panels (ts);
CREATE INDEX IF NOT EXISTS idx_sharpe_ts        ON sharpe_snapshots (ts);
CREATE INDEX IF NOT EXISTS idx_bot_snapshots_ts ON bot_snapshots (ts);
"""


# ── Connection helper ──────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    con = sqlite3.connect(_DB_PATH, timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    """Create tables and indexes if they don't exist."""
    try:
        with _lock:
            con = _conn()
            con.executescript(_SCHEMA)
            con.commit()
            con.close()
        logger.info("History DB initialised at %s", _DB_PATH)
    except Exception as exc:
        logger.warning("History DB init failed: %s", exc)


def _prune(con: sqlite3.Connection, table: str) -> None:
    """Keep only the last _MAX_ROWS_PER_TABLE rows in a table."""
    try:
        con.execute(
            f"DELETE FROM {table} WHERE id NOT IN "
            f"(SELECT id FROM {table} ORDER BY id DESC LIMIT ?)",
            (_MAX_ROWS_PER_TABLE,),
        )
    except Exception:
        pass


# ── Write helpers ──────────────────────────────────────────────────────────────

def record_trade(ts: str, ttype: str, pair: str, qty: float, price: float,
                 pnl_eur: float, balance_after: float, paper_mode: bool,
                 reason: str = "") -> None:
    try:
        with _lock:
            con = _conn()
            con.execute(
                "INSERT INTO trades (ts,type,pair,qty,price,pnl_eur,balance_after,paper_mode,reason) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (ts, ttype, pair, qty, price, pnl_eur, balance_after, int(paper_mode), reason),
            )
            _prune(con, "trades")
            con.commit()
            con.close()
    except Exception as exc:
        logger.debug("record_trade failed: %s", exc)


def record_ai_panel(ts: str, scores: dict, texts: dict, sharpe: Optional[float]) -> None:
    try:
        with _lock:
            con = _conn()
            con.execute(
                "INSERT INTO ai_panels "
                "(ts,combined_score,hermes_score,sonar_score,deepseek_score,llama_score,gpt_score,"
                "hermes_text,sonar_text,sharpe_at_time,market_outcome) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts,
                    scores.get("combined"),
                    scores.get("hermes"),
                    scores.get("sonar"),
                    scores.get("deepseek"),
                    scores.get("mistral"),   # stored under 'mistral' key but is llama
                    scores.get("gpt"),
                    texts.get("hermes", "")[:300],
                    texts.get("sonar", "")[:300],
                    sharpe,
                    "pending",
                ),
            )
            _prune(con, "ai_panels")
            con.commit()
            con.close()
    except Exception as exc:
        logger.debug("record_ai_panel failed: %s", exc)


def update_ai_outcome(outcome: str) -> None:
    """Mark the most recent pending AI panel with what actually happened."""
    try:
        with _lock:
            con = _conn()
            con.execute(
                "UPDATE ai_panels SET market_outcome=? WHERE market_outcome='pending' "
                "ORDER BY id DESC LIMIT 1",
                (outcome,),
            )
            con.commit()
            con.close()
    except Exception as exc:
        logger.debug("update_ai_outcome failed: %s", exc)


def record_sharpe_snapshot(ts: str, funding_scores: dict, insider_signal: float,
                            total_oi: Optional[float], oi_weighted: Optional[float]) -> None:
    try:
        with _lock:
            con = _conn()
            con.execute(
                "INSERT INTO sharpe_snapshots "
                "(ts,btc_funding_score,eth_funding_score,sol_funding_score,xrp_funding_score,"
                "insider_signal,total_oi_usd,oi_weighted_funding) VALUES (?,?,?,?,?,?,?,?)",
                (
                    ts,
                    funding_scores.get("BTC"),
                    funding_scores.get("ETH"),
                    funding_scores.get("SOL"),
                    funding_scores.get("XRP"),
                    insider_signal,
                    total_oi,
                    oi_weighted,
                ),
            )
            _prune(con, "sharpe_snapshots")
            con.commit()
            con.close()
    except Exception as exc:
        logger.debug("record_sharpe_snapshot failed: %s", exc)


def record_bot_snapshot(ts: str, loop: int, balance: float, trade_count: int,
                         sharpe: Optional[float], verdict: str, regime: str,
                         intel_score: float, signals: dict, paper_mode: bool) -> None:
    try:
        with _lock:
            con = _conn()
            con.execute(
                "INSERT INTO bot_snapshots "
                "(ts,loop,balance_eur,trade_count,sharpe_score,sharpe_verdict,regime,"
                "intelligence_score,btc_signal,eth_signal,sol_signal,xrp_signal,paper_mode) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, loop, balance, trade_count, sharpe, verdict, regime,
                    intel_score,
                    signals.get("XBTEUR") or signals.get("BTC"),
                    signals.get("ETHEUR") or signals.get("ETH"),
                    signals.get("SOLEUR") or signals.get("SOL"),
                    signals.get("XRPEUR") or signals.get("XRP"),
                    int(paper_mode),
                ),
            )
            _prune(con, "bot_snapshots")
            con.commit()
            con.close()
    except Exception as exc:
        logger.debug("record_bot_snapshot failed: %s", exc)


def record_optimizer_decision(ts: str, param_key: str, section: str,
                               old_val: float, new_val: float,
                               sharpe_before: float, sharpe_after: Optional[float],
                               verdict: str) -> None:
    try:
        with _lock:
            con = _conn()
            con.execute(
                "INSERT INTO optimizer_decisions "
                "(ts,param_key,section,old_value,new_value,sharpe_before,sharpe_after,verdict) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ts, param_key, section, old_val, new_val, sharpe_before, sharpe_after, verdict),
            )
            _prune(con, "optimizer_decisions")
            con.commit()
            con.close()
    except Exception as exc:
        logger.debug("record_optimizer_decision failed: %s", exc)


# ── Read helpers (for AI context injection) ────────────────────────────────────

def get_recent_trades(n: int = 10) -> list:
    try:
        with _lock:
            con = _conn()
            rows = con.execute(
                "SELECT ts,type,pair,pnl_eur,balance_after FROM trades "
                "ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
            con.close()
            return [dict(r) for r in reversed(rows)]
    except Exception:
        return []


def get_recent_ai_panels(n: int = 8) -> list:
    try:
        with _lock:
            con = _conn()
            rows = con.execute(
                "SELECT ts,combined_score,hermes_score,sonar_score,sharpe_at_time,market_outcome "
                "FROM ai_panels ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
            con.close()
            return [dict(r) for r in reversed(rows)]
    except Exception:
        return []


def get_recent_sharpe_snapshots(n: int = 5) -> list:
    try:
        with _lock:
            con = _conn()
            rows = con.execute(
                "SELECT ts,btc_funding_score,eth_funding_score,sol_funding_score,"
                "xrp_funding_score,insider_signal,total_oi_usd FROM sharpe_snapshots "
                "ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
            con.close()
            return [dict(r) for r in reversed(rows)]
    except Exception:
        return []


def get_recent_optimizer_decisions(n: int = 10) -> list:
    try:
        with _lock:
            con = _conn()
            rows = con.execute(
                "SELECT ts,param_key,old_value,new_value,sharpe_before,sharpe_after,verdict "
                "FROM optimizer_decisions ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
            con.close()
            return [dict(r) for r in reversed(rows)]
    except Exception:
        return []


def get_db_stats() -> dict:
    """Return row counts per table — used by dashboard."""
    stats = {}
    try:
        with _lock:
            con = _conn()
            for table in ("trades", "ai_panels", "sharpe_snapshots",
                          "bot_snapshots", "optimizer_decisions"):
                count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                stats[table] = count
            # Oldest trade date
            oldest = con.execute(
                "SELECT MIN(ts) FROM trades"
            ).fetchone()[0]
            stats["oldest_trade"] = oldest
            con.close()
    except Exception:
        pass
    return stats


def build_history_context(n_trades: int = 10, n_panels: int = 6,
                           n_sharpe: int = 4, n_optimizer: int = 5) -> str:
    """
    Build a text block summarising recent history for injection into AI prompts.
    The models use this to calibrate: did past bearish calls precede drops?
    Did funding rate signals prove accurate?
    """
    lines = ["\n--- HISTORICAL CONTEXT (from persistent DB) ---"]

    # Recent trades
    trades = get_recent_trades(n_trades)
    if trades:
        lines.append(f"Last {len(trades)} trades:")
        for t in trades:
            pnl = t.get("pnl_eur") or 0
            outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "FLAT")
            lines.append(
                f"  {t['ts'][:16]} {t['type']} {t['pair']} "
                f"→ {outcome} {pnl:+.4f}EUR (bal after: €{t.get('balance_after', 0):.2f})"
            )

    # Recent AI panels
    panels = get_recent_ai_panels(n_panels)
    if panels:
        lines.append(f"\nLast {len(panels)} AI panel calls:")
        for p in panels:
            outcome = p.get("market_outcome") or "pending"
            lines.append(
                f"  {p['ts'][:16]} combined={p.get('combined_score', 0):+.2f} "
                f"hermes={p.get('hermes_score', 0):+.1f} "
                f"sonar={p.get('sonar_score', 0):+.1f} "
                f"sharpe_then={p.get('sharpe_at_time') or '—'} "
                f"outcome={outcome}"
            )

    # Recent funding rate trend
    snaps = get_recent_sharpe_snapshots(n_sharpe)
    if snaps:
        lines.append(f"\nFunding rate signal trend (last {len(snaps)} snapshots):")
        for s in snaps:
            lines.append(
                f"  {s['ts'][:16]} BTC={s.get('btc_funding_score', 0):+.0f} "
                f"ETH={s.get('eth_funding_score', 0):+.0f} "
                f"SOL={s.get('sol_funding_score', 0):+.0f} "
                f"XRP={s.get('xrp_funding_score', 0):+.0f} "
                f"insider={s.get('insider_signal', 0):+.1f}"
            )

    # Optimizer decisions
    opts = get_recent_optimizer_decisions(n_optimizer)
    if opts:
        lines.append(f"\nLast {len(opts)} optimizer decisions:")
        for o in opts:
            lines.append(
                f"  {o['ts'][:16]} {o['param_key']}: "
                f"{o['old_value']}→{o['new_value']} "
                f"Sharpe {o.get('sharpe_before', 0):.3f}→{o.get('sharpe_after') or '?'} "
                f"[{o['verdict']}]"
            )

    return "\n".join(lines) if len(lines) > 1 else ""

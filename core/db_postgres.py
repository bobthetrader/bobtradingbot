"""
PostgreSQL State Manager
=========================
Replaces fragile JSON file state with proper atomic PostgreSQL writes.

Manages three critical state stores:
  positions        — open trading positions (was purchase_prices_*.json)
  optimizer_state  — scientific method experiment state (was optimizer_state.json)
  listing_watchlist— new listings being tracked (was listing_watchlist.json)

Design:
  - Dual-write mode: writes to BOTH JSON files AND PostgreSQL simultaneously
  - Reads from PostgreSQL with automatic JSON file fallback if PG unavailable
  - Bot continues running without PostgreSQL (graceful degradation)
  - Zero downtime migration — flip to PG-primary once confirmed stable

Connection:
  Set DATABASE_URL in .env:
  DATABASE_URL=postgresql://tradingbot:PASSWORD@localhost:5432/tradingbot
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

_conn = None
_available = False
_last_attempt = 0.0
_RETRY_INTERVAL = 60   # retry connection every 60s if it fails


def _get_conn():
    """Return a live PostgreSQL connection, reconnecting if needed."""
    global _conn, _available, _last_attempt

    if _conn is not None:
        try:
            _conn.cursor().execute("SELECT 1")
            return _conn
        except Exception:
            _conn = None
            _available = False

    if time.time() - _last_attempt < _RETRY_INTERVAL:
        return None

    _last_attempt = time.time()
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return None

    try:
        import psycopg2
        _conn = psycopg2.connect(url)
        _conn.autocommit = True
        _available = True
        logger.info("PostgreSQL connected: %s", url.split("@")[-1])
        # Create schema on first successful connection
        try:
            init_schema()
        except Exception:
            pass
        return _conn
    except Exception as exc:
        logger.warning("PostgreSQL unavailable: %s — using JSON fallback", exc)
        _available = False
        return None


def is_available() -> bool:
    return _get_conn() is not None


def init_schema() -> bool:
    """Create tables if they don't exist. Safe to call on every startup."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    pair        VARCHAR(20)   NOT NULL,
                    mode        VARCHAR(10)   NOT NULL DEFAULT 'paper',
                    qty         DECIMAL(18,8) NOT NULL,
                    entry_price DECIMAL(18,8) NOT NULL,
                    buy_ts      TIMESTAMPTZ   DEFAULT NOW(),
                    meta        JSONB,
                    PRIMARY KEY (pair, mode)
                );

                CREATE TABLE IF NOT EXISTS optimizer_state (
                    key         VARCHAR(50) PRIMARY KEY,
                    value       JSONB        NOT NULL,
                    updated_at  TIMESTAMPTZ  DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS listing_watchlist (
                    symbol      VARCHAR(20) PRIMARY KEY,
                    data        JSONB        NOT NULL,
                    updated_at  TIMESTAMPTZ  DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS known_pairs (
                    pair        VARCHAR(20) PRIMARY KEY,
                    first_seen  TIMESTAMPTZ  DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS scalper_trades (
                    id          SERIAL        PRIMARY KEY,
                    ts          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
                    pair        VARCHAR(20)   NOT NULL,
                    entry_price DECIMAL(18,8) NOT NULL,
                    exit_price  DECIMAL(18,8) NOT NULL,
                    qty         DECIMAL(18,8) NOT NULL,
                    pnl_eur     DECIMAL(12,6) NOT NULL,
                    pnl_pct     DECIMAL(10,4) NOT NULL,
                    reason      VARCHAR(30)   NOT NULL,
                    held_min    DECIMAL(8,2)  NOT NULL
                );
            """)
        logger.info("PostgreSQL schema initialised")
        return True
    except Exception as exc:
        logger.warning("PostgreSQL schema init failed: %s", exc)
        return False


# ── Positions ─────────────────────────────────────────────────────────────────

def save_position(pair: str, qty: float, entry_price: float,
                  mode: str = "paper", meta: dict = None) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO positions (pair, mode, qty, entry_price, buy_ts, meta)
                VALUES (%s, %s, %s, %s, NOW(), %s)
                ON CONFLICT (pair, mode) DO UPDATE
                SET qty=EXCLUDED.qty, entry_price=EXCLUDED.entry_price,
                    buy_ts=NOW(), meta=EXCLUDED.meta
            """, (pair, mode, qty, entry_price, json.dumps(meta or {})))
        return True
    except Exception as exc:
        logger.warning("PG save_position failed: %s", exc)
        return False


def delete_position(pair: str, mode: str = "paper") -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM positions WHERE pair=%s AND mode=%s", (pair, mode))
        return True
    except Exception as exc:
        logger.warning("PG delete_position failed: %s", exc)
        return False


def load_positions(mode: str = "paper") -> dict:
    """Returns {pair: {qty, entry_price, meta}} or empty dict if PG unavailable."""
    conn = _get_conn()
    if not conn:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pair, qty, entry_price, meta FROM positions WHERE mode=%s",
                (mode,)
            )
            rows = cur.fetchall()
        return {
            row[0]: {
                "qty":         float(row[1]),
                "entry_price": float(row[2]),
                "meta":        row[3] or {},
            }
            for row in rows
        }
    except Exception as exc:
        logger.warning("PG load_positions failed: %s", exc)
        return {}


def clear_positions(mode: str = "paper") -> bool:
    """Delete all positions for the given mode (used on clean paper reset)."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM positions WHERE mode=%s", (mode,))
        return True
    except Exception as exc:
        logger.warning("PG clear_positions failed: %s", exc)
        return False


# ── Optimizer state ───────────────────────────────────────────────────────────

def save_optimizer_state(state: dict) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO optimizer_state (key, value, updated_at)
                VALUES ('main', %s, NOW())
                ON CONFLICT (key) DO UPDATE
                SET value=EXCLUDED.value, updated_at=NOW()
            """, (json.dumps(state),))
        return True
    except Exception as exc:
        logger.warning("PG save_optimizer_state failed: %s", exc)
        return False


def load_optimizer_state() -> Optional[dict]:
    conn = _get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM optimizer_state WHERE key='main'")
            row = cur.fetchone()
        return row[0] if row else None
    except Exception as exc:
        logger.warning("PG load_optimizer_state failed: %s", exc)
        return None


# ── Listing watchlist ──────────────────────────────────────────────────────────

def save_listing_watchlist(watchlist: dict) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            # Delete removed entries
            if watchlist:
                cur.execute(
                    "DELETE FROM listing_watchlist WHERE symbol NOT IN %s",
                    (tuple(watchlist.keys()),)
                )
            else:
                cur.execute("DELETE FROM listing_watchlist")
            # Upsert current entries
            for symbol, data in watchlist.items():
                cur.execute("""
                    INSERT INTO listing_watchlist (symbol, data, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (symbol) DO UPDATE
                    SET data=EXCLUDED.data, updated_at=NOW()
                """, (symbol, json.dumps(data)))
        return True
    except Exception as exc:
        logger.warning("PG save_listing_watchlist failed: %s", exc)
        return False


def load_listing_watchlist() -> Optional[dict]:
    conn = _get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, data FROM listing_watchlist")
            rows = cur.fetchall()
        return {row[0]: row[1] for row in rows}
    except Exception as exc:
        logger.warning("PG load_listing_watchlist failed: %s", exc)
        return None


# ── Known pairs ───────────────────────────────────────────────────────────────

def save_known_pairs(pairs: set) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            for pair in pairs:
                cur.execute("""
                    INSERT INTO known_pairs (pair) VALUES (%s)
                    ON CONFLICT (pair) DO NOTHING
                """, (pair,))
        return True
    except Exception as exc:
        logger.warning("PG save_known_pairs failed: %s", exc)
        return False


def load_known_pairs() -> Optional[set]:
    conn = _get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pair FROM known_pairs")
            rows = cur.fetchall()
        return {row[0] for row in rows}
    except Exception as exc:
        logger.warning("PG load_known_pairs failed: %s", exc)
        return None


# ── Scalper trades ────────────────────────────────────────────────────────────

def save_scalper_trade(pair: str, entry_price: float, exit_price: float,
                       qty: float, pnl_eur: float, pnl_pct: float,
                       reason: str, held_min: float) -> bool:
    """Insert a completed scalp trade. Returns True on success."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scalper_trades
                    (pair, entry_price, exit_price, qty, pnl_eur, pnl_pct, reason, held_min)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (pair, entry_price, exit_price, qty, pnl_eur, pnl_pct, reason, held_min))
        return True
    except Exception as exc:
        logger.warning("PG save_scalper_trade failed: %s", exc)
        return False

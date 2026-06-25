"""Scalping strategy — daemon thread alongside the main bot.

Fast loop (30 s). Trades BTC, ETH, SOL, XRP, LINK, AVAX, ADA, DOT, ATOM EUR pairs.
Paper-only: positions tracked in data/scalper_positions.json.

Signals (scored, threshold ±2 to enter):
  1-min RSI < 35 → +2  (oversold, buy)
  1-min RSI > 65 → -2  (overbought, sell)
  Price < VWAP by >0.3% → +1  (below fair value, buy)
  Price > VWAP by >0.3% → -1  (above fair value, sell)
  Order book bid vol > ask vol by >20% → +1  (buying pressure)
  Order book ask vol > bid vol by >20% → -1  (selling pressure)

TP: 0.7% / SL: 0.35%  (clears 0.52% round-trip Kraken taker fee)
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PAIRS          = [
    # Tier 1 — highest liquidity
    "XBTEUR", "XETHZEUR", "SOLEUR", "XXRPZEUR", "LINKEUR", "AVAXEUR",
    "ADAEUR", "DOTEUR", "ATOMEUR", "UNIEUR",
    # Tier 2 — high volume
    "LTCEUR", "BCHEUR", "TRXEUR", "XMREUR", "AAVEEUR", "NEAREUR",
    "ALGOEUR", "ETCEUR", "SHIBEUR", "ZECEUR",
    # Tier 3 — good volume
    "MKREUR", "SNXEUR", "OPEUR", "ARBEUR", "SANDEUR",
    "MANAUER", "INJEUR", "FTMEUR", "GALEUR", "APEEUR",
]
_INTERVAL_SEC   = 15
_RSI_PERIOD     = 14
_RSI_BUY        = 28.0     # tightened: only buy extremely oversold
_RSI_SELL       = 72.0     # tightened: only sell extremely overbought
_VWAP_CANDLES   = 30       # rolling window for VWAP calc
_VWAP_THRESH    = 0.003    # 0.3% deviation from VWAP to signal
_OB_IMBALANCE   = 0.20     # 20% bid/ask vol imbalance to signal
_SCORE_THRESH   = 2.5      # raised: require stronger combined signal
_TP_PCT         = 0.58     # take-profit % (base — adjusted dynamically by fee tier)
_SL_PCT         = 0.20     # stop-loss %
_ALLOCATION_EUR = 10.0     # paper EUR per scalp trade
_MAX_HOLD_MIN   = 60       # force-exit after 60 minutes regardless
_MIN_PROFIT_BPS = 0.06     # minimum net profit above round-trip fee (6 basis points)

# Kraken fee tiers: (30-day USD volume threshold, taker fee %)
# TP is set dynamically to round_trip_fee + _MIN_PROFIT_BPS
_FEE_TIERS = [
    (0,          0.26),   # <$50k      → round trip 0.52%  → TP 0.58%
    (50_000,     0.24),   # $50k+      → round trip 0.48%  → TP 0.54%
    (100_000,    0.22),   # $100k+     → round trip 0.44%  → TP 0.50%
    (250_000,    0.20),   # $250k+     → round trip 0.40%  → TP 0.46%
    (500_000,    0.18),   # $500k+     → round trip 0.36%  → TP 0.42%
    (1_000_000,  0.16),   # $1M+       → round trip 0.32%  → TP 0.38%
    (2_500_000,  0.14),   # $2.5M+     → round trip 0.28%  → TP 0.34%
    (5_000_000,  0.12),   # $5M+       → round trip 0.24%  → TP 0.30%
    (10_000_000, 0.10),   # $10M+      → round trip 0.20%  → TP 0.26%
]


def _fee_tier(volume_usd: float) -> tuple:
    """Return (taker_fee_pct, round_trip_pct, dynamic_tp_pct) for given 30-day volume."""
    taker = 0.26
    for threshold, fee in reversed(_FEE_TIERS):
        if volume_usd >= threshold:
            taker = fee
            break
    round_trip = taker * 2
    dynamic_tp = round(round_trip + _MIN_PROFIT_BPS, 4)
    return taker, round_trip, dynamic_tp


def _calc_rsi(closes: list, period: int = _RSI_PERIOD) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _calc_vwap(candles: list) -> Optional[float]:
    """Volume-weighted average across candle VWAPs for the rolling window."""
    num = denom = 0.0
    for row in candles:
        if len(row) < 7:
            continue
        vwap_c = float(row[5])
        vol    = float(row[6])
        num   += vwap_c * vol
        denom += vol
    return (num / denom) if denom > 0 else None


class ScalperEngine:
    """Scalping engine — start once, runs forever in a daemon thread."""

    def __init__(self, kraken_api, paper_mode: bool = True,
                 data_dir: str = "data", ws_feed=None):
        self._api      = kraken_api
        self._paper    = paper_mode
        self._ws       = ws_feed       # KrakenWSFeed instance (optional, faster prices)
        self._data_dir = Path(data_dir)
        self._lock     = threading.Lock()
        self._running  = False
        self._thread   = None

        # State
        self._positions: dict = {}     # pair → {qty, entry, ts, score}
        self._trade_log: list = []     # last 100 completed trades (in-memory)
        self._volume_usd: float = 0.0  # cumulative 30-day equivalent volume (USD)
        self._pair_scores: dict = {}   # pair → latest score (+ bullish, - bearish)

        # Persistent paths
        self._pos_path    = self._data_dir / "scalper_positions.json"
        self._trades_path = self._data_dir / "scalper_trades.jsonl"

        self._load_positions()
        self._load_volume()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="ScalperEngine"
        )
        self._thread.start()
        logger.info(
            "[SCALP] Engine started | pairs=%s | TP=%.1f%% | SL=%.1f%% | alloc=€%.0f",
            ", ".join(_PAIRS), _TP_PCT, _SL_PCT, _ALLOCATION_EUR,
        )

    def stop(self):
        self._running = False

    def get_status(self) -> dict:
        """Snapshot consumed by the dashboard."""
        with self._lock:
            positions   = {p: dict(v) for p, v in self._positions.items()}
            recent      = list(self._trade_log[-20:])
            pair_scores = dict(self._pair_scores)
        wins  = sum(1 for t in self._trade_log if t.get("pnl_eur", 0) > 0)
        losses = sum(1 for t in self._trade_log if t.get("pnl_eur", 0) < 0)
        total = len(self._trade_log)
        taker, round_trip, dynamic_tp = _fee_tier(self._volume_usd)
        return {
            "positions":     positions,
            "recent_trades": recent,
            "total_trades":  total,
            "wins":          wins,
            "losses":        losses,
            "win_rate":      round(wins / total * 100, 1) if total else 0,
            "total_pnl_eur": round(sum(t.get("pnl_eur", 0) for t in self._trade_log), 4),
            "volume_usd":    round(self._volume_usd, 2),
            "taker_fee_pct": taker,
            "round_trip_pct": round_trip,
            "dynamic_tp_pct": dynamic_tp,
            "pair_scores":   pair_scores,
        }

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                self._check_exits()
                self._scan_entries()
            except Exception as exc:
                logger.error("[SCALP] Loop error: %s", exc, exc_info=True)
            time.sleep(_INTERVAL_SEC)

    # ── Exit logic ────────────────────────────────────────────────────────────

    def _check_exits(self):
        with self._lock:
            open_pairs = list(self._positions.keys())

        for pair in open_pairs:
            price = self._get_price(pair)
            if price is None or price <= 0:
                continue
            with self._lock:
                pos = self._positions.get(pair)
            if not pos:
                continue

            entry      = pos["entry"]
            pct_change = (price - entry) / entry * 100
            held_min   = (time.time() - pos["ts"]) / 60
            _, _, tp   = _fee_tier(self._volume_usd)  # dynamic TP based on current fee tier

            if pct_change >= tp:
                self._close_position(pair, price, "TAKE_PROFIT", pct_change)
            elif pct_change <= -_SL_PCT:
                self._close_position(pair, price, "STOP_LOSS", pct_change)
            elif held_min >= _MAX_HOLD_MIN:
                self._close_position(pair, price, "TIMEOUT", pct_change)

    # ── Entry logic ───────────────────────────────────────────────────────────

    def _is_bear_market(self) -> bool:
        """Returns True if BTC 1h trend is bearish — skip new longs in downtrend."""
        try:
            ohlc = self._api.get_ohlc_data("XBTEUR", interval=60)
            if not ohlc:
                return False
            key = next((k for k in ohlc if k != "last"), None)
            if not key:
                return False
            closes = [float(r[4]) for r in ohlc[key][-20:]]
            if len(closes) < 10:
                return False
            # Bear if 10-period EMA is falling
            ema = closes[0]
            for c in closes[1:]:
                ema = ema * 0.8 + c * 0.2
            return closes[-1] < closes[-5]  # price lower than 5 candles ago
        except Exception:
            return False

    def _scan_entries(self):
        bear = self._is_bear_market()
        if bear:
            logger.debug("[SCALP] Bear market — scoring pairs for display but skipping entries")
        for pair in _PAIRS:
            with self._lock:
                has_position = pair in self._positions
            if has_position:
                continue

            score = self._score_pair(pair)
            if score is None:
                continue

            with self._lock:
                self._pair_scores[pair] = score

            if bear:
                continue  # scores tracked for dashboard, no new longs in downtrend

            price = self._get_price(pair)
            if price is None or price <= 0:
                continue

            if score >= _SCORE_THRESH:
                self._open_position(pair, price, score)
            elif score <= -_SCORE_THRESH:
                logger.debug("[SCALP] %s SELL signal (score=%.1f) — shorts disabled", pair, score)

    def _score_pair(self, pair: str) -> Optional[float]:
        try:
            ohlc = self._api.get_ohlc_data(pair, interval=1)
            if not ohlc:
                return None
            key = next((k for k in ohlc if k != "last"), None)
            if not key:
                return None
            candles = ohlc[key]
            if len(candles) < _RSI_PERIOD + 2:
                return None

            closes = [float(r[4]) for r in candles]
            rsi    = _calc_rsi(closes, _RSI_PERIOD)
            vwap   = _calc_vwap(candles[-_VWAP_CANDLES:])
            price  = closes[-1]
            score  = 0.0

            if rsi is not None:
                if rsi < _RSI_BUY:
                    score += 2
                elif rsi > _RSI_SELL:
                    score -= 2

            if vwap and vwap > 0:
                dev = (price - vwap) / vwap
                if dev < -_VWAP_THRESH:
                    score += 1
                elif dev > _VWAP_THRESH:
                    score -= 1

            ob = self._api.get_order_book(pair, count=10)
            if ob:
                book    = next(iter(ob.values()), {}) if isinstance(ob, dict) else {}
                bids    = book.get("bids", [])
                asks    = book.get("asks", [])
                bid_vol = sum(float(b[1]) for b in bids if len(b) >= 2)
                ask_vol = sum(float(a[1]) for a in asks if len(a) >= 2)
                total   = bid_vol + ask_vol
                if total > 0:
                    imbalance = (bid_vol - ask_vol) / total
                    if imbalance > _OB_IMBALANCE:
                        score += 1
                    elif imbalance < -_OB_IMBALANCE:
                        score -= 1

            logger.debug(
                "[SCALP] %s | score=%.1f | rsi=%.1f | vwap=%.4f | price=%.4f",
                pair, score, rsi or 0.0, vwap or 0.0, price,
            )
            return score

        except Exception as exc:
            logger.warning("[SCALP] Score error for %s: %s", pair, exc)
            return None

    # ── Order execution ───────────────────────────────────────────────────────

    def _open_position(self, pair: str, price: float, score: float):
        qty = round(_ALLOCATION_EUR / price, 8)
        ts  = time.time()
        with self._lock:
            self._positions[pair] = {
                "qty":   qty,
                "entry": price,
                "ts":    ts,
                "score": score,
            }
        self._save_positions()
        # Deduct allocation from paper balance
        try:
            self._api.adjust_paper_balance(-_ALLOCATION_EUR)
        except Exception:
            pass
        logger.info(
            "[SCALP] BUY  %s @ %.6f  qty=%.8f  score=%.1f  (paper)",
            pair, price, qty, score,
        )

    def _close_position(self, pair: str, price: float, reason: str, pct: float):
        with self._lock:
            pos = self._positions.pop(pair, None)
        if not pos:
            return

        pnl_eur  = (price - pos["entry"]) * pos["qty"]
        held_min = (time.time() - pos["ts"]) / 60
        trade = {
            "ts":       datetime.now(timezone.utc).isoformat(),
            "pair":     pair,
            "entry":    round(pos["entry"], 6),
            "exit":     round(price, 6),
            "qty":      pos["qty"],
            "pnl_eur":  round(pnl_eur, 4),
            "pnl_pct":  round(pct, 3),
            "reason":   reason,
            "held_min": round(held_min, 1),
        }
        # Return allocation + P&L to paper balance (allocation was deducted on buy)
        try:
            self._api.adjust_paper_balance(_ALLOCATION_EUR + pnl_eur)
        except Exception:
            pass

        # Accumulate trade volume (EUR → approximate USD, update _EUR_USD_APPROX if rate drifts)
        trade_value_usd = price * pos["qty"] * 1.10
        self._volume_usd += trade_value_usd
        self._save_volume()

        _, _, current_tp = _fee_tier(self._volume_usd)
        with self._lock:
            self._trade_log.append(trade)
            if len(self._trade_log) > 100:
                self._trade_log = self._trade_log[-100:]

        self._save_positions()
        self._log_trade(trade)
        self._persist_trade(trade)
        logger.info(
            "[SCALP] SELL %s @ %.6f  pnl=%.4f EUR (%.3f%%)  reason=%s  held=%.1fm  (paper)",
            pair, price, pnl_eur, pct, reason, held_min,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_price(self, pair: str) -> Optional[float]:
        # Prefer WebSocket (zero latency, no REST quota)
        if self._ws:
            p = self._ws.get_price(pair)
            if p:
                return float(p)
        # Fall back to REST ticker
        try:
            data = self._api.get_market_data(pair)
            if data:
                key = next(iter(data), None)
                if key:
                    return float(data[key]["c"][0])
        except Exception:
            pass
        return None

    def _load_positions(self):
        try:
            if self._pos_path.exists():
                self._positions = json.loads(self._pos_path.read_text())
                logger.info("[SCALP] Loaded %d open position(s) from disk", len(self._positions))
        except Exception as exc:
            logger.warning("[SCALP] Could not load positions: %s", exc)
            self._positions = {}

    def _save_positions(self):
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._pos_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._positions, separators=(",", ":")))
            tmp.replace(self._pos_path)
        except Exception as exc:
            logger.warning("[SCALP] Could not save positions: %s", exc)

    def _log_trade(self, trade: dict):
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with open(self._trades_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(trade) + "\n")
        except Exception as exc:
            logger.warning("[SCALP] Could not log trade: %s", exc)

    def _load_volume(self):
        try:
            vol_path = self._data_dir / "scalper_volume.json"
            if vol_path.exists():
                self._volume_usd = float(json.loads(vol_path.read_text()).get("volume_usd", 0))
                logger.info("[SCALP] Loaded cumulative volume: $%.2f", self._volume_usd)
        except Exception as exc:
            logger.warning("[SCALP] Could not load volume: %s", exc)

    def _save_volume(self):
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            vol_path = self._data_dir / "scalper_volume.json"
            vol_path.write_text(json.dumps({"volume_usd": round(self._volume_usd, 2)}))
        except Exception as exc:
            logger.warning("[SCALP] Could not save volume: %s", exc)

    def _persist_trade(self, trade: dict):
        try:
            from core.db_postgres import save_scalper_trade as _pg_save
        except ImportError:
            try:
                from db_postgres import save_scalper_trade as _pg_save
            except ImportError:
                return
        try:
            _pg_save(
                pair        = trade["pair"],
                entry_price = trade["entry"],
                exit_price  = trade["exit"],
                qty         = trade["qty"],
                pnl_eur     = trade["pnl_eur"],
                pnl_pct     = trade["pnl_pct"],
                reason      = trade["reason"],
                held_min    = trade["held_min"],
            )
        except Exception as exc:
            logger.warning("[SCALP] Could not persist trade to PostgreSQL: %s", exc)

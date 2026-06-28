"""Scalping strategy — daemon thread alongside the main bot.

Dynamic mode: discovers all EUR pairs on Kraken every 5 minutes, filters to
the top 40 by 24h volume (≥€50k), then scores them every 15 seconds using
RSI/VWAP/order-book signals.  The highest-scoring pair above the threshold is
traded rather than the first one found.

Signals (scored, threshold ±2.5 to enter):
  1-min RSI < 28 → +2  (oversold, buy)
  1-min RSI > 72 → -2  (overbought, sell)
  Price < VWAP by >0.3% → +1  (below fair value, buy)
  Price > VWAP by >0.3% → -1  (above fair value, sell)
  Order book bid vol > ask vol by >20% → +1  (buying pressure)
  Order book ask vol > bid vol by >20% → -1  (selling pressure)

TP: 0.58% dynamic / SL: 0.20%  (clears 0.52% round-trip Kraken taker fee)
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Fallback list used only until the first screener run completes
_PAIRS_FALLBACK = [
    "XBTEUR", "XETHZEUR", "SOLEUR", "XXRPZEUR", "LINKEUR", "AVAXEUR",
    "ADAEUR", "DOTEUR", "ATOMEUR", "UNIEUR",
    "LTCEUR", "BCHEUR", "TRXEUR", "XMREUR", "AAVEEUR", "NEAREUR",
    "ALGOEUR", "ETCEUR", "SHIBEUR", "ZECEUR",
    "MKREUR", "SNXEUR", "OPEUR", "ARBEUR", "SANDEUR",
    "MANAUER", "INJEUR", "FTMEUR", "GALEUR", "APEEUR",
]

# Screener settings
_SCREENER_INTERVAL_SEC = 300   # re-discover pairs every 5 minutes
_MAX_ACTIVE_PAIRS      = 40    # keep top-N by 24h EUR volume
_MIN_VOL_EUR_24H       = 50_000  # discard pairs below this daily EUR turnover
_SCREENER_CHUNK        = 50    # pairs per Ticker batch call

# Keywords in altname that flag a stablecoin or non-spot instrument to exclude
_EXCLUDE_KEYWORDS = ("USD", "USDT", "USDC", "DAI", "BUSD", "TUSD", "FRAX",
                     "LUSD", "GUSD", "PYUSD", "EURT", "STEUR", "EURR", "PAX")

_INTERVAL_SEC   = 15
_RSI_PERIOD     = 14
_RSI_BUY        = 28.0     # tightened: only buy extremely oversold
_RSI_SELL       = 72.0     # tightened: only sell extremely overbought
_VWAP_CANDLES   = 30       # rolling window for VWAP calc
_VWAP_THRESH    = 0.003    # 0.3% deviation from VWAP to signal
_OB_IMBALANCE   = 0.20     # 20% bid/ask vol imbalance to signal
_SCORE_THRESH   = 2.5      # raised: require stronger combined signal
_TP_PCT         = 0.86     # take-profit % (base — adjusted dynamically by fee tier)
_SL_PCT         = 0.20     # stop-loss %
_ALLOCATION_EUR = 10.0     # paper EUR per scalp trade
_MAX_HOLD_MIN   = 60       # force-exit after 60 minutes regardless
_MIN_PROFIT_BPS = 0.06     # minimum net profit above round-trip fee (6 basis points)
_AI_REVIEW_EVERY = 25      # trigger AI param review after this many closed trades

# Hard bounds — AI suggestions are clamped to these before applying
_AI_BOUNDS = {
    "rsi_buy":      (25.0, 40.0),
    "rsi_sell":     (60.0, 75.0),
    "vwap_thresh":  (0.001, 0.006),
    "score_thresh": (1.5, 3.0),
    "sl_pct":       (0.15, 0.35),
}

# Kraken fee tiers: (30-day USD volume threshold, taker fee %)
# Source: Kraken AssetPairs API verified 2026-06-28. Fee volume currency: ZUSD.
# TP is set dynamically to round_trip_fee + _MIN_PROFIT_BPS
_FEE_TIERS = [
    (0,           0.40),  # <$10k       → round trip 0.80%  → TP 0.86%
    (10_000,      0.35),  # $10k+       → round trip 0.70%  → TP 0.76%
    (50_000,      0.24),  # $50k+       → round trip 0.48%  → TP 0.54%
    (100_000,     0.22),  # $100k+      → round trip 0.44%  → TP 0.50%
    (250_000,     0.20),  # $250k+      → round trip 0.40%  → TP 0.46%
    (500_000,     0.18),  # $500k+      → round trip 0.36%  → TP 0.42%
    (1_000_000,   0.16),  # $1M+        → round trip 0.32%  → TP 0.38%
    (2_500_000,   0.14),  # $2.5M+      → round trip 0.28%  → TP 0.34%
    (5_000_000,   0.12),  # $5M+        → round trip 0.24%  → TP 0.30%
    (10_000_000,  0.10),  # $10M+       → round trip 0.20%  → TP 0.26%
    (100_000_000, 0.08),  # $100M+      → round trip 0.16%  → TP 0.22%
]


def _fee_tier(volume_usd: float) -> tuple:
    """Return (taker_fee_pct, round_trip_pct, dynamic_tp_pct) for given 30-day volume."""
    taker = 0.40
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
        self._positions: dict = {}     # pair → {qty, entry, ts, score, rsi, vwap_dev, ob_imbalance}
        self._trade_log: list = []     # last 100 completed trades (in-memory)
        self._volume_usd: float = 0.0  # cumulative 30-day equivalent volume (USD)
        self._pair_scores: dict = {}   # pair → latest score (+ bullish, - bearish)
        self._trades_since_ai: int = 0  # counter — triggers AI review every N trades

        # Dynamic pair discovery
        self._active_pairs: list = []  # updated by screener every 5 min
        self._screener_ts: float = 0.0  # epoch of last screener run

        # Live AI-tuned params (start at defaults, overwritten by scalper_ai_params.json)
        self._ai_rsi_buy     = _RSI_BUY
        self._ai_rsi_sell    = _RSI_SELL
        self._ai_vwap_thresh = _VWAP_THRESH
        self._ai_score_thresh= _SCORE_THRESH
        self._ai_sl_pct      = _SL_PCT
        self._ai_blacklist: set = set()

        # Persistent paths
        self._pos_path      = self._data_dir / "scalper_positions.json"
        self._trades_path   = self._data_dir / "scalper_trades.jsonl"
        self._ai_params_path= self._data_dir / "scalper_ai_params.json"

        self._load_positions()
        self._load_volume()
        self._load_ai_params()
        _completed = self._count_completed_trades()
        self._trades_since_ai = _completed % _AI_REVIEW_EVERY
        # If trades exist but AI has never run successfully, trigger on the next trade
        _adj_path = self._data_dir / "scalper_ai_adjustments.jsonl"
        _last_succeeded = False
        if _adj_path.exists():
            try:
                last_line = _adj_path.read_text(encoding="utf-8").strip().split("\n")[-1]
                _last_succeeded = json.loads(last_line).get("success", True)
            except Exception:
                pass
        if _completed >= _AI_REVIEW_EVERY and not _last_succeeded:
            self._trades_since_ai = _AI_REVIEW_EVERY - 1
            logger.info("[SCALP-AI] Last AI review failed — will retry on next trade close")

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
            "[SCALP] Engine started (dynamic) | screener=%ds | top=%d pairs | "
            "min_vol=€%.0f/day | TP=%.1f%% | SL=%.1f%% | alloc=€%.0f",
            _SCREENER_INTERVAL_SEC, _MAX_ACTIVE_PAIRS, _MIN_VOL_EUR_24H,
            _TP_PCT, _SL_PCT, _ALLOCATION_EUR,
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
            "positions":        positions,
            "recent_trades":    recent,
            "total_trades":     total,
            "wins":             wins,
            "losses":           losses,
            "win_rate":         round(wins / total * 100, 1) if total else 0,
            "total_pnl_eur":    round(sum(t.get("pnl_eur", 0) for t in self._trade_log), 4),
            "volume_usd":       round(self._volume_usd, 2),
            "taker_fee_pct":    taker,
            "round_trip_pct":   round_trip,
            "dynamic_tp_pct":   dynamic_tp,
            "pair_scores":      pair_scores,
            "active_pairs":     list(self._active_pairs),
            "active_pairs_count": len(self._active_pairs) if self._active_pairs else len(_PAIRS_FALLBACK),
            "screener_last_ts": round(self._screener_ts),
            "ai_params": {
                "rsi_buy":      self._ai_rsi_buy,
                "rsi_sell":     self._ai_rsi_sell,
                "vwap_thresh":  self._ai_vwap_thresh,
                "score_thresh": self._ai_score_thresh,
                "sl_pct":       self._ai_sl_pct,
                "blacklist":    list(self._ai_blacklist),
            },
        }

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                self._refresh_active_pairs()
                self._check_exits()
                self._scan_entries()
            except Exception as exc:
                logger.error("[SCALP] Loop error: %s", exc, exc_info=True)
            time.sleep(_INTERVAL_SEC)

    # ── Dynamic pair discovery ────────────────────────────────────────────────

    def _refresh_active_pairs(self):
        """Run the screener if the interval has elapsed; no-op otherwise."""
        if time.time() - self._screener_ts < _SCREENER_INTERVAL_SEC:
            return
        self._screener_ts = time.time()
        discovered = self._discover_pairs()
        if discovered:
            self._active_pairs = discovered

    def _discover_pairs(self) -> list:
        """Fetch all Kraken EUR pairs, filter by volume, return top N altnames."""
        try:
            all_pairs = self._api.get_asset_pairs()
            if not all_pairs:
                logger.warning("[SCALP] Screener: AssetPairs empty — keeping existing list")
                return self._active_pairs or list(_PAIRS_FALLBACK)

            # Build altname → official_key for online EUR pairs only
            eur_map: dict = {}
            for official_key, info in all_pairs.items():
                if official_key.endswith(".d"):
                    continue
                if info.get("status") != "online":
                    continue
                if info.get("quote") not in ("ZEUR", "EUR"):
                    continue
                altname = info.get("altname", official_key)
                if any(kw in altname.upper() for kw in _EXCLUDE_KEYWORDS):
                    continue
                eur_map[altname] = official_key

            if not eur_map:
                return self._active_pairs or list(_PAIRS_FALLBACK)

            # Batch-fetch Ticker to rank by 24h volume
            # Response keys are official names; build reverse lookup
            rev = {v: k for k, v in eur_map.items()}
            altnames = list(eur_map.keys())
            volumes: dict = {}  # altname → EUR 24h volume

            for i in range(0, len(altnames), _SCREENER_CHUNK):
                chunk = altnames[i : i + _SCREENER_CHUNK]
                ticker = self._api.get_ticker_batch(chunk)
                if not ticker:
                    continue
                for resp_key, tick in ticker.items():
                    altname = rev.get(resp_key) or resp_key
                    try:
                        vol_base = float(tick["v"][1])   # 24h rolling volume
                        price    = float(tick["c"][0])   # last trade price
                        volumes[altname] = vol_base * price
                    except (KeyError, IndexError, ValueError):
                        pass

            qualified = [(a, v) for a, v in volumes.items() if v >= _MIN_VOL_EUR_24H]
            qualified.sort(key=lambda x: x[1], reverse=True)
            top_pairs = [a for a, _ in qualified[:_MAX_ACTIVE_PAIRS]]

            if not top_pairs:
                logger.warning("[SCALP] Screener: no pairs met €%.0f vol filter — using fallback", _MIN_VOL_EUR_24H)
                return list(_PAIRS_FALLBACK)

            logger.info(
                "[SCALP] Screener: %d EUR pairs → %d qualify (≥€%.0f/day) → top %d selected",
                len(eur_map), len(qualified), _MIN_VOL_EUR_24H, len(top_pairs),
            )
            try:
                path = self._data_dir / "scalper_active_pairs.json"
                path.write_text(json.dumps({
                    "pairs": top_pairs,
                    "ts": time.time(),
                    "total_eur_pairs": len(eur_map),
                    "qualifying": len(qualified),
                }, separators=(",", ":")))
            except Exception:
                pass
            return top_pairs

        except Exception as exc:
            logger.warning("[SCALP] Screener error: %s", exc)
            return self._active_pairs or list(_PAIRS_FALLBACK)

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
            _, _, tp   = _fee_tier(self._volume_usd)
            with self._lock:
                sl = self._ai_sl_pct

            if pct_change >= tp:
                self._close_position(pair, price, "TAKE_PROFIT", pct_change,
                                     self._get_exit_signals(pair))
            elif pct_change <= -sl:
                self._close_position(pair, price, "STOP_LOSS", pct_change,
                                     self._get_exit_signals(pair))
            elif held_min >= _MAX_HOLD_MIN:
                self._close_position(pair, price, "TIMEOUT", pct_change,
                                     self._get_exit_signals(pair))

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

        active_pairs = self._active_pairs or list(_PAIRS_FALLBACK)

        with self._lock:
            thresh = self._ai_score_thresh

        best_pair    = None
        best_score   = 0.0
        best_price   = 0.0
        best_signals: dict = {}

        for pair in active_pairs:
            with self._lock:
                has_position = pair in self._positions
                blacklisted  = pair in self._ai_blacklist
            if has_position or blacklisted:
                continue

            result = self._score_pair(pair)
            if result is None:
                continue
            score, signals = result

            with self._lock:
                self._pair_scores[pair] = score

            if bear or score < thresh:
                continue

            if score > best_score:
                price = self._get_price(pair)
                if price and price > 0:
                    best_pair    = pair
                    best_score   = score
                    best_price   = price
                    best_signals = signals

        if best_pair:
            self._open_position(best_pair, best_price, best_score, best_signals, bear)

        # Prune scores for pairs that are no longer in the active set
        active_set = set(active_pairs)
        with self._lock:
            stale = [p for p in list(self._pair_scores) if p not in active_set]
            for p in stale:
                del self._pair_scores[p]

    def _score_pair(self, pair: str) -> Optional[tuple]:
        """Return (score, signals_dict) or None. Uses live AI-tuned thresholds."""
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

            with self._lock:
                rsi_buy      = self._ai_rsi_buy
                rsi_sell     = self._ai_rsi_sell
                vwap_thresh  = self._ai_vwap_thresh

            vwap_dev     = 0.0
            ob_imbalance = 0.0

            if rsi is not None:
                if rsi < rsi_buy:
                    score += 2
                elif rsi > rsi_sell:
                    score -= 2

            if vwap and vwap > 0:
                vwap_dev = (price - vwap) / vwap
                if vwap_dev < -vwap_thresh:
                    score += 1
                elif vwap_dev > vwap_thresh:
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
                    ob_imbalance = (bid_vol - ask_vol) / total
                    if ob_imbalance > _OB_IMBALANCE:
                        score += 1
                    elif ob_imbalance < -_OB_IMBALANCE:
                        score -= 1

            signals = {
                "rsi":          round(rsi, 2) if rsi is not None else None,
                "vwap_dev":     round(vwap_dev * 100, 4),   # as percentage
                "ob_imbalance": round(ob_imbalance, 4),
            }
            logger.debug(
                "[SCALP] %s | score=%.1f | rsi=%.1f | vwap_dev=%.3f%% | ob=%.3f",
                pair, score, rsi or 0.0, vwap_dev * 100, ob_imbalance,
            )
            return score, signals

        except Exception as exc:
            logger.warning("[SCALP] Score error for %s: %s", pair, exc)
            return None

    # ── Order execution ───────────────────────────────────────────────────────

    def _open_position(self, pair: str, price: float, score: float,
                       signals: dict, bear: bool = False):
        qty    = round(_ALLOCATION_EUR / price, 8)
        ts     = time.time()
        open_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        with self._lock:
            concurrent = len(self._positions)  # other open positions before adding this one
            self._positions[pair] = {
                "qty":              qty,
                "entry":            price,
                "ts":               ts,
                "score":            score,
                "rsi":              signals.get("rsi"),
                "vwap_dev":         signals.get("vwap_dev"),
                "ob_imbalance":     signals.get("ob_imbalance"),
                "entry_hour_utc":   open_dt.hour,
                "entry_weekday":    open_dt.weekday(),   # 0=Mon, 6=Sun
                "concurrent_pos":   concurrent,
                "btc_bear":         bear,
                "active_rsi_buy":   self._ai_rsi_buy,
                "active_rsi_sell":  self._ai_rsi_sell,
                "active_score_thresh": self._ai_score_thresh,
                "active_vwap_thresh":  self._ai_vwap_thresh,
            }
        self._save_positions()
        try:
            self._api.adjust_paper_balance(-_ALLOCATION_EUR)
        except Exception:
            pass
        logger.info(
            "[SCALP] BUY  %s @ %.6f  qty=%.8f  score=%.1f  rsi=%.1f  vwap_dev=%.3f%%  (paper)",
            pair, price, qty, score,
            signals.get("rsi") or 0.0, signals.get("vwap_dev") or 0.0,
        )

    def _get_exit_signals(self, pair: str) -> dict:
        """Fetch RSI, VWAP deviation and order book imbalance at the moment of exit."""
        try:
            ohlc = self._api.get_ohlc_data(pair, interval=1)
            if not ohlc:
                return {}
            key = next((k for k in ohlc if k != "last"), None)
            if not key:
                return {}
            candles = ohlc[key]
            if len(candles) < _RSI_PERIOD + 2:
                return {}
            closes   = [float(r[4]) for r in candles]
            rsi      = _calc_rsi(closes, _RSI_PERIOD)
            vwap     = _calc_vwap(candles[-_VWAP_CANDLES:])
            price    = closes[-1]
            vwap_dev = round((price - vwap) / vwap * 100, 4) if vwap and vwap > 0 else None

            ob_imbalance = None
            ob = self._api.get_order_book(pair, count=10)
            if ob:
                book    = next(iter(ob.values()), {}) if isinstance(ob, dict) else {}
                bids    = book.get("bids", [])
                asks    = book.get("asks", [])
                bid_vol = sum(float(b[1]) for b in bids if len(b) >= 2)
                ask_vol = sum(float(a[1]) for a in asks if len(a) >= 2)
                total   = bid_vol + ask_vol
                if total > 0:
                    ob_imbalance = round((bid_vol - ask_vol) / total, 4)

            return {
                "exit_rsi":          round(rsi, 2) if rsi is not None else None,
                "exit_vwap_dev":     vwap_dev,
                "exit_ob_imbalance": ob_imbalance,
            }
        except Exception:
            return {}

    def _close_position(self, pair: str, price: float, reason: str, pct: float,
                        exit_signals: Optional[dict] = None):
        with self._lock:
            pos = self._positions.pop(pair, None)
        if not pos:
            return

        pnl_eur  = (price - pos["entry"]) * pos["qty"]
        held_min = (time.time() - pos["ts"]) / 60
        sig      = exit_signals or {}
        trade = {
            # ── Timestamps ───────────────────────────────────────────────────
            "open_ts":        datetime.fromtimestamp(pos["ts"], tz=timezone.utc).isoformat(),
            "ts":             datetime.now(timezone.utc).isoformat(),
            # ── Trade basics ─────────────────────────────────────────────────
            "pair":           pair,
            "entry":          round(pos["entry"], 6),
            "exit":           round(price, 6),
            "qty":            pos["qty"],
            "pnl_eur":        round(pnl_eur, 4),
            "pnl_pct":        round(pct, 3),
            "reason":         reason,
            "held_min":       round(held_min, 1),
            # ── Entry signals ─────────────────────────────────────────────────
            "entry_score":          round(pos.get("score", 0), 2),
            "entry_rsi":            pos.get("rsi"),
            "entry_vwap_dev":       pos.get("vwap_dev"),
            "entry_ob_imbalance":   pos.get("ob_imbalance"),
            # ── Exit signals ─────────────────────────────────────────────────
            "exit_rsi":             sig.get("exit_rsi"),
            "exit_vwap_dev":        sig.get("exit_vwap_dev"),
            "exit_ob_imbalance":    sig.get("exit_ob_imbalance"),
            # ── Context at entry ─────────────────────────────────────────────
            "entry_hour_utc":       pos.get("entry_hour_utc"),
            "entry_weekday":        pos.get("entry_weekday"),   # 0=Mon, 6=Sun
            "concurrent_positions": pos.get("concurrent_pos"),
            "btc_bear_at_entry":    pos.get("btc_bear"),
            # ── Active AI params when trade was taken ─────────────────────────
            "param_rsi_buy":        pos.get("active_rsi_buy"),
            "param_rsi_sell":       pos.get("active_rsi_sell"),
            "param_score_thresh":   pos.get("active_score_thresh"),
            "param_vwap_thresh":    pos.get("active_vwap_thresh"),
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
            self._trades_since_ai += 1
            trigger_ai = (self._trades_since_ai >= _AI_REVIEW_EVERY)
            if trigger_ai:
                self._trades_since_ai = 0

        self._save_positions()
        self._log_trade(trade)
        self._persist_trade(trade)

        if trigger_ai:
            self._run_ai_review()
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

    def _load_ai_params(self):
        """Read AI-suggested params from disk and apply within hard bounds."""
        try:
            if not self._ai_params_path.exists():
                return
            p = json.loads(self._ai_params_path.read_text())
            lo, hi = _AI_BOUNDS["rsi_buy"]
            self._ai_rsi_buy     = max(lo, min(hi, float(p.get("rsi_buy",     _RSI_BUY))))
            lo, hi = _AI_BOUNDS["rsi_sell"]
            self._ai_rsi_sell    = max(lo, min(hi, float(p.get("rsi_sell",    _RSI_SELL))))
            lo, hi = _AI_BOUNDS["vwap_thresh"]
            self._ai_vwap_thresh = max(lo, min(hi, float(p.get("vwap_thresh", _VWAP_THRESH))))
            lo, hi = _AI_BOUNDS["score_thresh"]
            self._ai_score_thresh= max(lo, min(hi, float(p.get("score_thresh",_SCORE_THRESH))))
            lo, hi = _AI_BOUNDS["sl_pct"]
            self._ai_sl_pct      = max(lo, min(hi, float(p.get("sl_pct",      _SL_PCT))))
            bl = p.get("pairs_blacklist", [])
            self._ai_blacklist   = set(bl) if isinstance(bl, list) else set()
            logger.info(
                "[SCALP-AI] Params loaded — RSI_BUY=%.0f RSI_SELL=%.0f "
                "VWAP=%.3f SCORE=%.1f SL=%.2f%% blacklist=%s",
                self._ai_rsi_buy, self._ai_rsi_sell, self._ai_vwap_thresh,
                self._ai_score_thresh, self._ai_sl_pct, list(self._ai_blacklist),
            )
        except Exception as exc:
            logger.warning("[SCALP-AI] Could not load AI params: %s", exc)

    def _count_completed_trades(self) -> int:
        """Count completed trades in the JSONL log so the AI counter survives restarts."""
        try:
            if not self._trades_path.exists():
                return 0
            count = 0
            with open(self._trades_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        count += 1
            logger.info("[SCALP-AI] %d completed trades found — counter starts at %d/%d",
                        count, count % _AI_REVIEW_EVERY, _AI_REVIEW_EVERY)
            return count
        except Exception as exc:
            logger.warning("[SCALP-AI] Could not count completed trades: %s", exc)
            return 0

    def _run_ai_review(self):
        """Spawn a background thread to run AI analysis (non-blocking)."""
        def _worker():
            try:
                try:
                    from core.scalper_ai import ScalperAI
                except ImportError:
                    from scalper_ai import ScalperAI
                ai = ScalperAI(data_dir=str(self._data_dir))
                ai.analyze()
                self._load_ai_params()  # always reload — covers both proposals and reverts
            except Exception as exc:
                logger.warning("[SCALP-AI] Review thread error: %s", exc)

        threading.Thread(target=_worker, daemon=True, name="ScalperAI").start()
        logger.info("[SCALP-AI] AI review triggered after %d trades", _AI_REVIEW_EVERY)

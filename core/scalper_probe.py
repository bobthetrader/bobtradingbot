"""Signal-excursion observer — measures how far price moves after a scalp signal.

Pure measurement: NO positions, NO paper balance, NO fees. Whenever a scalp
signal fires (score >= observe threshold), it records the entry and, after a
fixed observation window, reconstructs the 1-min price path to compute:
  - MFE (max favorable excursion %) and minutes to it
  - MAE (max adverse excursion %) and minutes to it
  - the ordered per-minute [high, low, close] path (for offline TP/SL first-touch
    simulation — the geometry backtest the closed-trade log can't give us)

It runs alongside the (possibly paused) trader to gather the data needed to
design fee-robust exit geometry. It decouples the two unknowns: does the signal
have directional follow-through (MFE vs MAE), and — separately — where the
net-EV-optimal TP/SL sit. Output: data/scalper_observations.jsonl

Reuses the scalper's screener + scoring by composition: it holds an *unstarted*
ScalperEngine purely as a scoring/screening library (its trade loop never runs,
so it opens no positions and touches no balance). Because it never calls
engine.start(), the only file it shares with the real scalper is the screener's
scalper_active_pairs.json (harmless — same data; the trader is paused anyway).
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_SCAN_INTERVAL_SEC = 60     # score the active set this often
_OBSERVE_SCORE_MIN = 4.0    # a "signal" = score >= this (matches scalper entry default)
_WINDOW_MIN        = 180    # track price for this long after a signal
_COOLDOWN_MIN      = 180    # don't re-observe the same pair within this many minutes


class ScalperProbe:
    """Observe scalp signals and record their forward price excursions."""

    def __init__(self, kraken_api, ws_feed=None, data_dir: str = "data"):
        self._api      = kraken_api
        self._ws       = ws_feed
        self._data_dir = Path(data_dir)
        self._running  = False
        self._thread   = None
        self._pending: list = []       # observations awaiting maturity
        self._last_obs_ts: dict = {}   # pair -> epoch of last observation (cooldown)
        self._obs_path = self._data_dir / "scalper_observations.jsonl"

        # Borrow the scalper's screener + scoring WITHOUT starting its trade loop.
        try:
            from core.scalper import ScalperEngine
        except ImportError:
            from scalper import ScalperEngine
        self._engine = ScalperEngine(
            kraken_api=kraken_api, paper_mode=True,
            data_dir=str(data_dir), ws_feed=ws_feed,
        )

    # ── Lifecycle ───────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="ScalperProbe"
        )
        self._thread.start()
        logger.info(
            "[PROBE] Signal-excursion observer started | scan=%ds | window=%dm | "
            "score>=%.1f | cooldown=%dm (measurement only — no trades)",
            _SCAN_INTERVAL_SEC, _WINDOW_MIN, _OBSERVE_SCORE_MIN, _COOLDOWN_MIN,
        )

    def stop(self):
        self._running = False

    def get_status(self) -> dict:
        """Snapshot for the dashboard/status writer."""
        return {
            "pending":       len(self._pending),
            "observed_pairs": len(self._last_obs_ts),
            "window_min":    _WINDOW_MIN,
            "score_min":     _OBSERVE_SCORE_MIN,
        }

    # ── Main loop ───────────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                self._engine._refresh_active_pairs()
                self._scan()
                self._finalize_matured()
            except Exception as exc:
                logger.error("[PROBE] loop error: %s", exc, exc_info=True)
            time.sleep(_SCAN_INTERVAL_SEC)

    def _scan(self):
        """Score the active set; open an observation for each fresh qualifying signal."""
        pairs = self._engine._active_pairs or []
        now   = time.time()
        pending_pairs = {o["pair"] for o in self._pending}

        for pair in pairs:
            if pair in pending_pairs:
                continue
            if now - self._last_obs_ts.get(pair, 0) < _COOLDOWN_MIN * 60:
                continue

            result = self._engine._score_pair(pair)
            if not result:
                continue
            score, signals = result
            if score < _OBSERVE_SCORE_MIN:
                continue

            price = self._engine._get_price(pair)
            if not price or price <= 0:
                continue

            self._pending.append({
                "pair":    pair,
                "entry":   float(price),
                "ts":      now,
                "score":   score,
                "signals": signals,
            })
            self._last_obs_ts[pair] = now
            logger.info("[PROBE] observing %s @ %.6f  score=%.1f", pair, price, score)

    def _finalize_matured(self):
        """Write out observations whose window has elapsed."""
        now   = time.time()
        still = []
        for o in self._pending:
            if now - o["ts"] < _WINDOW_MIN * 60:
                still.append(o)
                continue
            self._write_observation(o)
        self._pending = still

    # ── Observation output ──────────────────────────────────────────────────────

    def _write_observation(self, o: dict):
        path  = self._fetch_path(o["pair"], o["ts"], o["ts"] + _WINDOW_MIN * 60)
        entry = o["entry"]

        mfe = mae = 0.0
        mfe_min = mae_min = 0.0
        bars: list = []
        for bts, hi, lo, close in path:
            up = (hi - entry) / entry * 100
            dn = (lo - entry) / entry * 100
            if up > mfe:
                mfe, mfe_min = up, round((bts - o["ts"]) / 60, 1)
            if dn < mae:
                mae, mae_min = dn, round((bts - o["ts"]) / 60, 1)
            bars.append([round(hi, 8), round(lo, 8), round(close, 8)])

        rec = {
            "ts":            datetime.fromtimestamp(o["ts"], tz=timezone.utc).isoformat(),
            "pair":          o["pair"],
            "entry":         round(entry, 8),
            "entry_score":   o["score"],
            "entry_signals": o["signals"],
            "window_min":    _WINDOW_MIN,
            "mfe_pct":       round(mfe, 4),
            "mfe_min":       mfe_min,
            "mae_pct":       round(mae, 4),
            "mae_min":       mae_min,
            "n_bars":        len(bars),
            "path":          bars,   # ordered [high, low, close] per minute from entry
        }
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with open(self._obs_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
            logger.info(
                "[PROBE] %s done | MFE=+%.2f%%@%.0fm  MAE=%.2f%%@%.0fm  bars=%d",
                o["pair"], mfe, mfe_min, mae, mae_min, len(bars),
            )
        except Exception as exc:
            logger.warning("[PROBE] write failed for %s: %s", o["pair"], exc)

    def _fetch_path(self, pair: str, since: float, until: float) -> list:
        """Return ordered [(bar_ts, high, low, close)] 1-min bars covering the window.

        Kraken returns ~720 recent 1-min bars (~12h); the window (<=3h ago) is
        always within range at finalization time.
        """
        try:
            ohlc = self._api.get_ohlc_data(pair, interval=1)
            if not ohlc:
                return []
            key = next((k for k in ohlc if k != "last"), None)
            if not key:
                return []
            out = []
            for row in ohlc[key]:
                bts = int(row[0])
                if since - 60 <= bts <= until + 60:
                    # Kraken row: [time, open, high, low, close, vwap, volume, count]
                    out.append((bts, float(row[2]), float(row[3]), float(row[4])))
            out.sort(key=lambda x: x[0])
            return out
        except Exception as exc:
            logger.warning("[PROBE] path fetch failed for %s: %s", pair, exc)
            return []

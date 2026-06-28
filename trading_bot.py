# Trading Bot Core Logic - Multi-Pair Analysis
"""
Kraken Trading Bot â€” Core Engine
This module is the heart of the trading bot.  It contains the ``TradingBot``
class that orchestrates the full trading lifecycle, plus a minimal ``Backtester``
helper for offline strategy validation.

Signal flow (high level)
------------------------
::
                    'extra': extra or {},
    price_action.py  (bar-pattern helpers â€” optional context)
         â”‚
    analysis.py  TechnicalAnalysis.generate_signal_with_score()
                # If caller provided expected/fill price, compute slippage
                try:
                    expected = None
                    fill = None
                    if extra and isinstance(extra, dict):
                        expected = extra.get('expected_price')
                        fill = extra.get('fill_price')
                    if expected is not None and fill is not None:
                        try:
                            expected = float(expected)
                            fill = float(fill)
                            j['expected_price'] = expected
                            j['fill_price'] = fill
                            j['slippage_pct'] = round(((fill - expected) / expected) * 100.0, 4)
                        except Exception:
                            pass
         â”‚         â†³ RSI mean-reversion  (enable_mr_signals)
         â”‚         â†³ Bollinger-Band breakout (enable_trend_signals)
         â”‚         returns (signal: str, score: float  [-50 â€¦ +50])
         â”‚
    TradingBot.analyze_all_pairs()
         â”‚         â†³ fetches live ticker prices for all configured pairs
         â”‚         â†³ seeds price history from 60m OHLC when too sparse
         â”‚         â†³ picks the highest-scoring actionable pair
         â”‚
    TradingBot.start_trading()  â€” main loop (~60 s cycle)
         â”‚         â†³ check_take_profit_or_stop_loss()  (exits first)
         â”‚         â†³ layered BUY guards (see below)
         â”‚         â†³ execute_buy_order() / execute_sell_order()
         â”‚              execute_open_short_order() / execute_close_short_order()
         â”‚
    kraken_interface.py  KrakenAPI.place_order()
                          â†³ exclusive order lock (order_lock.py)
                          â†³ exponential back-off on rate-limit errors

Layered BUY entry guards (all must pass before a buy is placed)
---------------------------------------------------------------
1. Not temporarily paused (loss-streak cooldown)
2. Daily drawdown limit not hit
3. Bear Shield not active (BTC above 4h EMA50)
4. Regime filter: BTC benchmark score â‰¥ regime_min_score (RISK_ON)
5. Signal score â‰¥ min_buy_score (default 15.0)
6. Sentiment guard: no bad-news keywords in marquee file (optional)
7. Open positions < max_open_positions
8. MTF trend (1h SMA crossover) is bullish
9. Trading hours filter (UTC window, optional)
10. Volume filter: latest 15m candle â‰¥ volume_filter_min_ratio Ã— 20-candle avg

Key responsibilities of TradingBot
-----------------------------------
- Maintains per-pair state: holdings, entry price, peak price, stop levels,
  short positions, trade metrics, cooldown timestamps.
- Reconciles holdings and average entry price from Kraken trade history on
  startup/restart (``load_purchase_prices_from_history``).
- Hot-reloads ``config.toml`` every 5 minutes â€” no restart needed for tweaks.
- Writes structured JSONL trade events to ``logs/trade_events.jsonl`` and a
  human-readable CSV to ``reports/trade_journal.csv``.
- Persists the price-history buffer to ``data/history_buffer.json`` so RSI/SMA
  indicators survive a bot restart without a warm-up gap.
- NAS paths (trade history, OHLC archives) are resolved via ``utils.nas_paths()``.

Usage (called from main.py)
---------------------------
::

    from trading_bot import TradingBot
    bot = TradingBot(api_client, config)
    bot.start_trading()
"""

import json
import logging
import time
import os
from datetime import datetime, timezone
from pathlib import Path
from analysis import TechnicalAnalysis
from utils import load_config, pct_to_frac, apply_trade_costs, append_jsonl_locked, last_closed_trade_net_profit_pct

# Load .env if python-dotenv is available (graceful fallback otherwise)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        for _line in _env_path.read_text().splitlines():
            if "=" in _line and not _line.startswith("#"):
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from core import notifier as _notifier
from core import fee_sync as _fee_sync
try:
    from core.ws_feed import KrakenWSFeed as _KrakenWSFeed
    _WS_FEED_AVAILABLE = True
except ImportError:
    _WS_FEED_AVAILABLE = False

try:
    from core.market_intelligence import get_market_intelligence as _get_market_intelligence
    _INTELLIGENCE_AVAILABLE = True
except ImportError:
    _INTELLIGENCE_AVAILABLE = False

try:
    from core.history_db import (
        init_db as _init_db,
        record_trade as _record_trade,
        record_bot_snapshot as _record_bot_snapshot,
        update_ai_outcome as _update_ai_outcome,
        get_db_stats as _get_db_stats,
    )
    _HISTORY_DB_AVAILABLE = True
except ImportError:
    _HISTORY_DB_AVAILABLE = False

try:
    from core.onchain_data import fetch_all_onchain as _fetch_onchain_status
    _ONCHAIN_AVAILABLE = True
except ImportError:
    _ONCHAIN_AVAILABLE = False

try:
    import core.db_postgres as _pg
    _PG_AVAILABLE = True
except ImportError:
    _PG_AVAILABLE = False

try:
    from core.lunarcrush_data import fetch_all_sentiment as _fetch_lunar_status
    _LUNAR_STATUS_AVAILABLE = True
except ImportError:
    _LUNAR_STATUS_AVAILABLE = False

# Initialise PostgreSQL schema on startup if available
if _PG_AVAILABLE:
    try:
        _pg.init_schema()
    except Exception as _pge:
        pass

try:
    from core.alpaca_interface import (
        get_client as _alpaca_client,
        is_available as _alpaca_available,
        BTC_CORRELATES as _BTC_CORRELATES,
        ETH_CORRELATES as _ETH_CORRELATES,
        ALPACA_ALLOCATION_PCT as _ALPACA_ALLOC_PCT,
    )
    _ALPACA_ENABLED = True
except ImportError:
    _ALPACA_ENABLED = False

try:
    from core.listings_monitor import (
        fetch_new_kraken_listings as _fetch_listings,
        fetch_kraken_blog_listings as _fetch_blog_listings,
        fetch_kraken_new_pairs as _fetch_new_pairs,
        fetch_kraken_blog_headlines as _fetch_blog_headlines,
        load_watchlist as _load_watchlist,
        save_watchlist as _save_watchlist,
        add_to_watchlist as _add_to_watchlist,
        mark_bought as _mark_bought,
        remove_from_watchlist as _remove_listing,
        is_trending_up as _listing_trending_up,
        is_expired as _listing_expired,
        fetch_coingecko_new_coins as _fetch_coingecko_new,
        load_prewatchlist as _load_prewatchlist,
        save_prewatchlist as _save_prewatchlist,
        add_to_prewatchlist as _add_to_prewatchlist,
        check_prewatchlist_on_kraken as _check_prewatchlist,
    )
    _LISTINGS_AVAILABLE = True
except ImportError:
    _LISTINGS_AVAILABLE = False

try:
    from core.sharpe_calculator import calculate_sharpe as _calculate_sharpe
    from core.param_optimizer import run_optimizer as _run_optimizer, get_optimizer_history as _get_optimizer_history
    _OPTIMIZER_AVAILABLE = True
except ImportError:
    _OPTIMIZER_AVAILABLE = False

try:
    from core.ichimoku_gaussian import get_signal as _ichi_get_signal
    _ICHI_AVAILABLE = True
except ImportError:
    _ICHI_AVAILABLE = False

# NAS root â€” read from config [paths] nas_root, fallback to default mount point
def _resolve_nas_root(config: dict) -> Path:
    return Path(config.get('paths', {}).get('nas_root', '/mnt/fritz_nas/Volume/kraken'))
_TRADE_HISTORY_REFRESH_INTERVAL = 600  # seconds between Kraken API fetches (10 min)


def _sd_notify_watchdog() -> None:
    """Send WATCHDOG=1 ping to systemd via the NOTIFY_SOCKET (no extra packages needed)."""
    import socket
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    try:
        addr = "\0" + sock_path[1:] if sock_path.startswith("@") else sock_path
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(b"WATCHDOG=1", addr)
    except Exception:
        pass


class TradingBot:
    def __init__(self, api_client, config, config_path=None):
        """
        config_path: path to the TOML file that should be re-read on every
        hot-reload cycle (see reload_config()). If not supplied, falls back
        to config.toml next to this file -- the historical default.

        IMPORTANT: if you instantiate this bot with a *paper* or test config
        (e.g. config.paper.toml) loaded into `config`, you MUST also pass the
        matching config_path here. Otherwise reload_config() will silently
        overwrite your paper settings with whatever is in the live
        config.toml after `config_reload_interval` seconds (default 60s).
        """
        self.api_client = api_client
        self.config = config
        self.config_path = config_path or os.path.join(os.path.dirname(__file__), 'config.toml')
        self.logger = logging.getLogger(__name__)
        self.nas_root = _resolve_nas_root(config)

        self.analysis_tool = TechnicalAnalysis(rsi_period=14, sma_short=20, sma_long=50)

        # Signal engine mode: mean-reversion (reversion_bias) and/or trend/breakout (BB)
        self.enable_mr_signals = bool(self.config.get('risk_management', {}).get('enable_mean_reversion_signals', True))
        self.enable_trend_signals = bool(self.config.get('risk_management', {}).get('enable_trend_breakout_signals', True))
        self.mr_rsi_oversold = float(self.config.get('risk_management', {}).get('mr_rsi_oversold_threshold', 33.0))
        self.mr_rsi_overbought = float(self.config.get('risk_management', {}).get('mr_rsi_overbought_threshold', 67.0))
        self.analysis_tool.enable_mr_signals = self.enable_mr_signals
        self.analysis_tool.enable_trend_signals = self.enable_trend_signals
        self.analysis_tool.mr_rsi_buy = self.mr_rsi_oversold
        self.analysis_tool.mr_rsi_sell = self.mr_rsi_overbought

        self.trade_pairs = self.config['bot_settings'].get('trade_pairs', ['XBTEUR'])
        self._core_trade_pairs = list(self.trade_pairs)  # permanent pairs; listing pairs are temporary
        self.pair_signals = {}
        self.pair_prices = {}
        self.pair_scores = {}
        self.holdings = {}
        self.purchase_prices = {}
        # per-pair timestamp to rate-limit phantom-position reconciliation (seconds)
        self._phantom_last_checked = {}
        # path to persist purchase prices for recovery across restarts
        # Use a separate state file for paper-mode runs so a paper session
        # can never read or write real entry-price/position data from a live
        # bot sharing this same checkout (and vice versa).
        _pp_filename = 'purchase_prices_paper.json' if getattr(api_client, 'paper_mode', False) else 'purchase_prices.json'
        self.data_purchase_prices_path = os.path.join(os.path.dirname(__file__), 'data', _pp_filename)
        self.peak_prices = {}
        self.position_qty = {}
        self.short_qty = {}
        self.short_entry_prices = {}
        self.realized_pnl = {}
        self.fees_paid = {}
        self.trade_metrics = {}
        self.closed_trade_pnls = []
        self.last_trade_at = {}
        self.entry_timestamps = {}
        self.last_global_trade_at = 0
        self._normalized_pair_logs_seen = set()
        self._last_empty_sell_log_at = {}
        self._load_cooldown_state()

        self.trade_count = 0
        self.consecutive_losses = 0
        self.trading_paused_until_ts = 0
        self._circuit_breaker_triggered = False
        self.target_balance_eur = self._get_target_balance()
        # stop info per pair (stop_price, type)
        self.stop_info = {}
        # journaling path
        self.journal_path = os.path.join(os.path.dirname(__file__), 'data', 'trade_journal.csv')

        # Start a watchdog heartbeat thread to reliably send WATCHDOG=1 pings
        # This prevents systemd watchdog kills when the main loop blocks on I/O.
        try:
            import threading
            self._watchdog_interval = int(self.config.get('bot_settings', {}).get('watchdog_heartbeat_seconds', 60))
            def _watchdog_loop():
                while True:
                    try:
                        _sd_notify_watchdog()
                    except Exception:
                        pass
                    time.sleep(self._watchdog_interval)
            # only start if systemd provides a NOTIFY_SOCKET
            if os.environ.get('NOTIFY_SOCKET'):
                t = threading.Thread(target=_watchdog_loop, name='watchdog-heartbeat', daemon=True)
                t.start()
        except Exception:
            pass
        # Initialise persistent history DB
        if _HISTORY_DB_AVAILABLE:
            try:
                _init_db()
            except Exception:
                pass

        # structured JSONL trade log â€” separate files for paper and live
        # so live Sharpe is never diluted by simulated paper fills
        _is_paper = bool(getattr(self.api_client, 'paper_mode', False))
        _journal_name = 'trade_events_paper.jsonl' if _is_paper else 'trade_events_live.jsonl'
        self.json_journal_path = os.path.join(os.path.dirname(__file__), 'data', _journal_name)
        os.makedirs(os.path.dirname(self.json_journal_path), exist_ok=True)
        # manual kill-switch file: if present, bot will pause buys
        self.kill_switch_path = os.path.join(os.path.dirname(__file__), 'PAUSE')

        # â”€â”€ Market intelligence (Hermes + GPT) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        _intel_cfg = self.config.get('intelligence', {})
        self._intelligence_score: float = 0.0
        self._intelligence_last_ts: float = 0.0
        self._intelligence_refresh_secs: int = int(_intel_cfg.get('refresh_seconds', 600))
        self._intelligence_score_weight: float = float(_intel_cfg.get('score_weight', 2.0))
        self._intelligence_model_scores: dict = {}
        self._intelligence_model_outputs: dict = {}
        self._sharpe_funding_scores: dict = {}
        self._sharpe_insider_scores: dict = {}
        self._lunarcrush_combined: float = 0.0   # -3..+3 from lunarcrush
        self._onchain_combined: float = 0.0       # -3..+3 from on-chain data
        self._last_balance_eur: float = 0.0
        # Lock protecting all _intelligence_* and _sharpe_* fields so the
        # background intel-refresh thread doesn't race with main-loop reads.
        import threading as _th_init
        self._intel_lock = _th_init.Lock()

        # â”€â”€ TR-GC inspired features â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self._btc_downtrend: bool = False
        self._breakout_timestamps: dict = {}
        self._current_market_regime: str = "RANGING"   # TRENDING_UP / TRENDING_DOWN / RANGING

        # New listings monitor
        self._listing_watchlist: dict = _load_watchlist() if _LISTINGS_AVAILABLE else {}
        self._listings_last_check: float = 0.0
        self._listings_check_interval: int = 600    # Sharpe.ai + blog: every 10 min
        self._assetpairs_last_check: float = 0.0
        self._assetpairs_check_interval: int = 7200  # AssetPairs: every 2 hours (1.1MB per call)
        self._listing_hold_hours: int = 12
        self._listing_trend_pct: float = 0.8
        self._listing_stop_loss_pct: float = 1.0    # dump if down this much from buy
        self._listing_fee_pct: float = 0.52          # minimum above buy to consider selling
        self._listing_profit_target_pct: float = 8.0 # take profit at +8% for slow climbers
        self._listing_pullback_pct: float = 0.5     # trailing stop: sell if pulled back this much from peak
        self._kraken_headlines: list = []
        # CoinGecko pre-watchlist — monitors new CoinGecko coins against Kraken
        self._coingecko_prewatchlist: dict = _load_prewatchlist() if _LISTINGS_AVAILABLE else {}
        self._coingecko_last_check: float = 0.0
        self._coingecko_check_interval: int = 1800   # poll CoinGecko every 30 min
        self._prewatchlist_kraken_check: float = 0.0
        self._prewatchlist_kraken_interval: int = 300  # check pre-watchlist vs Kraken every 5 min

        # Alpaca correlation trading state
        self._alpaca_positions: dict = {}   # symbol -> {bought_at, kraken_signal}
        self._alpaca_last_sync: float = 0.0

        # â”€â”€ Monthly return target (3-8% per month) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self._monthly_start_balance: float = 0.0
        self._monthly_start_month: int = 0   # calendar month number
        self._monthly_target_hit_notified: bool = False

        # â”€â”€ Sharpe + scientific-method optimizer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        _opt_cfg = self.config.get('optimizer', {})
        self._optimizer_interval: int = int(_opt_cfg.get('eval_every_n_trades', 10))
        self._optimizer_enabled: bool = bool(_opt_cfg.get('enabled', True))

        # Initialise Sharpe from existing trade history so restarts don't lose progress
        self._sharpe_result: dict = {}
        self._closed_trades_count: int = 0
        if _OPTIMIZER_AVAILABLE:
            try:
                _journal = os.path.join(
                    os.path.dirname(__file__), 'data',
                    'trade_events_paper.jsonl' if getattr(api_client, 'paper_mode', False)
                    else 'trade_events_live.jsonl'
                )
                if os.path.exists(_journal):
                    import json as _j
                    with open(_journal, 'r', encoding='utf-8') as _f:
                        self._closed_trades_count = sum(
                            1 for _l in _f
                            if _l.strip() and _j.loads(_l).get('type') in ('SELL', 'SHORT_CLOSE')
                        )
                    if self._closed_trades_count >= 5:
                        self._sharpe_result = _calculate_sharpe(_journal)
                        self.logger.info(
                            "Sharpe initialised from history: %.3f (%d closed trades)",
                            self._sharpe_result.get('sharpe') or 0.0,
                            self._closed_trades_count
                        )
            except Exception as _se:
                self.logger.debug("Sharpe init from history failed: %s", _se)
        self.take_profit_percent = self._get_take_profit_percent()
        self.stop_loss_percent = self._get_stop_loss_percent()
        self.max_open_positions = int(self.config.get('risk_management', {}).get('max_open_positions', 3))
        self.trade_cooldown_sec = int(self.config.get('risk_management', {}).get('trade_cooldown_seconds', 180))
        self.global_trade_cooldown_sec = int(self.config.get('risk_management', {}).get('global_trade_cooldown_seconds', 300))
        self.trailing_stop_percent = float(self.config.get('risk_management', {}).get('trailing_stop_percent', 1.5))
        self.min_buy_score = float(self.config.get("risk_management", {}).get("min_buy_score", 18.0))
        self.logger.info(f"min_buy_score loaded: {self.min_buy_score}")
        self.adaptive_tp_enabled = bool(self.config.get('risk_management', {}).get('adaptive_take_profit', True))
        self.max_tp_percent = float(self.config.get('risk_management', {}).get('max_take_profit_percent', 14.0))
        self.sell_fee_buffer_percent = float(self.config.get('risk_management', {}).get('sell_fee_buffer_percent', 0.0))
        # Load live Kraken fee schedule (falls back to base-tier 0.40%/0.25%)
        _data_dir = os.path.join(os.path.dirname(__file__), 'data')
        self._fee_data = _fee_sync.load(_data_dir)
        self._fee_last_sync = time.time()
        _live_taker = _fee_sync.base_taker(self._fee_data)
        _live_maker = _fee_sync.base_maker(self._fee_data)
        self.fees_maker_percent = float(self.config.get('risk_management', {}).get('fees_maker_percent', _live_maker))
        self.fees_taker_percent = float(self.config.get('risk_management', {}).get('fees_taker_percent', _live_taker))
        # Normalized fee fractions (e.g. 0.0026 for 0.26%) â€” use pct_to_frac for consistency
        try:
            self.fees_maker_frac = pct_to_frac(self.fees_maker_percent)
            self.fees_taker_frac = pct_to_frac(self.fees_taker_percent)
        except Exception:
            self.logger.warning('Fee config error - using Kraken base-tier defaults')
            self.fees_maker_frac = 0.0025
            self.fees_taker_frac = 0.0040
        self.logger.info('Fees loaded: taker=%.2f%% maker=%.2f%% source=%s',
                         self.fees_taker_percent, self.fees_maker_percent,
                         self._fee_data.get('source', 'unknown'))
        # Re-entry guard: only pairs listed here are subject to blocking new BUYs until
        # the last closed trade for that pair achieved min_reentry_profit_pct net profit
        self.reentry_guard_pairs = [p.upper() for p in self.config.get('risk_management', {}).get('reentry_guard_pairs', ['VER'])]
        self.min_reentry_profit_pct = float(self.config.get('risk_management', {}).get('min_reentry_profit_pct', 5.0))
        # Minimum net sell profit required (percent, net of fees). If >0, SELLs are blocked
        # until the net profit target is met. Use with caution.
        self.min_net_sell_profit_pct = float(self.config.get('risk_management', {}).get('min_net_sell_profit_pct', 0.0))
        self.empty_sell_log_cooldown_sec = int(self.config.get('risk_management', {}).get('empty_sell_log_cooldown_seconds', 1800))
        # ATR stop config
        self.enable_atr_stop = bool(self.config.get('risk_management', {}).get('enable_atr_stop', False))
        self.atr_period = int(self.config.get('risk_management', {}).get('atr_period', 14))
        self.atr_multiplier = float(self.config.get('risk_management', {}).get('atr_multiplier', 1.5))
        self.atr_trail_multiplier = float(self.config.get('risk_management', {}).get('atr_trail_multiplier', 0.75))
        # ATR dynamic take-profit: TP floor = atr_tp_multiplier Ã— ATR%
        self.enable_atr_dynamic_tp = bool(self.config.get('risk_management', {}).get('enable_atr_dynamic_tp', False))
        self.atr_tp_multiplier = float(self.config.get('risk_management', {}).get('atr_tp_multiplier', 2.0))
        # Signal refresh and regime cache (reduce API calls)
        self.signal_refresh_interval = int(self.config.get('execution', {}).get('signal_refresh_interval_seconds', 300))
        self._last_signal_refresh_ts = 0
        self._regime_cache_ttl = int(self.config.get('execution', {}).get('regime_cache_ttl_seconds', 300))
        self._regime_cache = {'ts': 0, 'risk_on': True}
        
        # Break-even stop-loss
        self.enable_break_even = bool(self.config.get('risk_management', {}).get('enable_break_even', True))
        self.break_even_trigger_pct = float(self.config.get('risk_management', {}).get('break_even_trigger_percent', 1.5))
        
        # pyramiding
        self.enable_pyramiding = bool(self.config.get('risk_management', {}).get('enable_pyramiding', False))
        self.pyramiding_add_pct = float(self.config.get('risk_management', {}).get('pyramiding_add_pct', 0.5))
        self.enable_regime_filter = bool(self.config.get('risk_management', {}).get('enable_regime_filter', True))
        self.regime_benchmark_pair = str(self.config.get('risk_management', {}).get('regime_benchmark_pair', 'XBTEUR')).upper()
        self.regime_min_score = float(self.config.get('risk_management', {}).get('regime_min_score', -5.0))
        self.enable_hard_stop_loss = bool(self.config.get('risk_management', {}).get('enable_hard_stop_loss', True))
        self.hard_stop_loss_percent = float(self.config.get('risk_management', {}).get('hard_stop_loss_percent', 4.0))
        self.enable_mtf_regime_scoring = bool(self.config.get('risk_management', {}).get('enable_mtf_regime_scoring', True))
        self.mtf_regime_min_score = float(self.config.get('risk_management', {}).get('mtf_regime_min_score', -2.0))
        self.enable_time_stop = bool(self.config.get('risk_management', {}).get('enable_time_stop', True))
        self.time_stop_hours = int(self.config.get('risk_management', {}).get('time_stop_hours', 72))
        self.enable_daily_drawdown = bool(self.config.get('risk_management', {}).get('enable_daily_drawdown', True))
        self.daily_drawdown_percent = float(self.config.get('risk_management', {}).get('daily_loss_limit_percent', 3.0))
        self.risk_off_allocation_multiplier = float(self.config.get('risk_management', {}).get('risk_off_allocation_multiplier', 0.35))
        self.enable_volatility_targeting = bool(self.config.get('risk_management', {}).get('enable_volatility_targeting', True))
        self.target_volatility_pct = float(self.config.get('risk_management', {}).get('target_volatility_pct', 1.6))
        self.max_consecutive_losses = int(self.config.get('risk_management', {}).get('max_consecutive_losses', 3))
        self.pause_after_loss_streak_minutes = int(self.config.get('risk_management', {}).get('pause_after_loss_streak_minutes', 180))
        self.enable_live_shorts = bool(self.config.get('shorting', {}).get('enabled', False))
        # Shorting config
        self.short_leverage = float(self.config.get('shorting', {}).get('leverage', 2.0))
        # cap per-short to limit tail risk
        self.max_short_notional_eur = float(self.config.get('shorting', {}).get('max_short_notional_eur', 50.0))
        self.short_take_profit_percent = float(self.config.get('shorting', {}).get('short_take_profit_percent', 2.5))
        self.short_stop_loss_percent = float(self.config.get('shorting', {}).get('short_stop_loss_percent', 3.5))
        # Safety: minimum margin buffer (fraction of free margin to keep)
        self.min_free_margin_buffer = float(self.config.get('shorting', {}).get('min_free_margin_buffer', 0.05))
        # Short enabling toggle
        self.enable_live_shorts = bool(self.config.get('shorting', {}).get('enabled', False))

        # Fast scalp / hit-and-run profile
        self.enable_fast_scalp = bool(self.config.get('profiles', {}).get('fast_scalp', {}).get('enabled', False))
        self.fast_scalp_require_flag = bool(self.config.get('profiles', {}).get('fast_scalp', {}).get('require_enable_flag', True))
        self.fast_scalp_time_stop_minutes = int(self.config.get('profiles', {}).get('fast_scalp', {}).get('time_stop_minutes', 30))
        self.fast_scalp_stop_loss_pct = float(self.config.get('profiles', {}).get('fast_scalp', {}).get('stop_loss_percent', 0.6))
        self.fast_scalp_take_profit_pct = float(self.config.get('profiles', {}).get('fast_scalp', {}).get('take_profit_percent', 1.2))

        self.start_time = datetime.now()
        self.last_config_reload = datetime.now()
        self.config_reload_interval = 60
        self.loop_interval_sec = int(self.config.get('bot_settings', {}).get('loop_interval_seconds', 60))
        self.daily_start_balance = None
        self.initial_balance_eur = None
        self.start_timestamp = int(time.time())
        self.net_deposits_eur = 0.0
        self.net_withdrawals_eur = 0.0
        self._last_cashflow_refresh_ts = 0
        self.cashflow_refresh_interval_sec = int(self.config.get('reporting', {}).get('cashflow_refresh_seconds', 600))

        # Daily CSV report
        self._report_time_utc       = str(self.config.get('reporting', {}).get('report_time_utc', '09:35'))
        self._report_last_sent_date: str = ''

        if self.cashflow_refresh_interval_sec > 300:
            self.logger.warning(
                f"cashflow_refresh_seconds is {self.cashflow_refresh_interval_sec}s (>5m). "
                f"Deposits/withdrawals may not be reflected for up to {self.cashflow_refresh_interval_sec}s. "
                f"Consider setting cashflow_refresh_seconds = 60 in config.toml [reporting]."
            )
        self.last_daily_reset_ts = int(time.time())

        # Initialise dicts used by _init_pair_state BEFORE it is called
        self._ema_bullish = {}
        # cache for 1h RSI and SMA200 used by simplified mean-reversion signals and exits
        self._rsi_1h = {}
        self._sma200_1h = {}
        self._macd_1h_hist = {}
        self._macd_15m_hist = {}
        self._macd_15m_hist_prev = {}
        self._partial_exit_done = {}

        self.valid_pairs = self._fetch_valid_trade_pairs(self.trade_pairs)
        self.trade_pairs = self.valid_pairs if self.valid_pairs else []
        self._init_pair_state(self.trade_pairs)
        
        # Flash-crash airbag tracking: {pair: [(timestamp, price), ...]}
        self.price_history_airbag = {p: [] for p in self.trade_pairs}
        self.airbag_drop_threshold = float(self.config.get('risk_management', {}).get('airbag_drop_threshold', 15.0))
        self.airbag_window_minutes = int(self.config.get('risk_management', {}).get('airbag_window_minutes', 10))
        
        # Sentiment integration (opt-in)
        self.enable_sentiment_guard = bool(self.config.get('risk_management', {}).get('enable_sentiment_guard', False))
        self.news_marquee_path = "/tmp/youtube_stream/news_marquee.txt"
        self.sentiment_pause_keywords = ["crash", "hack", "dump", "sec", "lawsuit", "regulation", "ban"]
        self.sentiment_active = False

        # Time-of-day filter: only open new positions during high-volume hours (UTC)
        self.enable_trading_hours = bool(self.config.get('risk_management', {}).get('enable_trading_hours', True))
        self.trading_hours_start_utc = int(self.config.get('risk_management', {}).get('trading_hours_start_utc', 14))
        self.trading_hours_end_utc = int(self.config.get('risk_management', {}).get('trading_hours_end_utc', 22))

        # Volume filter: skip entries when volume is unusually low
        self.enable_volume_filter = bool(self.config.get('risk_management', {}).get('enable_volume_filter', True))
        self.volume_filter_min_ratio = float(self.config.get('risk_management', {}).get('volume_filter_min_ratio', 0.5))
        self._volume_cache = {}  # {pair: (timestamp, ratio)}

        # Bear Shield: auto-park in FIAT during confirmed downtrends
        bear_cfg = self.config.get('bear_shield', {})
        self.enable_bear_shield = bool(bear_cfg.get('enable_bear_shield', False))
        self.bear_ema_period = int(bear_cfg.get('bear_ema_period', 50))
        self.bear_confirm_candles = int(bear_cfg.get('bear_confirm_candles', 3))
        self.bear_benchmark_pair = str(bear_cfg.get('bear_benchmark_pair', 'XETHZEUR')).upper()
        self.bear_log_interval_minutes = int(bear_cfg.get('bear_log_interval_minutes', 60))
        self._bear_mode_active = False          # current state
        self._bear_last_log_ts = 0              # throttle logging

        # Trade history cache: avoids hitting Kraken API every loop iteration
        self._trade_history_cache: dict = {}    # {trade_id: trade_dict}
        self._trade_history_last_fetch: float = 0.0  # unix timestamp of last API fetch

        # â”€â”€ Multi-timeframe OHLC caches for EMA + MTF MACD â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

        # â”€â”€ EMA crossover filter â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        tech_cfg = self.config.get('technical', {})
        self.enable_ema_crossover_filter = bool(tech_cfg.get('enable_ema_crossover_filter', True))
        self.ema_fast_period = int(tech_cfg.get('ema_fast_period', 9))
        self.ema_slow_period = int(tech_cfg.get('ema_slow_period', 21))

        # â”€â”€ Multi-timeframe MACD filter â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self.enable_mtf_macd_filter = bool(tech_cfg.get('enable_mtf_macd_filter', True))

        # â”€â”€ Partial take-profit exit â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self.enable_partial_exit = bool(tech_cfg.get('enable_partial_exit', True))
        self.partial_exit_trigger_pct = float(tech_cfg.get('partial_exit_trigger_pct', 4.0))
        self.partial_exit_fraction = float(tech_cfg.get('partial_exit_fraction', 0.5))
        self.partial_exit_min_remaining_eur = float(tech_cfg.get('partial_exit_min_remaining_eur', 5.0))
        # _partial_exit_done dict already created before _init_pair_state; no re-init here

        # â”€â”€ WebSocket price feed (zero-cost live prices, falls back to REST) â”€â”€
        self.ws_feed = None
        ws_cfg = self.config.get('websocket', {})
        if bool(ws_cfg.get('enable_ws_feed', True)) and _WS_FEED_AVAILABLE:
            try:
                self.ws_feed = _KrakenWSFeed(self.trade_pairs)
                self.ws_feed.start()
            except Exception as _e:
                self.logger.warning(f"WebSocket feed could not start: {_e} â€” falling back to REST polling")

    def _notify_pause(self, reason):
        """Log and attempt to notify an external channel when a trading pause activates."""
        try:
            import json, subprocess, datetime, os
            logp = os.path.join(os.path.dirname(__file__), 'logs', 'pause_events.log')
            os.makedirs(os.path.dirname(logp), exist_ok=True)
            entry = {
                'ts': datetime.datetime.utcnow().isoformat(),
                'reason': reason,
                'balance': float(self.get_eur_balance()),
                'consecutive_losses': int(getattr(self,'consecutive_losses',0))
            }
            with open(logp,'a') as f:
                f.write(json.dumps(entry) + "\n")
            # call optional notifier script
            script = os.path.join(os.path.dirname(__file__), 'scripts', 'notify_pause.sh')
            if os.path.exists(script) and os.access(script, os.X_OK):
                try:
                    subprocess.Popen([script, reason], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception as e:
                    self.logger.debug(f"notify_pause: could not run notifier script: {e}")
        except Exception as e:
            self.logger.warning(f"notify_pause: failed to write pause log: {e}")

    # â”€â”€ Bear Shield â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _calc_ema(self, prices, period):
        """Simple EMA calculation (no external dependencies)."""
        if len(prices) < period:
            return None
        k = 2.0 / (period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = p * k + ema * (1 - k)
        return ema

    def _is_bear_market(self):
        """Return True when the 4h trend has confirmed a downtrend for bear_confirm_candles.

        Logic: fetch 4h OHLC for bear_benchmark_pair, compute EMA(bear_ema_period).
               If the last bear_confirm_candles closes are ALL below EMA â†' bear mode.
               If price crosses back above EMA â†' bull mode restored.
        Fails safe: returns False (allow trading) if API call fails.
        """
        if not self.enable_bear_shield:
            return False
        try:
            ohlc = self.api_client.get_ohlc_data(self.bear_benchmark_pair, interval=240)  # 4h
            if not ohlc:
                return False
            key = [k for k in ohlc.keys() if k != 'last']
            if not key:
                return False
            rows = ohlc[key[0]]
            closes = [float(r[4]) for r in rows if r and len(r) >= 5]
            if len(closes) < self.bear_ema_period + self.bear_confirm_candles:
                return False

            ema = self._calc_ema(closes[:-self.bear_confirm_candles], self.bear_ema_period)
            if ema is None:
                return False

            # Check last N candles are all below EMA
            last_n = closes[-self.bear_confirm_candles:]
            return all(c < ema for c in last_n)
        except Exception as e:
            self.logger.debug(f"Bear shield check failed (safe fallback to False): {e}")
            return False

    def _bear_shield_exit_all(self):
        """Sell all open long positions to park in FIAT (bear market escape)."""
        sold_any = False
        for pair in list(self.trade_pairs):
            qty = self.holdings.get(pair, 0.0)
            min_vol = self._get_min_volume(pair)
            if qty >= min_vol:
                price = self.pair_prices.get(pair, 0.0)
                if price > 0:
                    self.logger.warning(
                        f"BEAR SHIELD: selling {qty:.6f} {pair} @ {price:.4f} EUR to park in FIAT"
                    )
                    self.execute_sell_order(pair, price)
                    sold_any = True
        return sold_any

    def _update_airbag_history(self, pair, price):
        """Append (timestamp, price) to the rolling flash-crash window for *pair*.

        The window is kept to the last ``airbag_window_minutes`` minutes.
        Called every cycle from ``analyze_all_pairs`` before the airbag check.
        """
        now = time.time()
        history = self.price_history_airbag.get(pair, [])
        history.append((now, price))
        # Remove old entries
        cutoff = now - (self.airbag_window_minutes * 60)
        self.price_history_airbag[pair] = [h for h in history if h[0] >= cutoff]
        
    def _check_airbag_trigger(self, pair):
        """Return True if price has dropped â‰¥ airbag_drop_threshold% within the airbag window.

        When triggered, the caller (``analyze_all_pairs``) immediately issues a
        market sell to exit the position â€” this is the "flash-crash airbag".
        Requires at least 2 data points; returns False if insufficient history.
        """
        history = self.price_history_airbag.get(pair, [])
        if len(history) < 2:
            return False
        peak_price = max(h[1] for h in history)
        current_price = history[-1][1]
        drop = ((peak_price - current_price) / peak_price) * 100.0
        if drop >= self.airbag_drop_threshold:
            self.logger.critical(f"AIRBAG TRIGGERED for {pair}: drop of {drop:.2f}% in {self.airbag_window_minutes}m")
            return True
        return False

    def _scan_news_sentiment(self):
        try:
            if not os.path.exists(self.news_marquee_path):
                return False
            import re, fcntl
            with open(self.news_marquee_path, 'r') as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                    content = f.read().lower()
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except (OSError, BlockingIOError):
                    # File is being written to; skip this cycle safely
                    return self.sentiment_active  # Keep previous state
            # Use word boundaries to avoid false positives (like 'sec' in 'secretary')
            found = [k for k in self.sentiment_pause_keywords if re.search(r'\b' + re.escape(k) + r'\b', content)]
            if found:
                if not self.sentiment_active:
                    self.logger.warning(f"SENTIMENT GUARD: Keywords found in news ({', '.join(found)}). Pausing Buys.")
                return True
            return False
        except Exception:
            return False

    def _init_pair_state(self, pairs):
        """Initialise all per-pair state dicts for newly added pairs.

        Called once at startup for all configured pairs and again whenever
        ``reload_config`` detects that new pairs have been added to the config.
        Safe to call multiple times â€” ``setdefault`` prevents overwriting
        existing state for pairs that are already active.
        """
        for pair in pairs:
            self.pair_signals.setdefault(pair, "HOLD")
            self.holdings.setdefault(pair, 0.0)
            self.purchase_prices.setdefault(pair, 0.0)
            self.peak_prices.setdefault(pair, 0.0)
            self.position_qty.setdefault(pair, 0.0)
            self.short_qty.setdefault(pair, 0.0)
            self.short_entry_prices.setdefault(pair, 0.0)
            self.realized_pnl.setdefault(pair, 0.0)
            self.fees_paid.setdefault(pair, 0.0)
            self.trade_metrics.setdefault(pair, {"closed": 0, "wins": 0, "losses": 0, "sum_pnl": 0.0})
            self.last_trade_at.setdefault(pair, 0)
            self.entry_timestamps.setdefault(pair, None)
            # MTF indicator caches and partial exit state
            self._ema_bullish.setdefault(pair, None)
            self._macd_1h_hist.setdefault(pair, None)
            self._macd_15m_hist.setdefault(pair, None)
            self._macd_15m_hist_prev.setdefault(pair, None)
            self._partial_exit_done.setdefault(pair, False)

    def _get_target_balance(self):
        try:
            return self.config['bot_settings']['trade_amounts'].get('target_balance_eur', 1000.0)
        except Exception:
            return self.config['bot_settings'].get('target_balance_eur', 1000.0)

    def _get_take_profit_percent(self):
        try:
            return float(self.config['risk_management'].get('take_profit_percent', 5.0))
        except Exception:
            return 5.0

    def _get_stop_loss_percent(self):
        try:
            return float(self.config['risk_management'].get('stop_loss_percent', 2.0))
        except Exception:
            return 2.0

    def _get_trade_amount_eur(self):
        try:
            return float(self.config['bot_settings']['trade_amounts'].get('trade_amount_eur', 30.0))
        except Exception:
            return 30.0

    def _get_optimizer_status(self) -> dict:
        """Return current optimizer experiment + recent history for dashboard."""
        try:
            if not _OPTIMIZER_AVAILABLE:
                return {}
            from core.param_optimizer import _load_state as _opt_state
            state = _opt_state()
            exp   = state.get("current_experiment")
            hist  = state.get("history", [])[-10:]   # last 10 decisions
            return {
                "baseline_sharpe":  state.get("baseline_sharpe"),
                "current_experiment": {
                    "param":     exp.get("key") if exp else None,
                    "old_value": exp.get("original") if exp else None,
                    "new_value": exp.get("tested") if exp else None,
                    "direction": exp.get("direction") if exp else None,
                    "sharpe_at_start": exp.get("sharpe_at_start") if exp else None,
                } if exp else None,
                "history": [
                    {
                        "param":         h.get("key"),
                        "old":           h.get("original"),
                        "new":           h.get("tested"),
                        "sharpe_before": h.get("sharpe_before"),
                        "sharpe_after":  h.get("sharpe_after"),
                        "verdict":       h.get("verdict"),
                        "pct_change":    round(
                            ((h.get("sharpe_after") or 0) - (h.get("sharpe_before") or 0))
                            / max(abs(h.get("sharpe_before") or 1), 0.001) * 100, 1
                        ) if h.get("sharpe_after") is not None else None,
                    }
                    for h in reversed(hist)
                ],
            }
        except Exception:
            return {}

    def _monthly_return_pct(self, current_balance: float) -> float:
        """Return % gain since the start of the current calendar month."""
        if self._monthly_start_balance <= 0:
            return 0.0
        return ((current_balance - self._monthly_start_balance) / self._monthly_start_balance) * 100.0

    def _monthly_size_multiplier(self, current_balance: float) -> float:
        """
        Adjust position sizing based on monthly return progress:
          >= 8%  â†' 0.3Ã— (protect gains â€” lock in the month)
          >= 3%  â†' 0.7Ã— (on track â€” slightly conservative)
          >= 0%  â†' 1.0Ã— (normal)
          <  0%  â†' 1.2Ã— (behind â€” slight aggression, within risk limits)
        """
        pct = self._monthly_return_pct(current_balance)
        if pct >= 8.0:
            return 0.3
        if pct >= 3.0:
            return 0.7
        if pct >= 0.0:
            return 1.0
        return 1.2

    def _is_btc_downtrend(self) -> bool:
        """Return True when BTC close is below its 20-period SMA â€' BEAR regime.
        Mirrors the TR-GC-Crypto-LS-2 BTC regime flag from step 3."""
        btc_pair = next((p for p in self.trade_pairs if 'XBT' in p or 'BTC' in p), None)
        if not btc_pair:
            return False
        try:
            history = list(self.analysis_tool._get_price_history(btc_pair))
            if len(history) < 20:
                return False
            sma20 = sum(history[-20:]) / 20
            return history[-1] < sma20
        except Exception:
            return False

    def _detect_market_regime(self) -> str:
        """
        Classify the current market into one of three regimes:

          TRENDING_UP    BTC SMA8 > SMA21 > SMA50, trending clearly upward
          TRENDING_DOWN  BTC SMA8 < SMA21 < SMA50, trending clearly downward
          RANGING        SMAs are tangled, no clear direction — sideways market

        Returns one of: "TRENDING_UP", "TRENDING_DOWN", "RANGING"

        Used to switch between momentum (trending) and mean-reversion (ranging) strategies.
        """
        btc_pair = next((p for p in self.trade_pairs if 'XBT' in p or 'BTC' in p), None)
        if not btc_pair:
            return "RANGING"
        try:
            history = list(self.analysis_tool._get_price_history(btc_pair))
            if len(history) < 50:
                return "RANGING"

            import numpy as _np
            prices = _np.array(history)
            sma8   = float(_np.mean(prices[-8:]))
            sma21  = float(_np.mean(prices[-21:]))
            sma50  = float(_np.mean(prices[-50:]))

            # Clear uptrend: each shorter SMA above longer SMA
            if sma8 > sma21 > sma50:
                # Confirm momentum: SMA8 at least 0.3% above SMA50
                if (sma8 - sma50) / sma50 * 100 >= 0.3:
                    return "TRENDING_UP"

            # Clear downtrend: each shorter SMA below longer SMA
            if sma8 < sma21 < sma50:
                if (sma50 - sma8) / sma50 * 100 >= 0.3:
                    return "TRENDING_DOWN"

            return "RANGING"
        except Exception:
            return "RANGING"

    def _regime_strategy_config(self) -> dict:
        """
        Return strategy parameters appropriate for the current regime.

        TRENDING:  Use trend-following signals. Raise min_buy_score so only
                   strong breakout signals fire. Disable mean reversion.
        RANGING:   Use mean reversion. Lower min_buy_score so RSI extremes fire.
                   Reduce position size (ranging markets reverse quickly).
        """
        regime = self._current_market_regime
        if regime == "TRENDING_UP":
            return {
                "enable_mr":        False,   # don't fade an uptrend
                "enable_trend":     True,
                "score_multiplier": 1.3,     # boost trend signals
                "size_multiplier":  1.2,     # bigger positions in trending market
                "label":            "TREND UP",
            }
        elif regime == "TRENDING_DOWN":
            return {
                "enable_mr":        False,
                "enable_trend":     True,
                "score_multiplier": 1.3,
                "size_multiplier":  0.8,     # slightly smaller — downtrends risky for longs
                "label":            "TREND DOWN",
            }
        else:  # RANGING
            return {
                "enable_mr":        True,    # fade RSI extremes in ranging market
                "enable_trend":     False,   # don't chase breakouts in ranging market
                "score_multiplier": 1.0,
                "size_multiplier":  0.7,     # smaller size — ranging markets whipsaw
                "label":            "RANGING",
            }

    # Per-pair strategy profiles — each coin gets the approach that suits its behaviour
    _PAIR_PROFILES = {
        # Large caps: slower moving, needs genuine trend confirmation
        "XBTEUR":   {"rsi_buy": 32, "rsi_sell": 68, "min_score": 8,  "strategy": "trend"},
        "XXBTZEUR": {"rsi_buy": 32, "rsi_sell": 68, "min_score": 8,  "strategy": "trend"},
        "XETHZEUR": {"rsi_buy": 32, "rsi_sell": 68, "min_score": 8,  "strategy": "trend"},
        "ETHEUR":   {"rsi_buy": 32, "rsi_sell": 68, "min_score": 8,  "strategy": "trend"},
        # Mid caps: more volatile, mean reversion works well
        "SOLEUR":   {"rsi_buy": 28, "rsi_sell": 72, "min_score": 5,  "strategy": "mean_reversion"},
        "XXRPZEUR": {"rsi_buy": 28, "rsi_sell": 72, "min_score": 5,  "strategy": "mean_reversion"},
        "XRPEUR":   {"rsi_buy": 28, "rsi_sell": 72, "min_score": 5,  "strategy": "mean_reversion"},
        "ADAEUR":   {"rsi_buy": 28, "rsi_sell": 72, "min_score": 5,  "strategy": "mean_reversion"},
        # Small/mid caps: higher volatility, accept weaker signals
        "DOTEUR":   {"rsi_buy": 25, "rsi_sell": 75, "min_score": 3,  "strategy": "mean_reversion"},
        "LINKEUR":  {"rsi_buy": 25, "rsi_sell": 75, "min_score": 3,  "strategy": "mean_reversion"},
    }

    def _pair_profile(self, pair: str) -> dict:
        """Return the strategy profile for a pair, with sensible defaults."""
        return self._PAIR_PROFILES.get(pair, {
            "rsi_buy": 30, "rsi_sell": 70, "min_score": 5, "strategy": "mean_reversion"
        })

    def _correlation_size_multiplier(self, pair: str) -> float:
        """Reduce position size when correlated assets are already held.
        All crypto pairs move together (~0.8 correlation) so n open positions
        = one large bet, not n independent ones. Formula: 1/(1+n_open)."""
        n_open = sum(
            1 for p in self.trade_pairs
            if p != pair and
            (self.position_qty.get(p, 0) or self.holdings.get(p, 0)) >= self._get_min_volume(p)
        )
        return round(1.0 / (1.0 + n_open), 3)

    def _breakout_size_multiplier(self, pair) -> float:
        """Size multiplier based on breakout recency (TR-GC inspired).
        Recent BB breakout (< 25 days) â†' 2Ã— allocation.
        No recent breakout â†' 0.5Ã— allocation.
        This encourages entering bigger on fresh signals."""
        last_ts = self._breakout_timestamps.get(pair, 0)
        if last_ts <= 0:
            return 0.5   # no recorded breakout â†' conservative
        days_ago = (time.time() - last_ts) / 86400
        if days_ago <= 25:
            return 2.0   # fresh breakout â†' double allocation
        return 0.5       # stale breakout â†' halve allocation

    def _get_dynamic_trade_amount_eur(self, pair, available_eur):
        """Dynamic sizing: adjusted by ATR volatility and available EUR."""
        base_amount = self._get_trade_amount_eur()
        
        # 1. Start with percentage-based sizing
        allocation_pct = float(self.config.get('risk_management', {}).get('allocation_per_trade_percent', 10.0))
        amount = available_eur * (allocation_pct / 100.0)

        # small-account override: for tiny accounts prefer a fixed trade amount
        small_account_floor = float(self.config.get('risk_management', {}).get('small_account_fixed_trade_eur', 25.0))
        small_account_threshold = float(self.config.get('risk_management', {}).get('small_account_threshold_eur', 200.0))
        if available_eur <= small_account_threshold:
            amount = min(amount, small_account_floor)
        
        # 2. ATR adjustment (Vol Targeting)
        # We target a specific % movement per trade.
        atr = self.analysis_tool.calculate_atr(pair)
        current_price = self.pair_prices.get(pair, 0)
        
        if atr and current_price > 0:
            # Volatility scaling: target a notional such that 1 ATR move equals target_vol_pct of trade
            volatility_ratio = (atr / current_price) * 100.0
            target_vol_pct = float(self.config.get('risk_management', {}).get('target_volatility_pct', 1.6))
            # multiplier proportional to target_vol_pct / observed_vol (clamped)
            vol_multiplier = (target_vol_pct / max(0.1, volatility_ratio))
            vol_multiplier = max(0.3, min(3.0, vol_multiplier))
            amount *= vol_multiplier

        # 3. Apply risk-off multiplier from regime
        amount *= self._allocation_multiplier()

        # 4. Breakout recency multiplier (TR-GC inspired)
        amount *= self._breakout_size_multiplier(pair)

        # 5. Correlation-aware sizing — shrink when correlated positions already open
        amount *= self._correlation_size_multiplier(pair)

        # 6. Regime size multiplier — bigger in trends, smaller when ranging
        amount *= self._regime_strategy_config().get('size_multiplier', 1.0)

        # 7. Half-Kelly sizing — scale by 0.5 x Kelly fraction from actual trade history
        # Uses 0.5x (half-Kelly) not full Kelly to reduce variance while keeping the edge.
        # Needs 10+ closed trades to activate; defaults to 0.1 (10%) before then.
        kelly = getattr(self, 'kelly_fraction', 0.1)
        half_kelly = kelly * 0.5
        # Map kelly [0.01, 0.5] -> multiplier [0.3, 1.5] so it meaningfully adjusts size
        kelly_multiplier = max(0.3, min(1.5, half_kelly / 0.1))
        amount *= kelly_multiplier

        # 6. Monthly return multiplier — protect gains, slight aggression when behind
        amount *= self._monthly_size_multiplier(available_eur)

        # Cap at configured max base amount and available funds
        return min(base_amount * 2.0, amount, available_eur * 0.95)

    def _is_mtf_trend_bullish(self, pair):
        """Check 1h timeframe to confirm bullish trend.

        Behaviour change: prefer local cached history when the OHLC API is
        unavailable or returns no data. Previously the function was "fail-closed"
        (return False), which could indefinitely block buys during transient
        API failures or rate limits. New behaviour:
          - If API returns no data, try local buffer via analysis_tool.pair_price_history
          - If no local history is available, fall back to fail-open (return True)
            to avoid permanently blocking trading due to transient infra issues.
        """
        try:
            # Use cached regime data if fresh to avoid extra API calls
            now = time.time()
            if (now - self._regime_cache.get('ts', 0)) <= self._regime_cache_ttl:
                # rely on cached pair history for MTF check
                returns = self.analysis_tool.pair_price_history.get(pair)
                if returns:
                    closes = list(returns)
                    return self.analysis_tool.check_mtf_trend(closes)

            ohlc = self.api_client.get_ohlc_data(pair, interval=60) # 1h
            data_key = next((k for k in ohlc if k != 'last'), None) if ohlc else None
            if not ohlc or not data_key:
                # API returned no data â€” fallback to local buffer if available,
                # otherwise fail-open (allow trading) to avoid permanent blocks
                self.logger.warning(f"MTF check: no OHLC from API for {pair}; falling back to local history if available")
                local = self.analysis_tool.pair_price_history.get(pair)
                if local:
                    return self.analysis_tool.check_mtf_trend(list(local))
                return True

            # Kraken returns [time, open, high, low, close, vwap, volume, count]
            closes = [float(row[4]) for row in ohlc[data_key]]
            return self.analysis_tool.check_mtf_trend(closes)
        except Exception as e:
            self.logger.error(f"MTF check failed for {pair}: {e}")
            # Exception occurred â€” attempt to use local cached history before failing open
            local = self.analysis_tool.pair_price_history.get(pair)
            if local:
                try:
                    return self.analysis_tool.check_mtf_trend(list(local))
                except Exception:
                    pass
            # As a last resort, allow trading (fail-open) to avoid blocking due to transient errors
            return True

    def _is_mtf_trend_bullish_30m(self, pair):
        """Check 30m timeframe to confirm bullish trend (used for shorting decisions).

        Same fail-open logic as _is_mtf_trend_bullish but on 30m interval.
        """
        try:
            # Use cached regime data if fresh to avoid extra API calls
            now = time.time()
            if (now - self._regime_cache.get('ts', 0)) <= self._regime_cache_ttl:
                # rely on cached pair history for MTF check
                returns = self.analysis_tool.pair_price_history.get(pair)
                if returns:
                    closes = list(returns)
                    return self.analysis_tool.check_mtf_trend(closes)

            ohlc = self.api_client.get_ohlc_data(pair, interval=30) # 30m
            data_key = next((k for k in ohlc if k != 'last'), None) if ohlc else None
            if not ohlc or not data_key:
                # API returned no data â€” fallback to local buffer if available,
                # otherwise fail-open (allow trading) to avoid permanent blocks
                self.logger.warning(f"MTF30 check: no OHLC from API for {pair}; falling back to local history if available")
                local = self.analysis_tool.pair_price_history.get(pair)
                if local:
                    return self.analysis_tool.check_mtf_trend(list(local))
                return True

            # Kraken returns [time, open, high, low, close, vwap, volume, count]
            closes = [float(row[4]) for row in ohlc[data_key]]
            return self.analysis_tool.check_mtf_trend(closes)
        except Exception as e:
            self.logger.error(f"MTF30 check failed for {pair}: {e}")
            # Exception occurred â€” attempt to use local cached history before failing open
            local = self.analysis_tool.pair_price_history.get(pair)
            if local:
                try:
                    return self.analysis_tool.check_mtf_trend(list(local))
                except Exception:
                    pass
            # As a last resort, allow trading (fail-open) to avoid blocking due to transient errors
            return True
    def _get_min_volume(self, pair):
        try:
            min_volumes = self.config['bot_settings'].get('min_volumes', {})
            if pair in min_volumes:
                return float(min_volumes.get(pair, 0.0001))

            # alias fallback (altname <-> wsname style)
            aliases = {
                'XBTEUR': 'XXBTZEUR',
                'ETHEUR': 'XETHZEUR',
                'XRPEUR': 'XXRPZEUR',
                'XXBTZEUR': 'XBTEUR',
                'XETHZEUR': 'ETHEUR',
                'XXRPZEUR': 'XRPEUR',
            }
            alt = aliases.get(pair)
            if alt and alt in min_volumes:
                return float(min_volumes.get(alt, 0.0001))

            return 0.0001
        except Exception:
            return 0.0001

    def _calculate_volume(self, pair, price, available_eur=None):
        trade_amount_eur = self._get_trade_amount_eur()
        if available_eur is not None:
            trade_amount_eur = min(trade_amount_eur, max(0.0, available_eur))
        min_volume = self._get_min_volume(pair)
        if price <= 0:
            return 0.0
        calculated_volume = trade_amount_eur / price
        return max(calculated_volume, min_volume)

    def _fetch_valid_trade_pairs(self, requested_pairs):
        assets = self.api_client.get_asset_pairs()
        if not assets:
            self.logger.warning("Could not fetch AssetPairs; using configured pairs unchanged")
            return requested_pairs

        valid_requested = []
        seen = set()

        # Build flexible normalization index (ALTNAME, WSNAME, and slashless variants)
        pair_index = {}
        for key, meta in assets.items():
            alt = (meta.get('altname') or key or '').upper()
            ws = (meta.get('wsname') or '').upper()
            ws_noslash = ws.replace('/', '')
            key_u = (key or '').upper()
            for alias in [alt, ws, ws_noslash, key_u, alt.replace('/', '')]:
                if alias:
                    pair_index[alias] = alt

        for raw_pair in requested_pairs:
            pair = (raw_pair or '').upper()
            normalized = pair_index.get(pair) or pair_index.get(pair.replace('/', ''))
            if normalized:
                if normalized not in seen:
                    valid_requested.append(normalized)
                    seen.add(normalized)
                if pair != normalized:
                    normalization_key = f"{pair}->{normalized}"
                    if normalization_key not in self._normalized_pair_logs_seen:
                        self.logger.info(f"Pair normalized: {pair} -> {normalized}")
                        self._normalized_pair_logs_seen.add(normalization_key)
            else:
                self.logger.warning(f"Skipping unknown Kraken pair: {raw_pair}")
        self.kelly_fraction = self._calculate_kelly_fraction()

        if not valid_requested:
            self.logger.error("No valid trading pairs after Kraken validation")
        else:
            self.logger.info(f"Validated trading pairs: {valid_requested}")
        return valid_requested

    def reload_config(self):
        """Hot-reload config.toml and apply all changed settings without restarting.

        Called automatically every ``config_reload_interval`` seconds from the
        main loop.  Detects newly added trade pairs and initialises their state.
        Existing holdings and entry prices are preserved across reloads.
        Returns True on success, False if the config file cannot be parsed.
        """
        try:
            new_config = load_config(self.config_path)
            if not new_config:
                return False

            old_pairs = set(self.trade_pairs)
            self.config = new_config
            requested = self.config['bot_settings'].get('trade_pairs', ['XBTEUR'])
            self.trade_pairs = self._fetch_valid_trade_pairs(requested)
            new_pairs = set(self.trade_pairs)
            # Only initialise state for truly NEW pairs; preserve holdings/entry-prices for existing ones
            added_pairs = list(new_pairs - old_pairs)
            if added_pairs:
                self._init_pair_state(added_pairs)
            # Immediately reconcile live state so no stale holdings data lingers
            self._sync_account_state()

            self.target_balance_eur = self._get_target_balance()
            self.take_profit_percent = self._get_take_profit_percent()
            self.stop_loss_percent = self._get_stop_loss_percent()
            self.max_open_positions = int(self.config.get('risk_management', {}).get('max_open_positions', self.max_open_positions))
            self.trade_cooldown_sec = int(self.config.get('risk_management', {}).get('trade_cooldown_seconds', self.trade_cooldown_sec))
            self.global_trade_cooldown_sec = int(self.config.get('risk_management', {}).get('global_trade_cooldown_seconds', self.global_trade_cooldown_sec))
            self.trailing_stop_percent = float(self.config.get('risk_management', {}).get('trailing_stop_percent', self.trailing_stop_percent))
            self.empty_sell_log_cooldown_sec = int(self.config.get('risk_management', {}).get('empty_sell_log_cooldown_seconds', self.empty_sell_log_cooldown_sec))
            self.enable_regime_filter = bool(self.config.get('risk_management', {}).get('enable_regime_filter', self.enable_regime_filter))
            self.regime_benchmark_pair = str(self.config.get('risk_management', {}).get('regime_benchmark_pair', self.regime_benchmark_pair)).upper()
            self.regime_min_score = float(self.config.get('risk_management', {}).get('regime_min_score', self.regime_min_score))
            self.enable_hard_stop_loss = bool(self.config.get('risk_management', {}).get('enable_hard_stop_loss', self.enable_hard_stop_loss))
            self.hard_stop_loss_percent = float(self.config.get('risk_management', {}).get('hard_stop_loss_percent', self.hard_stop_loss_percent))
            self.enable_mtf_regime_scoring = bool(self.config.get('risk_management', {}).get('enable_mtf_regime_scoring', self.enable_mtf_regime_scoring))
            self.mtf_regime_min_score = float(self.config.get('risk_management', {}).get('mtf_regime_min_score', self.mtf_regime_min_score))
            self.enable_time_stop = bool(self.config.get('risk_management', {}).get('enable_time_stop', self.enable_time_stop))
            self.time_stop_hours = int(self.config.get('risk_management', {}).get('time_stop_hours', self.time_stop_hours))
            self.enable_daily_drawdown = bool(self.config.get('risk_management', {}).get('enable_daily_drawdown', self.enable_daily_drawdown))
            self.daily_drawdown_percent = float(self.config.get('risk_management', {}).get('daily_loss_limit_percent', self.daily_drawdown_percent))
            self.risk_off_allocation_multiplier = float(self.config.get('risk_management', {}).get('risk_off_allocation_multiplier', self.risk_off_allocation_multiplier))
            self.enable_volatility_targeting = bool(self.config.get('risk_management', {}).get('enable_volatility_targeting', self.enable_volatility_targeting))
            self.target_volatility_pct = float(self.config.get('risk_management', {}).get('target_volatility_pct', self.target_volatility_pct))
            self.max_consecutive_losses = int(self.config.get('risk_management', {}).get('max_consecutive_losses', self.max_consecutive_losses))
            self.pause_after_loss_streak_minutes = int(self.config.get('risk_management', {}).get('pause_after_loss_streak_minutes', self.pause_after_loss_streak_minutes))
            self.sell_fee_buffer_percent = float(self.config.get('risk_management', {}).get('sell_fee_buffer_percent', self.sell_fee_buffer_percent))
            # Refresh normalized fee fractions whenever config is reloaded
            try:
                self.fees_maker_frac = pct_to_frac(float(self.config.get('risk_management', {}).get('fees_maker_percent', self.fees_maker_percent)))
                self.fees_taker_frac = pct_to_frac(float(self.config.get('risk_management', {}).get('fees_taker_percent', self.fees_taker_percent)))
            except Exception:
                # keep previous values on error
                self.fees_maker_frac = getattr(self, 'fees_maker_frac', 0.0)
                self.fees_taker_frac = getattr(self, 'fees_taker_frac', 0.0)
            self.enable_sentiment_guard = bool(self.config.get('risk_management', {}).get('enable_sentiment_guard', self.enable_sentiment_guard))
            # Signal engine mode reload
            self.enable_mr_signals = bool(self.config.get('risk_management', {}).get('enable_mean_reversion_signals', self.enable_mr_signals))
            self.enable_trend_signals = bool(self.config.get('risk_management', {}).get('enable_trend_breakout_signals', self.enable_trend_signals))
            self.mr_rsi_oversold = float(self.config.get('risk_management', {}).get('mr_rsi_oversold_threshold', self.mr_rsi_oversold))
            self.mr_rsi_overbought = float(self.config.get('risk_management', {}).get('mr_rsi_overbought_threshold', self.mr_rsi_overbought))
            self.analysis_tool.enable_mr_signals = self.enable_mr_signals
            self.analysis_tool.enable_trend_signals = self.enable_trend_signals
            self.analysis_tool.mr_rsi_buy = self.mr_rsi_oversold
            self.analysis_tool.mr_rsi_sell = self.mr_rsi_overbought
            # ATR + pyramiding reload
            self.enable_atr_stop = bool(self.config.get('risk_management', {}).get('enable_atr_stop', self.enable_atr_stop))
            self.atr_period = int(self.config.get('risk_management', {}).get('atr_period', self.atr_period))
            self.atr_multiplier = float(self.config.get('risk_management', {}).get('atr_multiplier', self.atr_multiplier))
            self.atr_trail_multiplier = float(self.config.get('risk_management', {}).get('atr_trail_multiplier', self.atr_trail_multiplier))
            self.enable_atr_dynamic_tp = bool(self.config.get('risk_management', {}).get('enable_atr_dynamic_tp', self.enable_atr_dynamic_tp))
            self.atr_tp_multiplier = float(self.config.get('risk_management', {}).get('atr_tp_multiplier', self.atr_tp_multiplier))
            self.enable_break_even = bool(self.config.get('risk_management', {}).get('enable_break_even', self.enable_break_even))
            self.break_even_trigger_pct = float(self.config.get('risk_management', {}).get('break_even_trigger_percent', self.break_even_trigger_pct))
            self.enable_pyramiding = bool(self.config.get('risk_management', {}).get('enable_pyramiding', self.enable_pyramiding))
            self.pyramiding_add_pct = float(self.config.get('risk_management', {}).get('pyramiding_add_pct', self.pyramiding_add_pct))

            if old_pairs != new_pairs:
                self.logger.info(f"CONFIG RELOAD: trade_pairs changed {sorted(old_pairs)} -> {sorted(new_pairs)}")

            # Bear Shield reload
            bear_cfg = self.config.get('bear_shield', {})
            self.enable_bear_shield = bool(bear_cfg.get('enable_bear_shield', self.enable_bear_shield))
            self.bear_ema_period = int(bear_cfg.get('bear_ema_period', self.bear_ema_period))
            self.bear_confirm_candles = int(bear_cfg.get('bear_confirm_candles', self.bear_confirm_candles))
            self.bear_benchmark_pair = str(bear_cfg.get('bear_benchmark_pair', self.bear_benchmark_pair)).upper()
            self.bear_log_interval_minutes = int(bear_cfg.get('bear_log_interval_minutes', self.bear_log_interval_minutes))

            tech_cfg = self.config.get('technical', {})
            self.enable_ema_crossover_filter = bool(tech_cfg.get('enable_ema_crossover_filter', self.enable_ema_crossover_filter))
            self.ema_fast_period = int(tech_cfg.get('ema_fast_period', self.ema_fast_period))
            self.ema_slow_period = int(tech_cfg.get('ema_slow_period', self.ema_slow_period))
            self.enable_mtf_macd_filter = bool(tech_cfg.get('enable_mtf_macd_filter', self.enable_mtf_macd_filter))
            self.enable_partial_exit = bool(tech_cfg.get('enable_partial_exit', self.enable_partial_exit))
            self.partial_exit_trigger_pct = float(tech_cfg.get('partial_exit_trigger_pct', self.partial_exit_trigger_pct))
            self.partial_exit_fraction = float(tech_cfg.get('partial_exit_fraction', self.partial_exit_fraction))
            self.partial_exit_min_remaining_eur = float(tech_cfg.get('partial_exit_min_remaining_eur', self.partial_exit_min_remaining_eur))

            self.last_config_reload = datetime.now()
            self.loop_interval_sec = int(self.config.get('bot_settings', {}).get('loop_interval_seconds', self.loop_interval_sec))
            return True
        except Exception as e:
            self.logger.error(f"Error reloading config: {e}")
            return False

    def get_eur_balance(self):
        """Return current EUR (ZEUR) balance from Kraken; returns 0.0 on error."""
        try:
            balance = self.api_client.get_account_balance()
            if balance:
                return float(balance.get('ZEUR', 0))
            return 0.0
        except Exception as e:
            self.logger.error(f"Error getting EUR balance: {e}")
            return 0.0

    def get_crypto_holdings(self):
        """Refresh ``self.holdings`` dict from Kraken account balance.

        Maps Kraken asset codes (e.g. 'XXBT') back to our pair keys
        (e.g. 'XBTEUR').  Only updates pairs listed in ``self.trade_pairs``.

        In paper mode, skip reading real Kraken balances — the paper positions
        are tracked via position_qty / purchase_prices_paper.json. Reading real
        balances in paper mode causes the bot to see real BTC/ETH as open longs
        and blocks new paper trades.
        """
        if getattr(self.api_client, 'paper_mode', False):
            return
        try:
            balance = self.api_client.get_account_balance()
            if not balance:
                return

            pair_to_balance = {
                'XBTEUR': 'XXBT', 'XXBTZEUR': 'XXBT',
                'ETHEUR': 'XETH', 'XETHZEUR': 'XETH',
                'SOLEUR': 'SOL',
                'ADAEUR': 'ADA',
                'DOTEUR': 'DOT',
                'XRPEUR': 'XXRP', 'XXRPZEUR': 'XXRP',
                'LINKEUR': 'LINK',
                'MATICEUR': 'MATIC',
                'POLEUR': 'POL'
            }
            for pair in self.trade_pairs:
                key = pair_to_balance.get(pair)
                if not key:
                    continue
                self.holdings[pair] = float(balance.get(key, 0))
        except Exception as e:
            self.logger.error(f"Error getting holdings: {e}")

    def _reconcile_open_orders(self):
        """Compare open orders on Kraken with local position state at startup.

        Detects 'orphaned' orders that exist on Kraken but are not reflected
        locally (e.g. bot died between placing an order and updating state).
        Logs a warning so the operator can decide to cancel manually if needed.
        """
        try:
            open_orders_result = self.api_client.get_open_orders()
            if not open_orders_result:
                return
            open_map = open_orders_result.get('open', open_orders_result) if isinstance(open_orders_result, dict) else {}
            if not open_map:
                return

            watched = set(self.trade_pairs)
            # Build alias map so we can match Kraken pair names to our normalised pairs
            pair_aliases = {
                'XXBTZEUR': 'XBTEUR', 'XBTEUR': 'XBTEUR',
                'XETHZEUR': 'ETHEUR', 'ETHEUR': 'ETHEUR',
                'SOLEUR': 'SOLEUR', 'ADAEUR': 'ADAEUR',
                'DOTEUR': 'DOTEUR',
                'XXRPZEUR': 'XRPEUR', 'XRPEUR': 'XRPEUR',
                'LINKEUR': 'LINKEUR',
            }

            for txid, order in open_map.items():
                raw_pair = str(order.get('descr', {}).get('pair', '') or order.get('pair', '')).upper()
                norm_pair = pair_aliases.get(raw_pair, raw_pair)
                if norm_pair not in watched:
                    continue
                side = str(order.get('descr', {}).get('type', '') or '').lower()
                vol = float(order.get('vol', 0) or 0)
                local_holding = self.holdings.get(norm_pair, 0.0)
                local_short = self.short_qty.get(norm_pair, 0.0)

                # Check for mismatches
                if side == 'buy' and local_holding < self._get_min_volume(norm_pair):
                    self.logger.warning(
                        f"RECONCILE: Open BUY order {txid} ({vol:.6f} {norm_pair}) exists on Kraken "
                        f"but local holdings={local_holding:.8f}. Bot may have crashed before state update."
                    )
                elif side == 'sell' and local_short <= 0 and local_holding < self._get_min_volume(norm_pair):
                    self.logger.warning(
                        f"RECONCILE: Open SELL order {txid} ({vol:.6f} {norm_pair}) exists on Kraken "
                        f"but no local long/short position found."
                    )

            self.logger.info(f"Order reconciliation complete. {len(open_map)} open order(s) checked.")
        except Exception as e:
            self.logger.error(f"Order reconciliation failed: {e}", exc_info=True)

    def _sync_account_state(self, force_history: bool = False):
        """Refresh local holdings and purchase-price state from the Kraken API.

        Called after every trade and at startup.  When ``force_history=True``
        (post-trade or on first boot) it bypasses the 10-minute cache and
        re-fetches the full trade history from Kraken / NAS to recompute the
        average entry price.

        Also invalidates the balance cache on force calls so the post-trade sync
        always sees the account state after the fill, not cached pre-trade data.
        """
        if force_history:
            try:
                self.api_client.invalidate_balance_cache()
            except Exception:
                pass
        self.get_crypto_holdings()
        # In paper mode: if the positions file is missing, auto-clear any stale
        # PostgreSQL paper positions so a deliberate reset is fully clean.
        _is_paper = getattr(self.api_client, 'paper_mode', False)
        if _is_paper and not os.path.exists(self.data_purchase_prices_path):
            if _PG_AVAILABLE:
                try:
                    _pg.clear_positions(mode='paper')
                    self.logger.info("Paper positions file absent — cleared stale PostgreSQL paper positions.")
                except Exception as _pg_exc:
                    self.logger.debug("Could not auto-clear PG paper positions: %s", _pg_exc)

        # Load persisted purchase prices first (higher priority than history)
        try:
            persisted = {}
            if os.path.exists(self.data_purchase_prices_path):
                with open(self.data_purchase_prices_path, 'r', encoding='utf-8') as pf:
                    try:
                        persisted = json.load(pf)
                    except Exception:
                        persisted = {}
            for p, meta in (persisted or {}).items():
                try:
                    side = meta.get('side', 'long')
                    if side == 'short':
                        # Route short positions into the short-tracking dicts.
                        # Previously these were loaded into purchase_prices/
                        # position_qty (the LONG dicts) regardless of side,
                        # which meant every open short was silently forgotten
                        # on restart (short_qty stayed empty -> duplicate-short
                        # guard never triggered) AND could be misread as an
                        # open long position with no corresponding buy.
                        self.short_qty[p] = float(meta.get('qty', 0.0) or 0.0)
                        self.short_entry_prices[p] = float(meta.get('entry_price_eur', 0.0) or 0.0)
                        self.entry_timestamps[p] = int(meta.get('entry_ts', 0) or 0)
                        self.fees_paid[p] = float(meta.get('fees_eur', 0.0) or 0.0)
                    else:
                        self.purchase_prices[p] = float(meta.get('entry_price_eur', 0.0) or 0.0)
                        self.position_qty[p] = float(meta.get('qty', 0.0) or 0.0)
                        self.entry_timestamps[p] = int(meta.get('entry_ts', 0) or 0)
                        self.fees_paid[p] = float(meta.get('fees_eur', 0.0) or 0.0)
                except Exception:
                    self.logger.warning(f"Could not parse persisted purchase meta for {p} in {self.data_purchase_prices_path}")
        except Exception:
            pass
        # Fall back to history replay if no persisted file or force requested
        self.load_purchase_prices_from_history(force=force_history)
        # Ensure all keys exist
        for p in list(self.purchase_prices.keys()):
            self.fees_paid.setdefault(p, 0.0)
            self.position_qty.setdefault(p, 0.0)
            self.entry_timestamps.setdefault(p, None)

    def _place_live_order(self, pair, direction, volume, price=None, leverage=None, post_only=False, reduce_only=False):
        """Place a live order using the configured execution path.

        If limit fallback is enabled, wait for the order to fill (or be
        cancelled + replaced with market on timeout) before returning. This
        prevents the bot from treating a merely accepted post-only order as an
        executed trade, which otherwise can cause duplicate SELLs / phantom
        drawdown when funds are temporarily reserved.
        """
        exec_cfg = self.config.get('execution', {}) if isinstance(self.config, dict) else {}
        use_fallback = bool(exec_cfg.get('enable_live_limit_fallback', True))
        timeout_sec = int(exec_cfg.get('limit_fallback_timeout_sec', 30))

        if use_fallback:
            return self.api_client.place_order_with_fallback(
                pair=pair,
                direction=direction,
                volume=volume,
                price=price,
                leverage=leverage,
                post_only=post_only,
                reduce_only=reduce_only,
                timeout_sec=timeout_sec,
            )

        return self.api_client.place_order(
            pair=pair,
            direction=direction,
            volume=volume,
            price=price,
            leverage=leverage,
            post_only=post_only,
            reduce_only=reduce_only,
        )

    def _get_open_orders_snapshot(self):
        """Return normalized open-order metadata keyed by Kraken txid.

        Normalizes pair aliases and computes remaining volume so callers can
        reason about pending orders without duplicating Kraken response parsing.
        """
        try:
            open_orders_result = self.api_client.get_open_orders()
            if not open_orders_result:
                return {}

            open_map = open_orders_result.get('open', open_orders_result) if isinstance(open_orders_result, dict) else {}
            if not isinstance(open_map, dict) or not open_map:
                return {}

            pair_aliases = {
                'XXBTZEUR': 'XBTEUR', 'XBTEUR': 'XBTEUR',
                'XETHZEUR': 'ETHEUR', 'ETHEUR': 'ETHEUR',
                'SOLEUR': 'SOLEUR',
                'ADAEUR': 'ADAEUR',
                'DOTEUR': 'DOTEUR',
                'XXRPZEUR': 'XRPEUR', 'XRPEUR': 'XRPEUR',
                'LINKEUR': 'LINKEUR',
                'MATICEUR': 'MATICEUR',
                'POLEUR': 'POLEUR',
            }

            normalized = {}
            for txid, order in open_map.items():
                descr = order.get('descr', {}) if isinstance(order, dict) else {}
                side = str(descr.get('type', '') or order.get('type', '') or '').lower()
                raw_pair = str(descr.get('pair', '') or order.get('pair', '') or '').upper()
                norm_pair = pair_aliases.get(raw_pair, raw_pair)
                try:
                    vol = float(order.get('vol', 0) or 0)
                    vol_exec = float(order.get('vol_exec', 0) or 0)
                    remaining_vol = max(0.0, vol - vol_exec)
                except Exception:
                    remaining_vol = 0.0

                price_raw = descr.get('price', None)
                if price_raw in (None, '', '0', 0):
                    price_raw = order.get('price', 0)
                try:
                    limit_price = float(price_raw or 0)
                except Exception:
                    limit_price = 0.0

                normalized[txid] = {
                    'pair': norm_pair,
                    'side': side,
                    'remaining_vol': remaining_vol,
                    'limit_price': limit_price,
                    'raw': order,
                }
            return normalized
        except Exception as e:
            self.logger.debug(f"Could not load open-order snapshot: {e}")
            return {}

    def _has_open_order(self, pair, side) -> bool:
        """Return True when there is already a pending order for pair+side."""
        try:
            for _, meta in self._get_open_orders_snapshot().items():
                if meta.get('pair') == pair and meta.get('side') == side and float(meta.get('remaining_vol', 0.0)) > 0:
                    return True
            return False
        except Exception:
            return False

    def _estimate_open_buy_reserve_eur(self) -> float:
        """Best-effort estimate of EUR currently reserved in open BUY orders.

        Kraken's free EUR balance can drop as soon as a post-only BUY is placed,
        even before the trade is filled and before crypto holdings appear.
        Without adding this reserve back, the bot can misread a normal pending
        entry as a large portfolio drawdown on a small account.
        """
        try:
            reserved_eur = 0.0
            for _, meta in self._get_open_orders_snapshot().items():
                if meta.get('side') != 'buy':
                    continue
                remaining_vol = float(meta.get('remaining_vol', 0.0))
                limit_price = float(meta.get('limit_price', 0.0))
                if remaining_vol > 0 and limit_price > 0:
                    reserved_eur += remaining_vol * limit_price
            return reserved_eur
        except Exception as e:
            self.logger.debug(f"Could not estimate reserved BUY EUR from open orders: {e}")
            return 0.0

    def _load_trade_history_from_nas(self, year: int) -> dict:
        """Load persisted trade history from NAS JSON file. Returns {} if unavailable."""
        path = self.nas_root / str(year) / 'trade_history' / f'trades_{year}.json'
        try:
            if path.exists():
                with open(path, 'r') as f:
                    data = json.load(f)
                self.logger.info(f"Loaded {len(data)} trades from NAS cache ({path.name})")
                return data
        except Exception as e:
            self.logger.warning(f"Could not load NAS trade history ({path}): {e}")
        return {}

    def _save_trade_history_to_nas(self, trades: dict, year: int) -> None:
        """Persist trade history to NAS JSON file for future incremental loads."""
        try:
            trade_history_dir = self.nas_root / str(year) / 'trade_history'
            trade_history_dir.mkdir(parents=True, exist_ok=True)
            path = trade_history_dir / f'trades_{year}.json'
            with open(path, 'w') as f:
                json.dump(trades, f, separators=(',', ':'))
            self.logger.debug(f"Saved {len(trades)} trades to NAS cache ({path.name})")
        except Exception as e:
            self.logger.warning(f"Could not save trade history to NAS ({e}) â€” NAS mounted?")

    def _refresh_trade_history_cache(self, force: bool = False) -> None:
        """Fetch trade history from Kraken API and merge into in-memory + NAS cache.

        Uses TTL: only fetches if cache is older than _TRADE_HISTORY_REFRESH_INTERVAL seconds.
        Always fetches after a trade (force=True).
        Incremental: only requests trades newer than the last cached entry.
        """
        now = time.time()
        if not force and (now - self._trade_history_last_fetch) < _TRADE_HISTORY_REFRESH_INTERVAL:
            return

        year = datetime.now(tz=timezone.utc).year
        year_start_ts = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())

        # Bootstrap from NAS on first run (cache is empty)
        if not self._trade_history_cache:
            self._trade_history_cache = self._load_trade_history_from_nas(year)

        # Only fetch trades newer than the latest entry we already have
        if self._trade_history_cache:
            last_ts = max(float(t.get('time', 0)) for t in self._trade_history_cache.values())
            fetch_start = max(year_start_ts, int(last_ts))
        else:
            fetch_start = year_start_ts

        new_trades = self.api_client.get_trade_history(start=fetch_start, fetch_all=True)
        if new_trades:
            self._trade_history_cache.update(new_trades)
            self._save_trade_history_to_nas(self._trade_history_cache, year)

        self._trade_history_last_fetch = now
        self.logger.debug(
            f"Trade history cache refreshed: {len(self._trade_history_cache)} total trades "
            f"(+{len(new_trades) if new_trades else 0} new, start={fetch_start})"
        )

    def load_purchase_prices_from_history(self, force: bool = False):
        """Rebuild per-pair average entry price + realized PnL from Kraken trade history.

        Logic:
        - BUY increases position size and weighted average entry (including fees)
        - SELL reduces position and realizes PnL (net of fees)

        Uses an in-memory + NAS cache to avoid hitting the Kraken API on every loop iteration.
        Pass force=True immediately after a trade to ensure fresh data.
        """
        try:
            self._refresh_trade_history_cache(force=force)
            trades = self._trade_history_cache
            if not trades:
                return

            watched = set(self.trade_pairs)
            pair_aliases = {
                'XXBTZEUR': 'XBTEUR', 'XBTEUR': 'XBTEUR',
                'XETHZEUR': 'ETHEUR', 'ETHEUR': 'ETHEUR',
                'SOLEUR': 'SOLEUR',
                'ADAEUR': 'ADAEUR',
                'DOTEUR': 'DOTEUR',
                'XXRPZEUR': 'XRPEUR', 'XRPEUR': 'XRPEUR',
                'LINKEUR': 'LINKEUR',
                'MATICEUR': 'MATICEUR',
                'POLEUR': 'POLEUR'
            }

            # Reset state before replay
            for pair in watched:
                self.position_qty[pair] = 0.0
                self.purchase_prices[pair] = 0.0
                self.realized_pnl[pair] = 0.0
                self.fees_paid[pair] = 0.0

            sorted_trades = sorted(trades.values(), key=lambda t: float(t.get('time', 0)))
            history_trade_count = 0

            for trade in sorted_trades:
                raw_pair = trade.get('pair', '')
                pair = pair_aliases.get(raw_pair, raw_pair)
                if pair not in watched:
                    continue

                ttype = trade.get('type', '').lower()
                vol = float(trade.get('vol', 0) or 0)
                cost = float(trade.get('cost', 0) or 0)  # quote currency (EUR)
                fee = float(trade.get('fee', 0) or 0)
                if vol <= 0:
                    continue

                self.fees_paid[pair] += fee
                qty = self.position_qty.get(pair, 0.0)
                avg = self.purchase_prices.get(pair, 0.0)

                if ttype == 'buy':
                    history_trade_count += 1
                    total_cost = cost + fee
                    new_qty = qty + vol
                    if new_qty > 0:
                        new_avg = ((avg * qty) + total_cost) / new_qty
                    else:
                        new_avg = 0.0
                    self.position_qty[pair] = new_qty
                    self.purchase_prices[pair] = new_avg
                    self.peak_prices[pair] = max(self.peak_prices.get(pair, 0.0), new_avg)

                elif ttype == 'sell':
                    history_trade_count += 1
                    sell_qty = min(qty, vol)
                    proceeds_net = cost - fee
                    if sell_qty > 0 and avg > 0:
                        cost_basis = avg * sell_qty
                        self.realized_pnl[pair] += (proceeds_net - cost_basis)
                    remaining_qty = max(0.0, qty - sell_qty)
                    self.position_qty[pair] = remaining_qty
                    if remaining_qty <= self._get_min_volume(pair):
                        self.purchase_prices[pair] = 0.0
                        self.peak_prices[pair] = 0.0

            # Keep displayed trade counter consistent across restarts (history + new trades)
            if history_trade_count > 0:
                self.trade_count = history_trade_count

            # Reconcile with live holdings from balance (source of truth for quantity)
            for pair in watched:
                live_qty = self.holdings.get(pair, 0.0)
                history_qty = self.position_qty.get(pair, 0.0)
                self.position_qty[pair] = live_qty
                min_vol = self._get_min_volume(pair)
                # Use a small grace margin (5%) so a position at exactly min_volume
                # is NOT treated as empty and does not lose its entry price.
                if live_qty < min_vol * 0.95:
                    self.purchase_prices[pair] = 0.0
                    self.peak_prices[pair] = 0.0
                    self.entry_timestamps[pair] = None
                elif self.purchase_prices.get(pair, 0.0) <= 0.0:
                    # Position exists but entry price is unknown (e.g. after a crash-restart)
                    self.logger.warning(
                        f"Position {pair} exists ({live_qty:.8f}) but entry price is unknown! "
                        f"TP/SL calculations may be inaccurate until next history replay."
                    )
                    if self.entry_timestamps.get(pair) is None:
                        self.entry_timestamps[pair] = int(time.time())
                else:
                    # Detect phantom historical positions: if history qty >> live qty by >10%,
                    # the VWAP is contaminated by old sessions. Fall back to most recent buy.
                    # rate-limit phantom checks to once per 60s per pair to avoid API limits
                    last = self._phantom_last_checked.get(pair, 0)
                    if time.time() - last < 60:
                        continue
                    if history_qty > live_qty * 1.10 and live_qty >= min_vol * 0.95:
                        # Find most recent BUY for this pair in history
                        recent_buy = next(
                            (t for t in reversed(sorted_trades)
                             if pair_aliases.get(t.get('pair', ''), t.get('pair', '')) == pair
                             and t.get('type', '').lower() == 'buy'),
                            None
                        )
                        if recent_buy:
                            rc = float(recent_buy.get('cost', 0))
                            rv = float(recent_buy.get('vol', 1)) or 1.0
                            rf = float(recent_buy.get('fee', 0))
                            corrected = (rc + rf) / rv
                            # Use the MOST RECENT BUY price as authoritative; log that concisely
                            self.logger.warning(
                                f"purchase_prices[{pair}]: last_buy={corrected:.5f} EUR | live_qty={live_qty:.4f}"
                            )
                            # persist the corrected (most-recent-buy) entry price
                            self.purchase_prices[pair] = corrected
                            # mark check time to rate-limit repeated API hits
                            self._phantom_last_checked[pair] = int(time.time())
                    if self.entry_timestamps.get(pair) is None:
                        self.entry_timestamps[pair] = int(time.time())

        except Exception as e:
            self.logger.error(f"Error loading last purchase prices: {e}")

    def _resolve_benchmark_history(self):
        bench = self.regime_benchmark_pair
        aliases = [bench, bench.replace('/', '')]
        if bench == 'XBTEUR':
            aliases += ['XXBTZEUR']
        if bench == 'ETHEUR':
            aliases += ['XETHZEUR']
        for key in aliases:
            history = self.analysis_tool.pair_price_history.get(key)
            if history:
                return list(history)
        return []

    def _compute_mtf_regime_score(self):
        prices = self._resolve_benchmark_history()
        if len(prices) < 80:
            return None

        def _safe_rsi(window):
            val = self.analysis_tool.calculate_rsi(window)
            return 50.0 if val is None else float(val)

        rsi_fast = _safe_rsi(prices[-25:])
        rsi_mid = _safe_rsi(prices[-35:])
        rsi_slow = _safe_rsi(prices[-80:])

        sma10 = sum(prices[-10:]) / 10.0
        sma30 = sum(prices[-30:]) / 30.0
        sma70 = sum(prices[-70:]) / 70.0

        trend = (((sma10 - sma30) / sma30) * 100.0) * 0.9 + (((sma30 - sma70) / sma70) * 100.0) * 1.2
        momentum = ((rsi_fast - 50.0) * 0.4) + ((rsi_mid - 50.0) * 0.35) + ((rsi_slow - 50.0) * 0.25)

        recent = prices[-24:]
        mean = sum(recent) / len(recent)
        vol_pct = 0.0
        if mean > 0:
            variance = sum((p - mean) ** 2 for p in recent) / len(recent)
            vol_pct = ((variance ** 0.5) / mean) * 100.0
        vol_penalty = max(0.0, vol_pct - 2.2) * 1.5

        return trend + momentum - vol_penalty

    def _is_risk_on_regime(self):
        """Return True when the market regime is considered bullish (RISK_ON).

        When ``enable_mtf_regime_scoring`` is on, uses the composite MTF score
        (short/long SMA + RSI across multiple timeframes) for the BTC benchmark.
        Falls back to the simpler pair-score comparison against ``regime_min_score``.
        Always returns True when the regime filter is disabled in config.
        """
        if not self.enable_regime_filter:
            return True

        if self.enable_mtf_regime_scoring:
            mtf_score = self._compute_mtf_regime_score()
            if mtf_score is not None:
                return mtf_score >= self.mtf_regime_min_score

        benchmark = self.regime_benchmark_pair
        score = float(self.pair_scores.get(benchmark, 0.0))
        return score >= self.regime_min_score

    def _benchmark_volatility_pct(self):
        bench = self.regime_benchmark_pair
        aliases = [bench, bench.replace('/', '')]
        # analysis stores histories by raw Kraken key seen in ticker payload
        if bench == 'XBTEUR':
            aliases += ['XXBTZEUR']
        if bench == 'ETHEUR':
            aliases += ['XETHZEUR']

        try:
            history = None
            for key in aliases:
                history = self.analysis_tool.pair_price_history.get(key)
                if history and len(history) >= 20:
                    break
            if not history or len(history) < 20:
                return 0.0
            prices = list(history)[-20:]
            mean = sum(prices) / len(prices)
            if mean <= 0:
                return 0.0
            variance = sum((p - mean) ** 2 for p in prices) / len(prices)
            return ((variance ** 0.5) / mean) * 100.0
        except Exception:
            return 0.0

    def _allocation_multiplier(self):
        """Return a [0.2, 1.25] multiplier applied to every order size.

        Combines two scaling factors:
        - Regime factor: 1.0 in RISK_ON, ``risk_off_allocation_multiplier``
          (default 0.5) in RISK_OFF â€” cuts size in half during bear markets.
        - Volatility factor: ``target_volatility_pct / current_vol``; reduces
          size when BTC volatility is elevated above the target (default 1.6%).

        The product is clamped to [0.2, 1.25] so orders never vanish entirely
        or exceed 125 % of base size.
        """
        base = 1.0 if self._is_risk_on_regime() else self.risk_off_allocation_multiplier
        if not self.enable_volatility_targeting:
            return base
        vol = self._benchmark_volatility_pct()
        if vol <= 0:
            return base
        # Higher volatility -> smaller size, lower volatility -> allow base size
        vol_scale = min(1.25, max(0.35, self.target_volatility_pct / vol))
        return max(0.2, min(1.25, base * vol_scale))

    def _is_trading_hours(self):
        """Returns True if current UTC hour is within the configured trading window."""
        if not self.enable_trading_hours:
            return True
        hour = datetime.now(timezone.utc).hour
        start = self.trading_hours_start_utc
        end = self.trading_hours_end_utc
        if start < end:
            return start <= hour < end
        # Overnight window support (e.g. 22:00â€“06:00)
        return hour >= start or hour < end

    def _has_sufficient_volume(self, pair):
        """Returns True if the latest 15m candle volume is >= min_ratio Ã— 20-candle average.
        Uses a 5-minute cache to avoid redundant API calls.
        """
        if not self.enable_volume_filter:
            return True
        try:
            cached = self._volume_cache.get(pair)
            if cached and (time.time() - cached[0]) < 300:
                return cached[1] >= self.volume_filter_min_ratio

            ohlc = self.api_client.get_ohlc_data(pair, interval=15)
            if not ohlc:
                return False
            data_key = next((k for k in ohlc if k != 'last'), None)
            if not data_key:
                return False
            rows = ohlc[data_key]
            if len(rows) < 3:
                return False
            volumes = [float(row[6]) for row in rows]
            window = volumes[-20:] if len(volumes) >= 20 else volumes
            avg_vol = sum(window) / len(window)
            current_vol = volumes[-1]
            ratio = current_vol / avg_vol if avg_vol > 0 else 1.0
            self._volume_cache[pair] = (time.time(), ratio)
            if ratio < self.volume_filter_min_ratio:
                self.logger.info(
                    f"BUY skipped for {pair}: low volume (ratio {ratio:.2f} < {self.volume_filter_min_ratio})"
                )
                return False
            return True
        except Exception as e:
            self.logger.warning(f"Volume check failed for {pair}: {e}")
            return False  # fail-closed: block trades when we cannot verify volume

    def _is_temporarily_paused(self):
        """Return True while the bot is in a loss-streak or drawdown cooldown period.

        Additionally, a manual kill-switch file (self.kill_switch_path) pauses buys
        when present. This allows an operator to quickly disable new entries by
        creating the PAUSE file in the project directory.
        """
        try:
            # Manual kill-switch: presence of PAUSE file disables buys immediately
            if getattr(self, 'kill_switch_path', None) and os.path.exists(self.kill_switch_path):
                try:
                    self.logger.info("Manual PAUSE file detected; buys are paused until file is removed.")
                except Exception:
                    pass
                return True
        except Exception as e:
            # Non-fatal; if check fails, fall back to time-based pause only
            try:
                self.logger.debug(f"Could not check kill switch file: {e}")
            except Exception:
                pass
        return time.time() < getattr(self, 'trading_paused_until_ts', 0)

    def _available_eur_for_buy(self):
        """Return spendable EUR after reserving 1.5 % for fees and slippage."""
        # SMART FEE RESERVE: leave 1.5% for fees and slippage to avoid 'Insufficient funds'
        return max(0.0, self.get_eur_balance() * 0.985)

    def _daily_drawdown_hit(self):
        # If disabled via config, never trigger the daily drawdown circuit
        # Compute drawdown against the full portfolio value (cash + holdings + reserved buys)
        if not getattr(self, 'enable_daily_drawdown', True):
            return False

        # Build current portfolio snapshot in a best-effort way
        try:
            current_cash = self.get_eur_balance()
            holdings_value = sum(
                float(self.holdings.get(p, 0.0)) * float(self.pair_prices.get(p, 0.0))
                for p in self.trade_pairs
            )
            reserved_buy_eur = 0.0
            try:
                reserved_buy_eur = float(self._estimate_open_buy_reserve_eur())
            except Exception:
                reserved_buy_eur = 0.0
            portfolio_value = current_cash + holdings_value + reserved_buy_eur
        except Exception:
            # Fail-safe: fall back to EUR cash only
            portfolio_value = float(self.get_eur_balance())

        # Initialise daily baseline on first call
        if self.daily_start_balance is None:
            self.daily_start_balance = portfolio_value
            return False
        if self.daily_start_balance <= 0:
            return False

        # Percentage drawdown relative to the daily baseline
        dd = ((self.daily_start_balance - portfolio_value) / self.daily_start_balance) * 100.0

        # Absolute loss threshold bypass
        abs_loss = max(0.0, self.daily_start_balance - portfolio_value)
        min_abs_loss = float(self.config.get('risk_management', {}).get('daily_loss_min_eur', 0.0))

        if dd >= self.daily_drawdown_percent and abs_loss >= min_abs_loss:
            self.logger.warning(
                f"Daily drawdown limit reached (portfolio): {dd:.2f}% >= {self.daily_drawdown_percent:.2f}% (abs loss {abs_loss:.2f} EUR). Pausing buys."
            )
            return True
        return False

    def _maybe_refresh_fees(self):
        """Refresh Kraken fee schedule once per 24h."""
        if time.time() - self._fee_last_sync < 86400:
            return
        _data_dir = os.path.join(os.path.dirname(__file__), 'data')
        fresh = _fee_sync.load(_data_dir)
        self._fee_last_sync = time.time()
        new_taker = float(self.config.get('risk_management', {}).get(
            'fees_taker_percent', _fee_sync.base_taker(fresh)))
        new_maker = float(self.config.get('risk_management', {}).get(
            'fees_maker_percent', _fee_sync.base_maker(fresh)))
        if new_taker != self.fees_taker_percent or new_maker != self.fees_maker_percent:
            self.logger.info('Fee update: taker %.2f%% -> %.2f%% maker %.2f%% -> %.2f%%',
                             self.fees_taker_percent, new_taker,
                             self.fees_maker_percent, new_maker)
            self.fees_taker_percent = new_taker
            self.fees_maker_percent = new_maker
            self.fees_taker_frac = new_taker / 100.0
            self.fees_maker_frac = new_maker / 100.0
        self._fee_data = fresh

    def _maybe_send_daily_report(self):
        """Save the daily CSV report to data/reports/ once per day at the configured time."""
        from datetime import datetime as _dt, timezone as _tz
        now_utc = _dt.now(tz=_tz.utc)
        today   = now_utc.strftime("%Y-%m-%d")
        if self._report_last_sent_date == today:
            return
        try:
            target_h, target_m = (int(x) for x in self._report_time_utc.split(":"))
        except Exception:
            target_h, target_m = 9, 35
        if now_utc.hour != target_h or now_utc.minute > target_m + 5:
            return
        import threading as _thr
        def _worker():
            try:
                from core.daily_report import save_daily_report
            except ImportError:
                from daily_report import save_daily_report
            try:
                paper = getattr(self.api_client, 'paper_mode', False)
                path = save_daily_report(
                    data_dir  = os.path.join(os.path.dirname(__file__), 'data'),
                    paper_mode = paper,
                )
                self.logger.info("Daily report saved: %s", path)
                self._report_last_sent_date = today
            except Exception as exc:
                self.logger.error("Daily report save failed: %s", exc)
        _thr.Thread(target=_worker, daemon=True, name="DailyReport").start()

    def _refresh_cashflows_from_ledger(self, force=False):
        now_ts = int(time.time())
        if not force and (now_ts - self._last_cashflow_refresh_ts) < self.cashflow_refresh_interval_sec:
            return

        try:
            ledgers = self.api_client.get_ledgers(asset='ZEUR', start=self.start_timestamp, fetch_all=True)
            if not ledgers:
                self._last_cashflow_refresh_ts = now_ts
                return

            deposits = 0.0
            withdrawals = 0.0
            for entry in ledgers.values():
                ltype = str(entry.get('type', '')).lower()
                try:
                    amount = abs(float(entry.get('amount', 0) or 0))
                except Exception:
                    amount = 0.0

                if amount <= 0:
                    continue

                if ltype == 'deposit':
                    deposits += amount
                elif ltype == 'withdrawal':
                    withdrawals += amount

            self.net_deposits_eur = deposits
            self.net_withdrawals_eur = withdrawals
            self._last_cashflow_refresh_ts = now_ts
        except Exception as e:
            self.logger.error(f"Error refreshing cashflows from ledger: {e}")

    def _adjusted_reference_balance(self):
        base = self.initial_balance_eur if self.initial_balance_eur is not None else (self.daily_start_balance or 0.0)
        return base + self.net_deposits_eur - self.net_withdrawals_eur

    def _adjusted_pnl_eur(self, current_balance):
        return current_balance - self._adjusted_reference_balance()

    # ------------------------------------------------------------------
    # Persistent cumulative P&L â€” survives restarts
    # ------------------------------------------------------------------

    def _pnl_state_path(self) -> Path:
        """Return path to the persistent P&L state file."""
        return Path(__file__).parent / "data" / "pnl_state.json"

    def _cooldown_state_path(self) -> Path:
        return Path(__file__).parent / "data" / "cooldown_state.json"

    def _save_cooldown_state(self) -> None:
        """Persist last_trade_at and last_global_trade_at so cooldowns survive restarts."""
        try:
            state = {
                "last_global_trade_at": self.last_global_trade_at,
                "last_trade_at": self.last_trade_at,
            }
            path = self._cooldown_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state))
        except Exception as exc:
            self.logger.warning(f"Could not save cooldown state: {exc}")

    def _load_cooldown_state(self) -> None:
        """Restore cooldown timestamps from the last run so we don't retrade immediately."""
        path = self._cooldown_state_path()
        try:
            if not path.exists():
                return
            state = json.loads(path.read_text())
            self.last_global_trade_at = float(state.get("last_global_trade_at", 0))
            saved_pair_times = state.get("last_trade_at", {})
            for pair, ts in saved_pair_times.items():
                self.last_trade_at[pair] = float(ts)
            self.logger.info(
                f"Restored cooldown state: global_last={self.last_global_trade_at:.0f}, "
                f"pairs={list(saved_pair_times.keys())}"
            )
        except Exception as exc:
            self.logger.warning(f"Could not load cooldown state: {exc}")

    def _load_cumulative_pnl_state(self, current_balance: float) -> None:
        """Load or initialise the persistent P&L baseline.

        On the very first run (no state file) the current balance is stored
        as the all-time start.  On subsequent runs the stored ``start_eur``
        value is restored so cumulative P&L is always relative to the very
        first time the bot ran.
        """
        path = self._pnl_state_path()
        try:
            if path.exists():
                state = json.loads(path.read_text())
                self.cumulative_start_eur: float = float(state.get("start_eur", current_balance))
                self.logger.info(
                    f"Loaded P&L baseline: {self.cumulative_start_eur:.2f} EUR "
                    f"(started {state.get('created_at', 'unknown')})"
                )
            else:
                self.cumulative_start_eur = current_balance
                state = {
                    "start_eur": current_balance,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(state, indent=2))
                self.logger.info(f"Created P&L baseline: {current_balance:.2f} EUR")
        except Exception as exc:
            self.logger.warning(f"Could not load P&L state: {exc}")
            self.cumulative_start_eur = current_balance

    def cumulative_pnl_eur(self, current_balance: float) -> float:
        """Return total P&L since the bot was first ever started."""
        return current_balance - getattr(self, "cumulative_start_eur", current_balance)

    # ------------------------------------------------------------------
    # Persistent balance state — peak_balance + initial_balance survive restarts
    # ------------------------------------------------------------------

    def _balance_state_path(self) -> Path:
        return Path(__file__).parent / "data" / "balance_state.json"

    def _save_balance_state(self, portfolio_value: float) -> None:
        """Persist peak_balance, initial_balance_eur, and paper EUR cash so restarts resume correctly."""
        try:
            path = self._balance_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "peak_balance":        round(float(getattr(self, "peak_balance", portfolio_value)), 4),
                "initial_balance_eur": round(float(getattr(self, "initial_balance_eur", portfolio_value)), 4),
            }
            # Persist the live paper EUR cash so it survives restarts
            _is_paper = getattr(self.api_client, 'paper_mode', False)
            if _is_paper:
                state["paper_balance_eur"] = round(float(getattr(self.api_client, '_paper_balance_eur', 0.0)), 4)
            path.write_text(json.dumps(state))
        except Exception as exc:
            self.logger.warning("Could not save balance state: %s", exc)

    def _load_balance_state(self, fallback_balance: float) -> None:
        """Restore peak_balance, initial_balance_eur, and paper EUR cash from disk.

        On the very first run the fallback_balance (EUR cash) is used and saved.
        On subsequent restarts the persisted values are restored so the dashboard
        shows a smooth continuation rather than a jump, and the paper balance does
        not reset to the config default on every deploy.
        """
        path = self._balance_state_path()
        try:
            if path.exists():
                state = json.loads(path.read_text())
                self.peak_balance        = float(state.get("peak_balance",       fallback_balance))
                self.initial_balance_eur = float(state.get("initial_balance_eur",fallback_balance))
                # Restore paper EUR cash — prevents the ghost-money reset on every restart
                _is_paper = getattr(self.api_client, 'paper_mode', False)
                if _is_paper and "paper_balance_eur" in state:
                    restored_paper = float(state["paper_balance_eur"])
                    self.api_client._paper_balance_eur = restored_paper
                    self.logger.info(
                        "Restored balance state: peak=%.2f initial=%.2f paper_eur=%.2f",
                        self.peak_balance, self.initial_balance_eur, restored_paper,
                    )
                else:
                    self.logger.info(
                        "Restored balance state: peak=%.2f initial=%.2f",
                        self.peak_balance, self.initial_balance_eur,
                    )
            else:
                self.peak_balance        = fallback_balance
                self.initial_balance_eur = fallback_balance
                self._save_balance_state(fallback_balance)
                self.logger.info("Created balance state: initial=%.2f", fallback_balance)
        except Exception as exc:
            self.logger.warning("Could not load balance state: %s", exc)
            self.peak_balance        = fallback_balance
            self.initial_balance_eur = fallback_balance

    def _count_open_positions(self) -> int:
        """Return the number of pairs where holdings exceed the minimum tradeable volume.
        Uses position_qty as primary source â€” holdings is zeroed by _sync_account_state
        in paper mode and cannot be trusted for counting active positions."""
        return sum(
            1 for pair in self.trade_pairs
            if (self.position_qty.get(pair, 0.0) or self.holdings.get(pair, 0.0))
            >= self._get_min_volume(pair)
        )

    def _is_on_cooldown(self, pair):
        """Return True if the per-pair cooldown period has not yet elapsed since the last trade."""
        return (time.time() - self.last_trade_at.get(pair, 0)) < self.trade_cooldown_sec

    def _is_global_cooldown(self):
        """Return True if the global inter-trade cooldown has not yet elapsed."""
        return (time.time() - self.last_global_trade_at) < self.global_trade_cooldown_sec

    def _log_empty_sell_signal_throttled(self, pair):
        now_ts = time.time()
        last_ts = self._last_empty_sell_log_at.get(pair, 0)
        if (now_ts - last_ts) >= self.empty_sell_log_cooldown_sec:
            self.logger.info(f"SELL signal for {pair} but no holdings")
            self._last_empty_sell_log_at[pair] = now_ts

    def _profit_percent_from_entry(self, pair, current_price):
        entry = self.purchase_prices.get(pair, 0.0)
        if entry <= 0 or current_price <= 0:
            return None
        return ((current_price - entry) / entry) * 100.0

    def _last_closed_trade_net_profit_pct(self, pair):
        """Return the net profit percent of the last closed (BUY->SELL) roundtrip for *pair*.

        Delegates to utils.last_closed_trade_net_profit_pct which performs a
        locked read of the JSONL journal and normalizes fee inputs consistently.
        """
        try:
            return last_closed_trade_net_profit_pct(self.json_journal_path, pair, self.fees_maker_percent, self.fees_taker_percent)
        except Exception:
            return None

    def _compute_atr(self, pair, period=None):
        """Compute approximate ATR from stored price history (fallback to close diffs).
        Returns ATR in price units (EUR).
        """
        try:
            p = period if period is not None else self.atr_period
            # Prefer computing ATR from 1h OHLC when available (more robust than tick diffs)
            try:
                ohlc = self.api_client.get_ohlc_data(pair, interval=60)
                if ohlc:
                    data_key = next((k for k in ohlc if k != 'last'), None)
                    if data_key:
                        series = ohlc[data_key]
                        if len(series) >= p + 1:
                            trs = []
                            prev_close = float(series[0][4])
                            for row in series[-(p+1):]:
                                high = float(row[2])
                                low = float(row[3])
                                close = float(row[4])
                                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                                trs.append(tr)
                                prev_close = close
                            if trs:
                                return float(sum(trs[-p:]) / len(trs[-p:]))
            except Exception:
                pass

            # Fallback: compute ATR from internal price history close diffs
            try:
                import numpy as _np
                history = list(self.analysis_tool.pair_price_history.get(pair, []))
                if not history or len(history) < 2:
                    return None
                prices = _np.array(history)
                tr = _np.abs(_np.diff(prices))
                if len(tr) < p:
                    return float(_np.mean(tr)) if len(tr) > 0 else None
                return float(_np.mean(tr[-p:]))
            except Exception:
                return None
        except Exception:
            return None

    def _dynamic_stop_loss_percent(self) -> float:
        """
        Regime-aware SL.
        Bearish → cut losses faster (0.6%).
        Bullish → give position room to breathe (1.2%).
        Fine-tuned by AI intelligence score.
        """
        regime = getattr(self, '_current_market_regime', 'RANGING')
        intel  = getattr(self, '_intelligence_score', 0.0)

        if regime == 'TRENDING_DOWN':
            sl = 0.6
        elif regime == 'TRENDING_UP':
            sl = 1.2
        else:
            sl = float(self.stop_loss_percent)  # config base (0.8%)

        # AI fine-tune: ±0.1% nudge on strong signals
        if intel < -2:
            sl = max(sl - 0.1, 0.5)   # extra bearish → tighter SL
        elif intel > 2:
            sl = min(sl + 0.1, 1.5)   # extra bullish → more room

        return round(sl, 2)

    def _dynamic_take_profit_percent(self) -> float:
        """
        Regime-aware TP target.
        Bearish market → take smaller wins quickly.
        Bullish market → let profits run further.
        Fine-tuned by the AI intelligence score.
        """
        regime  = getattr(self, '_current_market_regime', 'RANGING')
        intel   = getattr(self, '_intelligence_score', 0.0)

        if regime == 'TRENDING_UP':
            tp = 3.0
        elif regime == 'TRENDING_DOWN':
            tp = 1.25
        else:
            tp = float(self.take_profit_percent)  # config base (2.0%)

        # AI fine-tune: ±0.25% nudge on strong signals
        if intel > 2:
            tp = min(tp + 0.25, 4.0)
        elif intel < -2:
            tp = max(tp - 0.25, 1.0)

        return round(tp, 2)

    def _required_take_profit_percent(self, pair):
        """Adaptive TP using regime-aware dynamic base, optionally raised by ATR floor."""
        base_tp = self._dynamic_take_profit_percent()

        # ATR-based dynamic floor: require at least atr_tp_multiplier Ã— ATR% profit
        if self.enable_atr_dynamic_tp:
            atr = self._compute_atr(pair)
            current_price = self.pair_prices.get(pair, 0)
            if atr and current_price > 0:
                atr_pct = (atr / current_price) * 100.0
                base_tp = max(base_tp, self.atr_tp_multiplier * atr_pct)

        if not self.adaptive_tp_enabled:
            fee_buffer = float(self.sell_fee_buffer_percent or 0.0)
            return min(self.max_tp_percent, base_tp + fee_buffer)

        score = abs(float(self.pair_scores.get(pair, 0.0)))
        # Map score band [20..50] -> +0..+4%
        bonus = 0.0
        if score > 20:
            bonus = min(4.0, (score - 20.0) / 30.0 * 4.0)

        # Add fee buffer so required TP covers fees (configurable)
        fee_buffer = float(self.sell_fee_buffer_percent or 0.0)
        return min(self.max_tp_percent, base_tp + bonus + fee_buffer)

    def _can_sell_profit_target(self, pair, current_price):
        """Only allow sell when current price is at/above configured take-profit threshold from entry.

        Applies a conservative slippage buffer and ensures required TP + optional
        minimum net profit (net of fees) is met before allowing a SELL.
        """
        # With ATR trailing stop but WITHOUT dynamic TP: no indicator profit gate needed
        if self.enable_atr_stop and not self.enable_atr_dynamic_tp:
            return True  # Let winners run via trail; allow indicator-based exits without profit barrier
        # Conservative exit price accounting for slippage/spread
        slippage_pct = float(self.config.get('risk_management', {}).get('exit_slippage_buffer_pct', 0.3))
        conservative_exit_price = current_price * (1.0 - slippage_pct / 100.0)
        profit_pct = self._profit_percent_from_entry(pair, conservative_exit_price)
        if profit_pct is None:
            return False
        # require gross profit >= adaptive required TP
        required_tp = self._required_take_profit_percent(pair)
        if profit_pct < required_tp:
            return False
        # enforce minimum NET profit (after estimated fees) if configured
        min_net = float(self.config.get('risk_management', {}).get('min_net_sell_profit_pct', self.min_net_sell_profit_pct))
        if min_net > 0:
            fees_total_frac = pct_to_frac(getattr(self, 'fees_maker_percent', 0.0)) + pct_to_frac(getattr(self, 'fees_taker_percent', 0.0))
            fees_total_pct = fees_total_frac * 100.0
            net_profit_pct = profit_pct - fees_total_pct
            return net_profit_pct >= min_net
        return True

    def _can_close_short_profit_target(self, pair, current_price):
        """Only allow closing a short when it yields REAL net profit after fees.

        Mirror of _can_sell_profit_target for the short side. A short profits
        when price FALLS below entry. We apply a conservative slippage buffer
        (buying back slightly higher than mid), require the configured short
        take-profit, and enforce a minimum NET profit after roundtrip fees.
        Felix's rule: never close a short at a loss â€” only on real net gain.
        """
        entry = self.short_entry_prices.get(pair, 0.0)
        if entry <= 0 or current_price <= 0:
            return False
        slippage_pct = float(self.config.get('risk_management', {}).get('exit_slippage_buffer_pct', 0.3))
        # Closing a short = BUY, so a conservative (worse) fill is slightly higher.
        conservative_exit_price = current_price * (1.0 + slippage_pct / 100.0)
        # Short gross profit %: positive when we buy back below entry.
        profit_pct = ((entry - conservative_exit_price) / entry) * 100.0
        required_tp = float(self.short_take_profit_percent or 0.0)
        if profit_pct < required_tp:
            return False
        # Enforce minimum NET profit after estimated roundtrip fees.
        fees_total_frac = pct_to_frac(getattr(self, 'fees_maker_percent', 0.0)) + pct_to_frac(getattr(self, 'fees_taker_percent', 0.0))
        fees_total_pct = fees_total_frac * 100.0
        net_profit_pct = profit_pct - fees_total_pct
        min_net = float(self.config.get('risk_management', {}).get('min_net_sell_profit_pct', self.min_net_sell_profit_pct))
        return net_profit_pct >= max(0.0, min_net)

    def _update_trade_metrics(self, pair, pnl_eur):
        """Update per-pair win/loss counters and trigger loss-streak pause if needed.

        A winning trade (pnl_eur â‰¥ 0) resets the consecutive-loss counter and
        lifts any active loss-streak pause immediately.  After
        ``max_consecutive_losses`` losses the bot pauses new buys for
        ``pause_after_loss_streak_minutes`` minutes and recalculates the Kelly
        fraction for position sizing.
        """
        pnl_eur = float(pnl_eur)
        m = self.trade_metrics.setdefault(pair, {"closed": 0, "wins": 0, "losses": 0, "sum_pnl": 0.0})
        m["closed"] += 1
        m["sum_pnl"] += pnl_eur
        self.closed_trade_pnls.append(pnl_eur)
        if pnl_eur >= 0:
            m["wins"] += 1
            self.consecutive_losses = 0
            # A winning trade ends any active loss-streak pause immediately
            if self.trading_paused_until_ts > time.time():
                self.logger.info("Loss-streak pause lifted early after winning trade")
                self.trading_paused_until_ts = 0
        else:
            m["losses"] += 1
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.max_consecutive_losses:
                pause_sec = self.pause_after_loss_streak_minutes * 60
                self.trading_paused_until_ts = max(self.trading_paused_until_ts, int(time.time()) + pause_sec)
                self.logger.warning(
                    f"Loss-streak pause activated: {self.consecutive_losses} losses -> pause for {self.pause_after_loss_streak_minutes}m"
                )
                self.kelly_fraction = self._calculate_kelly_fraction()

    def _calculate_kelly_fraction(self):
        """Estimate Kelly fraction from realized closed trades (best-effort, bounded)."""
        try:
            pnls = list(self.closed_trade_pnls)
            if len(pnls) < 10:
                return 0.1

            wins = [p for p in pnls if p > 0]
            losses = [abs(p) for p in pnls if p < 0]
            if not wins or not losses:
                return 0.1

            win_rate = len(wins) / len(pnls)
            avg_win = sum(wins) / len(wins)
            avg_loss = sum(losses) / len(losses)
            if avg_win <= 0 or avg_loss <= 0:
                return 0.1

            b = avg_win / avg_loss
            kelly = win_rate - ((1 - win_rate) / b)
            return max(0.01, min(0.5, kelly))
        except Exception:
            return 0.1

    def check_take_profit_or_stop_loss(self):
        """Evaluate exits with TP first, then ATR stop, hard stop, time stop, then trailing stop."""
        for pair in self.trade_pairs:
            current_price = self.pair_prices.get(pair, 0)
            if current_price <= 0:
                continue

            # Long position exits â€” use position_qty (holdings zeroed in paper mode)
            holding = self.position_qty.get(pair, 0) or self.holdings.get(pair, 0)
            min_vol = self._get_min_volume(pair)
            if holding >= min_vol:
                prev_peak = self.peak_prices.get(pair, 0.0)
                self.peak_prices[pair] = max(prev_peak, current_price)

                change_percent = self._profit_percent_from_entry(pair, current_price)
                if change_percent is not None:
                    # ATR Trailing Stop Initialization & Update
                    if self.enable_atr_stop:
                        atr = self._compute_atr(pair)
                        if atr:
                            current_stop_info = self.stop_info.get(pair, {})
                            current_stop = current_stop_info.get('stop_price', 0)
                            
                            # Initialize if missing
                            if pair not in self.stop_info:
                                entry = self.purchase_prices.get(pair, current_price)
                                init_stop = max(0.0, entry - (atr * self.atr_multiplier))
                                self.stop_info[pair] = {'stop_price': init_stop, 'type': 'ATR'}
                                self.logger.info(f"Initialized ATR stop for {pair}: {init_stop:.4f} (atr={atr:.4f})")
                                current_stop = init_stop

                            # Ratchet up the stop: only move it UP
                            potential_stop = current_price - (atr * self.atr_trail_multiplier)
                            if potential_stop > current_stop:
                                self.stop_info[pair] = {'stop_price': potential_stop, 'type': 'ATR_TRAIL'}

                    # RSI-based Exit: if hourly RSI has recovered above overbought threshold, take profit
                    try:
                        rsi_cached = self._rsi_1h.get(pair)
                        if rsi_cached is not None and float(rsi_cached) >= float(self.mr_rsi_overbought):
                            return pair, "TAKE_PROFIT_RSI", change_percent
                    except Exception:
                        pass

                    # Exit Check 1: ATR/Trailing/Break-Even Stops
                    stop_data = self.stop_info.get(pair, {})
                    s_price = stop_data.get('stop_price')
                    if s_price is not None and current_price <= s_price:
                        return pair, stop_data.get('type', 'STOP'), change_percent

                    # Exit Check 2: Fixed Take Profit (ONLY if ATR trailing is NOT active)
                    if not self.enable_atr_stop:
                        req_tp = self._required_take_profit_percent(pair)
                        if self.take_profit_percent > 0 and change_percent >= req_tp:
                            return pair, "TAKE_PROFIT", change_percent

                    # Break-Even Stop-Loss logic (Manual activation if preferred)
                    if self.enable_break_even and change_percent >= self.break_even_trigger_pct:
                        entry_price = self.purchase_prices.get(pair, 0)
                        if entry_price > 0:
                            current_stop = self.stop_info.get(pair, {}).get('stop_price', 0)
                            if current_stop < entry_price:
                                self.stop_info[pair] = {'stop_price': entry_price, 'type': 'BREAK_EVEN'}
                                self.logger.info(f"BREAK-EVEN activated for {pair}: SL moved to entry ({entry_price:.4f})")

                    # Fixed dynamic stop-loss (regime-aware: 0.6% bearish / 0.8% ranging / 1.2% bullish)
                    _dsl = self._dynamic_stop_loss_percent()
                    if change_percent <= -abs(_dsl):
                        return pair, "STOP_LOSS", change_percent

                    if self.enable_hard_stop_loss and change_percent <= -abs(self.hard_stop_loss_percent):
                        return pair, "HARD_STOP", change_percent

                    if self.enable_time_stop:
                        opened_at = self.entry_timestamps.get(pair)
                        if opened_at and (time.time() - opened_at) >= (self.time_stop_hours * 3600):
                            return pair, "TIME_STOP", change_percent

                    # Legacy simple Trailing Stop-Loss
                    if not self.enable_atr_stop and self.trailing_stop_percent > 0 and change_percent > 0:
                        drop_from_peak = ((self.peak_prices[pair] - current_price) / self.peak_prices[pair]) * 100.0
                        if drop_from_peak >= self.trailing_stop_percent:
                            return pair, "TRAILING_STOP", change_percent

            # Short position exits
            short_qty = self.short_qty.get(pair, 0.0)
            short_entry = self.short_entry_prices.get(pair, 0.0)
            if self.enable_live_shorts and short_qty > 0 and short_entry > 0:
                short_change_percent = ((short_entry - current_price) / short_entry) * 100.0
                if short_change_percent >= self.short_take_profit_percent:
                    return pair, "SHORT_TAKE_PROFIT", short_change_percent
                # Stop loss: price moved against short by short_stop_loss_percent
                if self.short_stop_loss_percent > 0 and short_change_percent <= -self.short_stop_loss_percent:
                    return pair, "SHORT_STOP_LOSS", short_change_percent
                # Time review: after 12h close if net P&L (after accrued position fees)
                # won't survive the cost of another 4h rollover (0.02%)
                open_ts = self.entry_timestamps.get(pair) or 0
                if open_ts:
                    hours_held = (time.time() - open_ts) / 3600
                    if hours_held >= 12:
                        n_rollovers = int(hours_held / 4)
                        position_fees_pct = 0.04 + (n_rollovers * 0.02)  # open+close margin fees + rollovers
                        net_pnl_pct = short_change_percent - position_fees_pct
                        if net_pnl_pct < 0.02:  # below cost of next 4h rollover
                            return pair, "SHORT_TIME_REVIEW", short_change_percent

        return None, None, None

    def _warmup_pair_history(self, pair):
        """Seed price history â€” first tries NAS 5m OHLC, then falls back to Kraken API 60m."""
        # Prefer NAS 5m (more granular, no API call)
        if self.nas_root:
            try:
                self.analysis_tool.seed_from_nas_ohlc(pair, self.nas_root)
                history = self.analysis_tool._get_price_history(pair)
                if len(history) >= self.analysis_tool.sma_long:
                    return
            except Exception as e:
                self.logger.warning(f"NAS warmup failed for {pair}: {e}")
        # Fallback: Kraken API 60m OHLC
        try:
            ohlc = self.api_client.get_ohlc_data(pair, interval=60)
            if not ohlc:
                return
            data_key = next((k for k in ohlc if k != 'last'), None)
            if not data_key:
                return
            closes = [float(row[4]) for row in ohlc[data_key]]
            self.analysis_tool.seed_from_ohlc(pair, closes)
        except Exception as e:
            self.logger.warning(f"OHLC warmup failed for {pair}: {e}")

    def _refresh_hourly_signals(self):
        """Refresh signals and all MTF indicators for every configured pair.

        Per pair (every ``signal_refresh_interval`` seconds, default 5 min):
        - Fetches 1h OHLC  â†' signal/score, EMA crossover, 1h MACD histogram
        - Fetches 15m OHLC â†' 15m MACD histogram (for MTF MACD filter)

        All results are cached in instance dicts and read by the buy guards.
        """
        for pair in self.trade_pairs:
            try:
                # â”€â”€ 1h OHLC: signal + EMA crossover + 1h MACD â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                ohlc = self.api_client.get_ohlc_data(pair, interval=60)
                if not ohlc:
                    continue
                data_key = next((k for k in ohlc if k != 'last'), None)
                if not data_key:
                    continue
                series = ohlc[data_key]
                if not series:
                    continue
                closes_1h = [float(row[4]) for row in series]

                # Compute last close, RSI(14) and SMA200 on 1h series
                last_close = closes_1h[-1]
                try:
                    rsi_val = self.analysis_tool.calculate_rsi(closes_1h)
                except Exception:
                    rsi_val = None
                sma200 = None
                if len(closes_1h) >= 200:
                    sma200 = sum(closes_1h[-200:]) / 200.0

                # Store caches for exit checks later
                self._rsi_1h[pair] = rsi_val
                self._sma200_1h[pair] = sma200

                # Default signal via analysis tool (fallback)
                signal, score = self.analysis_tool.generate_signal_with_score({pair: {'c': [last_close]}})

                # Apply pair-specific RSI thresholds
                _prof     = self._pair_profile(pair)
                _rsi_buy  = _prof.get('rsi_buy',  self.mr_rsi_oversold)
                _rsi_sell = _prof.get('rsi_sell', self.mr_rsi_overbought)

                # Pair-specific mean-reversion override
                if self.enable_mr_signals and rsi_val is not None:
                    try:
                        if float(rsi_val) < float(_rsi_buy):
                            if sma200 is None or float(last_close) > float(sma200) * 0.97:
                                signal = 'BUY'
                                score  = max(score, (float(_rsi_buy) - float(rsi_val)) * 1.5)
                        elif float(rsi_val) > float(_rsi_sell):
                            signal = 'SELL'
                            score  = min(score, -(float(rsi_val) - float(_rsi_sell)) * 1.5)
                    except Exception:
                        pass

                # Contradictory signal (BUY + negative score, or SELL + positive score)
                # means two indicators disagree — treat as HOLD rather than flipping score
                if signal == 'BUY' and score < 0:
                    signal = 'HOLD'
                    score  = 0.0
                elif signal == 'SELL' and score > 0:
                    signal = 'HOLD'
                    score  = 0.0

                self.pair_signals[pair] = signal
                self.pair_scores[pair] = score

                # EMA crossover filter (EMA9 vs EMA21 on 1h)
                _, _, ema_bull = self.analysis_tool.calculate_ema_crossover(
                    closes_1h,
                    fast=self.ema_fast_period,
                    slow=self.ema_slow_period,
                )
                self._ema_bullish[pair] = ema_bull

                # 1h MACD histogram
                _, _, macd_h_1h = self.analysis_tool.calculate_macd(closes_1h)
                self._macd_1h_hist[pair] = macd_h_1h

                time.sleep(0.2)

                # â”€â”€ 15m OHLC: 15m MACD histogram â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                try:
                    ohlc_15 = self.api_client.get_ohlc_data(pair, interval=15)
                    if ohlc_15:
                        dk15 = next((k for k in ohlc_15 if k != 'last'), None)
                        if dk15 and ohlc_15[dk15]:
                            closes_15 = [float(row[4]) for row in ohlc_15[dk15]]
                            _, _, h15 = self.analysis_tool.calculate_macd(closes_15)
                            # Previous histogram value for trend direction
                            h15_prev = None
                            if len(closes_15) > 36:
                                _, _, h15_prev = self.analysis_tool.calculate_macd(closes_15[:-1])
                            self._macd_15m_hist[pair] = h15
                            self._macd_15m_hist_prev[pair] = h15_prev
                            time.sleep(0.2)
                except Exception as _e15:
                    self.logger.debug(f"15m MACD fetch error for {pair}: {_e15}")

            except Exception as e:
                self.logger.debug(f"Hourly signal refresh error for {pair}: {e}")

    # â”€â”€ New technical filters â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _is_ema_trend_bullish(self, pair):
        """Return True when the 1h EMA crossover is bullish (EMA-fast > EMA-slow).

        Computed in ``_refresh_hourly_signals`` every ``signal_refresh_interval``
        seconds and cached in ``self._ema_bullish``.  Returns True (allow trade)
        when no data is available yet so the bot isn't blocked on first startup.
        """
        if not self.enable_ema_crossover_filter:
            return True
        val = self._ema_bullish.get(pair)
        if val is None:
            return True  # no data yet â€” don't block
        if not val:
            self.logger.info(
                f"EMA crossover filter: BUY blocked for {pair} "
                f"(EMA{self.ema_fast_period} < EMA{self.ema_slow_period} on 1h â€” bearish trend)"
            )
        return val

    def _is_mtf_macd_buy_aligned(self, pair):
        """Return True when the MTF MACD picture is not strongly bearish.

        Logic (both timeframes must look bearish to block):
        - 1h  MACD histogram < -0.05% of price  AND
        - 15m MACD histogram < 0

        Requiring BOTH avoids blocking during consolidation where MACD hovers
        near zero.  When data is missing the check passes transparently.
        """
        if not self.enable_mtf_macd_filter:
            return True
        h1h = self._macd_1h_hist.get(pair)
        h15m = self._macd_15m_hist.get(pair)
        if h1h is None or h15m is None:
            return True  # no data yet
        price = self.pair_prices.get(pair, 1.0) or 1.0
        h1h_pct = (h1h / price) * 100.0
        if h1h_pct < -0.05 and h15m < 0:
            self.logger.info(
                f"MTF MACD filter: BUY blocked for {pair} "
                f"(1h hist {h1h_pct:.3f}%, 15m hist {h15m:.5f} â€” both bearish)"
            )
            return False
        return True

    def _auto_cancel_old_maker_orders(self):
        """Cancel post-only/maker orders older than configured threshold (hours).

        Scans open orders and cancels those whose order flags include 'post' (post-only)
        and which have been open longer than execution.maker_order_auto_cancel_hours.
        """
        try:
            exec_cfg = self.config.get('execution', {}) if isinstance(self.config, dict) else {}
            hours = int(exec_cfg.get('maker_order_auto_cancel_hours', 0))
            if hours <= 0:
                return
            now = time.time()
            open_orders = self.api_client.get_open_orders() or {}
            open_map = open_orders.get('open', open_orders) if isinstance(open_orders, dict) else open_orders
            if not open_map:
                return
            cancelled = 0
            for txid, order in list(open_map.items()):
                try:
                    descr = order.get('descr', {}) if isinstance(order, dict) else {}
                    oflags = (descr.get('oflags') or order.get('oflags') or '')
                    if not oflags or 'post' not in str(oflags):
                        continue
                    opentm = float(order.get('opentm') or descr.get('opentm') or 0)
                    if opentm <= 0:
                        continue
                    age_hours = (now - opentm) / 3600.0
                    if age_hours >= hours:
                        self.logger.info(f"Auto-cancel: cancelling maker order {txid} (age {age_hours:.1f}h >= {hours}h)")
                        try:
                            self.api_client.cancel_order(txid)
                            cancelled += 1
                        except Exception as _e:
                            self.logger.debug(f"Auto-cancel failed for {txid}: {_e}")
                except Exception:
                    continue
            if cancelled > 0:
                self.logger.info(f"Auto-cancel: cancelled {cancelled} old maker order(s)")
        except Exception as e:
            self.logger.debug(f"_auto_cancel_old_maker_orders error: {e}")

    def _execute_partial_exit(self, pair, price):
        """Sell ``partial_exit_fraction`` of the open position to lock in profits.

        Called automatically when unrealised profit â‰¥ ``partial_exit_trigger_pct``.
        Only fires once per entry (tracked via ``_partial_exit_done``).
        The remaining position continues running under ATR trailing stop.

        Skipped when:
        - Remaining EUR value after the sell would be below ``partial_exit_min_remaining_eur``
        - Sell volume is below the pair minimum
        """
        try:
            full_volume = self.position_qty.get(pair, 0.0) or self.holdings.get(pair, 0.0)
            min_vol = self._get_min_volume(pair)
            if full_volume < min_vol:
                self._partial_exit_done[pair] = True
                return

            sell_volume = round(full_volume * self.partial_exit_fraction, 8)
            remaining_volume = full_volume - sell_volume

            # Guard: don't leave a dust position
            if remaining_volume * price < self.partial_exit_min_remaining_eur:
                self.logger.info(
                    f"PARTIAL EXIT skipped for {pair}: remaining would be "
                    f"{remaining_volume * price:.2f} EUR < {self.partial_exit_min_remaining_eur:.2f} EUR minimum"
                )
                self._partial_exit_done[pair] = True
                return
            if sell_volume < min_vol:
                self.logger.info(
                    f"PARTIAL EXIT skipped for {pair}: sell volume {sell_volume:.8f} < min {min_vol}"
                )
                self._partial_exit_done[pair] = True
                return

            avg_entry = self.purchase_prices.get(pair, 0.0)
            est_profit_pct = self._profit_percent_from_entry(pair, price)
            est_profit_eur = (price - avg_entry) * sell_volume if avg_entry > 0 else 0.0
            pp_str = f"{est_profit_pct:.2f}%" if est_profit_pct is not None else "n/a"

            self.logger.info(
                f"PARTIAL EXIT ({self.partial_exit_fraction * 100:.0f}%): selling "
                f"{sell_volume:.6f} {pair} @ {price:.4f} EUR  profit={pp_str}  "
                f"keeping {remaining_volume:.6f}"
            )
            result = self._place_live_order(
                pair=pair, direction='sell', volume=sell_volume, price=price, post_only=True
            )
            if result:
                self._partial_exit_done[pair] = True
                self._sync_account_state(force_history=True)
                self.trade_count += 1
                now_ts = time.time()
                self.last_trade_at[pair] = now_ts
                self.last_global_trade_at = now_ts
                self._save_cooldown_state()
                self.logger.info(f"PARTIAL EXIT SUCCESS: {result}")
                self.logger.info(
                    f"PARTIAL SELL SUMMARY: {pair} {sell_volume:.6f} (~{sell_volume * price:.2f} EUR)"
                )
                self.logger.info(f"PARTIAL PNL ESTIMATE {pair}: {est_profit_eur:.2f} EUR ({pp_str})")
                self._update_trade_metrics(pair, est_profit_eur)
                fill_price = None
                try:
                    if isinstance(result, dict) and 'fill_price' in result:
                        fill_price = float(result['fill_price'])
                except Exception:
                    pass
                self._journal_trade(
                    'PARTIAL_SELL', pair, sell_volume, price, est_profit_eur, 'PARTIAL_EXIT',
                    extra={
                        'result': result,
                        'fraction': self.partial_exit_fraction,
                        'remaining_volume': remaining_volume,
                        'fill_price': fill_price,
                    },
                )
                print(
                    f"\n[PARTIAL SELL] {sell_volume:.6f} {pair} (~{sell_volume * price:.2f} EUR) "
                    f"kept {remaining_volume:.6f} â€” Trade #{self.trade_count}"
                )
                _notifier.send(
                    f"ðŸ“Š <b>PARTIAL SELL</b> #{self.trade_count}\n"
                    f"Pair: {pair}\n"
                    f"Sold: {sell_volume:.6f}  (~{sell_volume * price:.2f} EUR)\n"
                    f"Kept: {remaining_volume:.6f}\n"
                    f"Price: {price:.4f} EUR\n"
                    f"P&amp;L est.: {est_profit_eur:+.2f} EUR ({pp_str})"
                )
            else:
                self.logger.error(f"PARTIAL EXIT FAILED for {pair}")
        except Exception as e:
            self.logger.error(f"Error executing partial exit for {pair}: {e}", exc_info=True)

    def _update_regime_cache(self):
        """Update regime cache (risk_on flag) using benchmark pair and mtf scoring."""
        try:
            now = time.time()
            bench = self.regime_benchmark_pair
            # try compute mtf score from history or by fetching 1h OHLC
            mtf = self._compute_mtf_regime_score()
            if mtf is None:
                # seed history from 1h OHLC if needed
                try:
                    ohlc = self.api_client.get_ohlc_data(bench, interval=60)
                    if ohlc:
                        data_key = next((k for k in ohlc if k != 'last'), None)
                        if data_key:
                            closes = [float(r[4]) for r in ohlc[data_key]]
                            for c in closes[-self.analysis_tool.max_history:]:
                                self.analysis_tool._get_price_history(bench).append(c)
                            mtf = self._compute_mtf_regime_score()
                except Exception:
                    pass

            risk_on = True
            if mtf is not None:
                risk_on = mtf >= self.mtf_regime_min_score
            else:
                # fallback to pair_scores benchmark
                try:
                    score = float(self.pair_scores.get(bench, 0.0))
                    risk_on = score >= self.regime_min_score
                except Exception:
                    risk_on = True

            self._regime_cache = {'ts': now, 'risk_on': bool(risk_on)}
        except Exception:
            pass

    # â”€â”€ Three-phase pair analysis â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _fetch_market_data(self) -> None:
        """Phase 1 â€” Fetch live prices and run safety checks for every pair.

        For each pair:
        - Prefer WebSocket price; fall back to REST ticker.
        - Seed price history from OHLC candles if the buffer is sparse.
        - Update flash-crash airbag history; trigger emergency sell if tripped.

        Updates ``self.pair_prices`` in-place. No return value.
        """
        for pair in self.trade_pairs:
            try:
                pair_key = pair
                ws_price = None
                if self.ws_feed is not None:
                    try:
                        ws_price = self.ws_feed.get_price(pair)
                    except Exception:
                        ws_price = None

                if ws_price is not None:
                    current_price = ws_price
                    self.pair_prices[pair] = current_price
                else:
                    market_data = self.api_client.get_market_data(pair)
                    if market_data:
                        pair_key = list(market_data.keys())[0]
                        current_price = float(market_data[pair_key]['c'][0])
                        self.pair_prices[pair] = current_price
                    else:
                        current_price = self.pair_prices.get(pair, 0)

                # Seed price-history buffer from OHLC candles if too sparse
                if len(self.analysis_tool._get_price_history(pair_key)) < self.analysis_tool.max_history:
                    self._warmup_pair_history(pair)

                # Airbag: update history and panic-sell on flash crash
                self._update_airbag_history(pair, current_price)
                if self._check_airbag_trigger(pair):
                    if (self.position_qty.get(pair, 0) or self.holdings.get(pair, 0)) >= self._get_min_volume(pair):
                        self.execute_sell_order(pair, current_price,
                                                require_profit_target=False, reason="CRASH_AIRBAG")
            except Exception as exc:
                self.logger.error("_fetch_market_data error for %s: %s", pair, exc)

    def _generate_signals(self) -> None:
        """Phase 2 â€” Refresh pair signals and scores if the interval has elapsed.

        Calls ``_refresh_hourly_signals()`` which runs the full technical-analysis
        engine (RSI, Bollinger Bands, ATR, etc.) for each pair and stores results
        in ``self.pair_signals`` and ``self.pair_scores``.

        Reads from ``self.pair_prices`` populated by ``_fetch_market_data()``.
        """
        try:
            now = time.time()
            if (now - self._last_signal_refresh_ts) >= self.signal_refresh_interval:
                self._refresh_hourly_signals()
                self._last_signal_refresh_ts = now
        except Exception as exc:
            self.logger.error("_generate_signals error: %s", exc)

    def _select_best_pair(self) -> tuple:
        """Phase 3 â€” Read cached signals and select the best actionable pair.

        Iterates ``self.pair_signals`` / ``self.pair_scores`` (written by
        ``_generate_signals``) and returns the pair with the highest absolute
        score that has an actionable BUY or SELL signal.

        Returns:
            (best_pair, best_signal, best_score) or (None, "HOLD", 0).
        """
        best_pair   = None
        best_signal = "HOLD"
        best_score  = 0

        for pair in self.trade_pairs:
            try:
                signal = self.pair_signals.get(pair) or "HOLD"
                score  = float(self.pair_scores.get(pair, 0))
                self.logger.info("PAIR %s: %s | score %.2f", pair, signal, score)

                if signal not in ("BUY", "SELL"):
                    time.sleep(0.25)
                    continue

                # With mean reversion disabled, SMA momentum signals always have
                # scores that match direction. No contradictory filter needed.

                has_long  = (self.position_qty.get(pair, 0) or self.holdings.get(pair, 0)) >= self._get_min_volume(pair)
                has_short = self.short_qty.get(pair, 0.0) > 0

                # Filter non-actionable SELL on empty position
                if signal == "SELL" and not has_long and not has_short and not self.enable_live_shorts:
                    time.sleep(0.25)
                    continue

                # Skip BUY if an open short exists — buy gate would block it anyway,
                # so fall through to the next best pair instead of wasting the loop
                if signal == "BUY" and has_short:
                    self.logger.debug("Pair %s skipped in selection: open short exists", pair)
                    time.sleep(0.25)
                    continue

                # Skip BUY if already holding a long on this pair
                if signal == "BUY" and has_long:
                    time.sleep(0.25)
                    continue

                if abs(score) > abs(best_score):
                    best_pair   = pair
                    best_signal = signal
                    best_score  = score

                time.sleep(0.25)
            except Exception as exc:
                self.logger.error("_select_best_pair error for %s: %s", pair, exc)

        return best_pair, best_signal, best_score

    # â”€â”€ Extracted main-loop phases â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _run_initialization(self, max_runtime_seconds=None) -> float:
        """Phase: startup logging, balance fetch, account sync.
        Returns the initial EUR balance."""
        self.logger.info("=" * 60)
        self.logger.info("TRADING BOT STARTED - MULTI-PAIR MODE")
        self.logger.info(f"Watching: {', '.join(self.trade_pairs)}")
        self.logger.info(f"Target: {self.target_balance_eur} EUR")
        if max_runtime_seconds:
            self.logger.info(f"Max runtime: {max_runtime_seconds} seconds (self-terminating)")
        self.logger.info("=" * 60)
        print("=" * 60)
        print("KRAKEN TRADING BOT - MULTI-PAIR MODE")
        print(f"Watching {len(self.trade_pairs)} pairs: {', '.join(self.trade_pairs)}")
        print(f"Trade Amount: {self._get_trade_amount_eur()} EUR per trade")
        print(f"Target Balance: {self.target_balance_eur} EUR")
        if max_runtime_seconds:
            print(f"Max runtime: {max_runtime_seconds} seconds (will stop automatically)")
        print("Press Ctrl+C to stop")
        print("=" * 60)

        initial_balance = self.get_eur_balance()
        self._load_balance_state(initial_balance)   # restores peak + initial from disk
        self.daily_start_balance = initial_balance
        self._load_cumulative_pnl_state(initial_balance)
        self._sync_account_state(force_history=True)
        self._reconcile_open_orders()
        self._refresh_cashflows_from_ledger(force=True)

        self.logger.info(f"Initial EUR Balance: {initial_balance:.2f} EUR")
        self.logger.info(f"Take-Profit: {self.take_profit_percent}% | Stop-Loss: {self.stop_loss_percent}%")

        for pair in self.trade_pairs:
            qty = self.holdings.get(pair, 0.0)
            avg = self.purchase_prices.get(pair, 0.0)
            if qty >= self._get_min_volume(pair):
                self.logger.info(f"Startup position: {pair} qty={qty:.8f} avg_entry={avg:.4f} EUR")
            else:
                self.logger.info(f"Startup position: {pair} â€” no holdings (qty={qty:.8f})")

        return initial_balance

    def _check_market_intelligence(self) -> None:
        """Phase: spawn the AI intelligence refresh thread if the interval has elapsed.
        Non-blocking â€” the thread writes under _intel_lock when complete."""
        if not _INTELLIGENCE_AVAILABLE:
            return
        if time.time() - self._intelligence_last_ts < self._intelligence_refresh_secs:
            return
        try:
            import threading as _threading
            _bot_ctx = {
                "sharpe":          self._sharpe_result.get("sharpe"),
                "sharpe_verdict":  self._sharpe_result.get("verdict", "insufficient_data"),
                "sharpe_trending": self._sharpe_result.get("trending", "stable"),
                "trade_count":     getattr(self, "trade_count", 0),
                "balance_eur":     getattr(self, "_last_balance_eur", None),
                "pair_signals":    dict(getattr(self, "pair_signals", {})),
                "pair_scores":     dict(getattr(self, "pair_scores", {})),
                "open_positions": {
                    p: {"qty": float(self.position_qty.get(p, 0) or self.holdings.get(p, 0)),
                        "entry": float(self.purchase_prices.get(p, 0))}
                    for p in self.trade_pairs
                    if float(self.position_qty.get(p, 0) or self.holdings.get(p, 0)) > 0
                },
                "open_shorts": {
                    p: {"qty": float(self.short_qty.get(p, 0)),
                        "entry": float(self.short_entry_prices.get(p, 0))}
                    for p in self.trade_pairs
                    if float(self.short_qty.get(p, 0)) > 0
                },
            }
            try:
                _jpath = getattr(self, "json_journal_path", "")
                if _jpath and os.path.exists(_jpath):
                    with open(_jpath, "r") as _jf:
                        _lines = [l.strip() for l in _jf if l.strip()][-10:]
                    _bot_ctx["recent_trades"] = [json.loads(l) for l in _lines if l]
            except Exception:
                pass
            # Include Ichimoku + Gaussian data so AI models can reason about trend structure
            if _ICHI_AVAILABLE:
                try:
                    _bot_ctx["ichi"] = {
                        p: _ichi_get_signal(p, self.api_client)
                        for p in self.trade_pairs
                    }
                except Exception:
                    pass

            self._intelligence_last_ts = time.time()

            def _refresh_intel(_ctx=_bot_ctx):
                try:
                    result = _get_market_intelligence(self.trade_pairs, _ctx)
                    with self._intel_lock:
                        self._intelligence_score         = result.get("score", 0.0)
                        self._intelligence_model_scores  = result.get("model_scores", {})
                        self._intelligence_model_outputs = result.get("model_outputs", {})
                        self._sharpe_funding_scores      = result.get("sharpe_funding", {})
                        self._sharpe_insider_scores      = result.get("sharpe_insider", {})
                    self.logger.info(
                        "Intelligence refresh complete: score=%.2f sources=%d/5",
                        self._intelligence_score,
                        len([v for v in self._intelligence_model_scores.values() if v is not None])
                    )
                except Exception as exc:
                    self.logger.warning("Intelligence refresh error: %s", exc)

            _threading.Thread(target=_refresh_intel, daemon=True, name='intel-refresh').start()
        except Exception:
            pass

    def _manage_portfolio_risk(self, current_balance: float) -> tuple:
        """Phase: daily/monthly resets, drawdown circuit-breaker, regime evaluation.

        Returns:
            (regime_state, pause_state, adjusted_pnl) â€” strings + float needed
            by the status log and dashboard writer.
        """
        # Daily + monthly balance resets
        now = datetime.now()
        last_reset = datetime.fromtimestamp(self.last_daily_reset_ts)
        if now.day != last_reset.day or now.month != last_reset.month or now.year != last_reset.year:
            self.daily_start_balance = current_balance
            self.last_daily_reset_ts = int(time.time())
            self.logger.info(f"Daily start balance reset to {self.daily_start_balance:.2f} EUR")

        # Daily email report
        self._maybe_send_daily_report()

        # Refresh Kraken fee schedule once per 24h
        self._maybe_refresh_fees()

        # Portfolio valuation — must be calculated before monthly tracking uses it
        self._refresh_cashflows_from_ledger()
        adjusted_pnl = self._adjusted_pnl_eur(current_balance)
        holdings_value = 0.0
        try:
            holdings_value = float(sum(
                (self.position_qty.get(p, 0.0) or self.holdings.get(p, 0.0))
                * self.pair_prices.get(p, 0.0)
                for p in self.trade_pairs
            ))
        except Exception:
            pass
        # Include open scalper positions — their deployed cash is not a loss
        _sc = getattr(self, '_scalper', None)
        if _sc is not None:
            try:
                for _scp, _scv in _sc.get_status().get('positions', {}).items():
                    _sc_qty   = float(_scv.get('qty', 0) or 0)
                    _sc_price = float(self.pair_prices.get(_scp) or _scv.get('entry') or 0)
                    holdings_value += _sc_qty * _sc_price
            except Exception as _sce:
                self.logger.debug("Scalper portfolio calc error: %s", _sce)
        reserve = float(self._estimate_open_buy_reserve_eur() or 0)
        portfolio_value = float(current_balance or 0) + holdings_value + reserve

        if now.month != self._monthly_start_month or self._monthly_start_balance <= 0:
            self._monthly_start_balance = portfolio_value
            self._monthly_start_month = now.month
            self._monthly_target_hit_notified = False
            self.logger.info(
                f"Monthly tracker reset: start={portfolio_value:.2f} EUR portfolio (target +3-8%)"
            )

        _monthly_pct = self._monthly_return_pct(portfolio_value)
        if _monthly_pct >= 3.0 and not self._monthly_target_hit_notified:
            self._monthly_target_hit_notified = True
            self.logger.info(f"MONTHLY TARGET HIT: +{_monthly_pct:.2f}% this month")
            try:
                _notifier.send(
                    f"ðŸŽ¯ <b>Monthly target reached!</b>\n"
                    f"Return this month: <b>+{_monthly_pct:.2f}%</b>\n"
                    f"Start: â‚¬{self._monthly_start_balance:.2f} â†' Now: â‚¬{current_balance:.2f}\n"
                    f"Position sizing reduced to protect gains."
                )
            except Exception:
                pass

        # Drawdown circuit-breaker

        try:
            self.peak_balance = max(getattr(self, 'peak_balance', portfolio_value), portfolio_value)
            if self.peak_balance > 0:
                current_dd_pct = ((self.peak_balance - portfolio_value) / self.peak_balance) * 100.0
                max_dd_cfg = float(self.config.get('risk_management', {}).get('max_drawdown_percent', 10.0))
                if current_dd_pct >= max_dd_cfg and not self._circuit_breaker_triggered:
                    self._circuit_breaker_triggered = True
                    self.trading_paused_until_ts = int(time.time()) + 86400
                    self.logger.warning(
                        "CIRCUIT BREAKER: drawdown %.2f%% >= %.2f%%. "
                        "cash=%.2f holdings=%.2f reserve=%.2f portfolio=%.2f peak=%.2f",
                        current_dd_pct, max_dd_cfg,
                        float(current_balance or 0), holdings_value, reserve,
                        portfolio_value, self.peak_balance,
                    )
                    # Force-close all open positions
                    _cb_sold = False
                    for _cb_pair in list(self.trade_pairs):
                        _cb_qty = self.position_qty.get(_cb_pair, 0.0) or self.holdings.get(_cb_pair, 0.0)
                        _cb_price = self.pair_prices.get(_cb_pair, 0.0)
                        if _cb_qty >= self._get_min_volume(_cb_pair) and _cb_price > 0:
                            self.logger.warning("CIRCUIT BREAKER: closing %s @ %.4f", _cb_pair, _cb_price)
                            self.execute_sell_order(_cb_pair, _cb_price, require_profit_target=False, reason="CIRCUIT_BREAKER")
                            _cb_sold = True
                    # Telegram alert
                    try:
                        _notifier.send(
                            f"[CIRCUIT BREAKER] Drawdown {current_dd_pct:.1f}% >= {max_dd_cfg:.1f}% limit. "
                            f"All positions closed. Buying paused 24h. Peak: {self.peak_balance:.2f} EUR | "
                            f"Now: {portfolio_value:.2f} EUR"
                        )
                    except Exception:
                        pass
                elif current_dd_pct < max_dd_cfg * 0.5 and self._circuit_breaker_triggered:
                    # Reset once portfolio recovers to half the drawdown threshold
                    self._circuit_breaker_triggered = False
                    self.logger.info("CIRCUIT BREAKER reset: drawdown recovered to %.2f%%", current_dd_pct)
        except Exception as exc:
            self.logger.debug("Drawdown calculation failed: %s", exc)

        # Bear shield
        if self.enable_bear_shield:
            bear_now = self._is_bear_market()
            if bear_now and not self._bear_mode_active:
                self.logger.warning("BEAR SHIELD ACTIVATED â€” selling all positions, parking in EUR")
                self._bear_mode_active = True
                self._bear_shield_exit_all()
            elif not bear_now and self._bear_mode_active:
                self.logger.info("BEAR SHIELD DEACTIVATED â€” trend turned bullish")
                self._bear_mode_active = False
            elif bear_now:
                _now_ts = time.time()
                if (_now_ts - self._bear_last_log_ts) >= self.bear_log_interval_minutes * 60:
                    self.logger.info("BEAR SHIELD: still in bear mode")
                    self._bear_last_log_ts = _now_ts

        regime_state = "RISK_ON" if self._is_risk_on_regime() else "RISK_OFF"
        pause_state  = "PAUSED"  if self._is_temporarily_paused() else "ACTIVE"
        return regime_state, pause_state, adjusted_pnl, portfolio_value, holdings_value

    def _handle_trade_execution(self, best_pair, best_signal, best_score) -> None:
        """Phase: apply BUY and SELL gates, then execute the winning trade.

        Uses if/elif instead of continue-chains for readability. Each branch
        returns early (instead of continue) when a gate blocks execution.
        """
        if not best_pair or best_signal == "HOLD":
            return
        if self._is_on_cooldown(best_pair) or self._is_global_cooldown():
            return

        price = self.pair_prices.get(best_pair, 0)

        if best_signal == "BUY":
            self._execute_buy_gate(best_pair, price, float(self.pair_scores.get(best_pair, 0.0)))

        elif best_signal == "SELL":
            self._execute_sell_gate(best_pair, price)

    def _execute_buy_gate(self, pair: str, price: float, score: float) -> None:
        """All guards for a long entry, expressed as early returns (not continue).
        Executes the buy if every gate passes."""
        if self._is_temporarily_paused():
            self.logger.warning("BUY paused: loss-streak cooling period active")
            self.kelly_fraction = self._calculate_kelly_fraction()
            return
        if self._daily_drawdown_hit():
            self.logger.warning("BUY paused: daily loss limit reached")
            self.kelly_fraction = self._calculate_kelly_fraction()
            return

        with self._intel_lock:
            _iscore  = self._intelligence_score
            _iweight = self._intelligence_score_weight
        _intel_adj = -(_iscore * _iweight)

        # Sentiment adjustments: LunarCrush and on-chain scores shift effective min
        # Positive combined score = bullish sentiment = lower the bar to enter
        # Negative combined score = bearish sentiment = raise the bar
        _lunar_adj   = -(self._lunarcrush_combined * 0.5)   # max ±1.5 pts
        _onchain_adj = -(self._onchain_combined * 0.5)       # max ±1.5 pts

        # Use pair-specific min_score if defined, otherwise global setting
        _pair_min_score = self._pair_profile(pair).get('min_score', self.min_buy_score)
        _effective_min  = _pair_min_score + _intel_adj + _lunar_adj + _onchain_adj
        if score < _effective_min:
            self.logger.info(
                "BUY skipped for %s: score %.2f < effective_min %.2f "
                "(pair_base=%.2f intel_adj=%+.2f lunar_adj=%+.2f onchain_adj=%+.2f profile=%s)",
                pair, score, _effective_min, _pair_min_score, _intel_adj,
                _lunar_adj, _onchain_adj,
                self._pair_profile(pair).get('strategy', '?')
            )
            return
        # Block re-buying a pair that already has an open position (prevents averaging down)
        _existing_qty = self.position_qty.get(pair, 0) or self.holdings.get(pair, 0)
        if _existing_qty >= self._get_min_volume(pair):
            self.logger.info("BUY skipped for %s: already have open position (qty=%.6f) — wait for TP/SL exit first", pair, _existing_qty)
            return

        _open_pos = self._count_open_positions()
        if _open_pos >= self.max_open_positions:
            self.logger.info("BUY skipped: max open positions reached (%d/%d)", _open_pos, self.max_open_positions)
            return
        if not self._is_trading_hours():
            self.logger.info("BUY skipped: outside trading hours")
            return
        try:
            now = time.time()
            if (now - self._regime_cache.get('ts', 0)) > self._regime_cache_ttl:
                self._update_regime_cache()
            if self.enable_regime_filter and not bool(self._regime_cache.get('risk_on', True)):
                self.logger.info("BUY skipped: regime filter is RISK_OFF (cached)")
                return
        except Exception:
            pass
        if self._bear_mode_active:
            self.logger.info("BUY skipped: BEAR SHIELD active")
            return
        if self.sentiment_active:
            self.logger.info("BUY skipped for %s: sentiment guard active", pair)
            return
        if not self._is_ema_trend_bullish(pair):
            return
        if not self._is_mtf_macd_buy_aligned(pair):
            return
        # Ichimoku + Gaussian gate
        if _ICHI_AVAILABLE:
            try:
                _ichi = _ichi_get_signal(pair, self.api_client)
                _vs_cloud = _ichi.get("price_vs_cloud", "unknown")
                _trend    = _ichi.get("trend", "neutral")
                # Only hard-block when price is confirmed below the cloud.
                # Inside + neutral = consolidation, still tradeable on strong signals.
                if _vs_cloud == "below":
                    self.logger.info(
                        "BUY skipped for %s: Ichimoku cloud (%s) — trend=%s cloud=%.4f-%.4f",
                        pair, _vs_cloud, _trend,
                        _ichi.get("cloud_bottom", 0), _ichi.get("cloud_top", 0),
                    )
                    return
                if _vs_cloud == "inside" and _trend == "bearish":
                    self.logger.info(
                        "BUY skipped for %s: Ichimoku cloud (inside+bearish) — cloud=%.4f-%.4f",
                        pair, _ichi.get("cloud_bottom", 0), _ichi.get("cloud_top", 0),
                    )
                    return
                # Gaussian lower band touch within uptrend → boost score
                if _ichi.get("score_boost", 0) > 0:
                    score = score + _ichi["score_boost"]
                    self.logger.info(
                        "BUY boosted for %s: Gaussian lower band + Ichimoku bullish (+%.1f → score=%.2f)",
                        pair, _ichi["score_boost"], score,
                    )
            except Exception as _ie:
                self.logger.debug("Ichimoku gate error for %s: %s", pair, _ie)
        if not self._has_sufficient_volume(pair):
            return
        if any(g in (pair or '').upper() for g in self.reentry_guard_pairs):
            try:
                last_net = self._last_closed_trade_net_profit_pct(pair)
                if last_net is not None and last_net < self.min_reentry_profit_pct:
                    self.logger.info(
                        "BUY skipped for %s: last net profit %.2f%% < min_reentry %.2f%%",
                        pair, last_net, self.min_reentry_profit_pct
                    )
                    return
            except Exception as exc:
                self.logger.debug("Re-entry guard error for %s: %s", pair, exc)
        if float(getattr(self, 'short_qty', {}).get(pair, 0)) > 0:
            self.logger.info("BUY blocked for %s: open short exists â€” close short first", pair)
            return

        self._breakout_timestamps[pair] = time.time()
        self.execute_buy_order(pair, price)

    def _execute_sell_gate(self, pair: str, price: float) -> None:
        """SELL signal handler: close long if profitable, else open/manage short."""
        min_vol = self._get_min_volume(pair)
        # In paper mode self.holdings is always empty (get_crypto_holdings early-returns).
        # Also guard with purchase_prices so a long persisted in the position file but not
        # yet synced into position_qty (brief window at startup) can't trigger a short open.
        has_long = (
            (self.position_qty.get(pair, 0) or self.holdings.get(pair, 0)) >= min_vol
            or self.purchase_prices.get(pair, 0) > 0
        )

        if has_long:
            if self._can_sell_profit_target(pair, price):
                self.execute_sell_order(pair, price)
            else:
                pp  = self._profit_percent_from_entry(pair, price)
                req = self._required_take_profit_percent(pair)
                self.logger.info(
                    "SELL skipped for %s: profit target not reached (%s%% < %.2f%%)",
                    pair, f"{pp:.2f}" if pp is not None else "n/a", req
                )
        elif self.enable_live_shorts and self.short_qty.get(pair, 0.0) <= 0:
            score        = float(self.pair_scores.get(pair, 0.0))
            # Read cached EMA trend directly (not via the BUY filter flag) so
            # disabling enable_ema_crossover_filter doesn't also disable short entry.
            trend_bearish = not self._ema_bullish.get(pair, True)
            risk_off_ok  = (not self._is_risk_on_regime()
                            if self.enable_regime_filter else True)
            if (trend_bearish or abs(score) >= self.min_buy_score) and risk_off_ok and abs(score) >= self.min_buy_score:
                self.execute_open_short_order(pair, price)
            else:
                self.logger.info(
                    "SHORT skipped for %s: bearish=%s risk_off=%s score=%.2f",
                    pair, trend_bearish, risk_off_ok, score
                )
        elif self.enable_live_shorts and self.short_qty.get(pair, 0.0) > 0:
            if self._can_close_short_profit_target(pair, price):
                self.execute_close_short_order(pair, price)
            else:
                se  = self.short_entry_prices.get(pair, 0.0)
                spp = ((se - price) / se * 100.0) if se > 0 else 0.0
                self.logger.info(
                    "SHORT CLOSE skipped for %s: net profit target not reached (%.2f%% gross)",
                    pair, spp
                )
        else:
            self._log_empty_sell_signal_throttled(pair)

    def _handle_alpaca_correlations(self, best_pair: str, best_signal: str, best_score: float) -> None:
        """
        When a strong BTC or ETH signal fires on Kraken, mirror it on Alpaca
        by buying correlated stocks (MSTR, COIN, MARA).

        Entry: strong BUY signal (score >= 10) on XBTEUR/XETHZEUR
        Exit:  SELL signal on the same Kraken pair, OR market closes
        """
        if not _ALPACA_ENABLED or not _alpaca_available():
            return

        alpaca = _alpaca_client()

        # Only trade during US market hours
        if not alpaca.is_market_open():
            return

        # Determine which correlates apply
        correlates = []
        if best_pair in ("XBTEUR", "XXBTZEUR") and abs(best_score) >= 10:
            correlates = _BTC_CORRELATES
        elif best_pair in ("XETHZEUR", "ETHEUR") and abs(best_score) >= 10:
            correlates = _ETH_CORRELATES

        if not correlates:
            return

        portfolio_value = alpaca.get_portfolio_value()
        if portfolio_value <= 0:
            return
        notional = portfolio_value * (_ALPACA_ALLOC_PCT / 100)

        if best_signal == "BUY":
            for symbol in correlates:
                if alpaca.has_position(symbol):
                    continue
                result = alpaca.market_buy(symbol, notional)
                if result:
                    self._alpaca_positions[symbol] = {
                        "kraken_pair":  best_pair,
                        "kraken_score": best_score,
                        "notional_usd": notional,
                    }
                    try:
                        _notifier.send(
                            f"[ALPACA BUY] {symbol}\n"
                            f"Triggered by: {best_pair} score {best_score:+.2f}\n"
                            f"Notional: ${notional:.2f}"
                        )
                    except Exception:
                        pass

        elif best_signal == "SELL":
            for symbol in correlates:
                if not alpaca.has_position(symbol):
                    continue
                alpaca.market_sell_all(symbol)
                self._alpaca_positions.pop(symbol, None)
                try:
                    _notifier.send(
                        f"[ALPACA SELL] {symbol}\n"
                        f"Triggered by: {best_pair} SELL signal"
                    )
                except Exception:
                    pass

    def _handle_new_listing_cycle(self) -> None:
        """
        New listings strategy — runs every loop:
        1. Every 10 min: poll Sharpe.ai for new Kraken spot listings
        2. New coin found: record initial price, send Telegram alert
        3. Price up 2%+ from detection price: BUY
        4. 12 hours after BUY: force-SELL (capture pump, exit before drawdown)
        5. 12 hours without a buy: remove from watchlist
        """
        if not _LISTINGS_AVAILABLE:
            return

        # Step 1 — periodic Sharpe.ai poll
        now = time.time()
        if now - self._listings_last_check >= self._listings_check_interval:
            self._listings_last_check = now
            try:
                # Three sources combined — deduplicated by symbol
                seen_symbols = set(self._listing_watchlist.keys())
                blog_listings   = _fetch_blog_listings(hours_lookback=48)
                sharpe_listings = _fetch_listings(hours_lookback=24)
                self._kraken_headlines = _fetch_blog_headlines(limit=8)
                # AssetPairs is 1.1MB — only check every 2 hours
                pairs_listings = []
                if now - self._assetpairs_last_check >= self._assetpairs_check_interval:
                    self._assetpairs_last_check = now
                    pairs_listings = _fetch_new_pairs(hours_lookback=48)

                # Prioritise: AssetPairs (exact) > Blog RSS (early) > Sharpe.ai (hourly)
                new_listings = []
                for lst in pairs_listings + blog_listings + sharpe_listings:
                    if lst["symbol"] not in seen_symbols:
                        seen_symbols.add(lst["symbol"])
                        new_listings.append(lst)

                if new_listings:
                    self.logger.info(
                        "Listings sources: %d blog, %d pairs, %d sharpe = %d unique new",
                        len(blog_listings), len(pairs_listings),
                        len(sharpe_listings), len(new_listings)
                    )
                for listing in new_listings:
                    symbol = listing["symbol"]
                    if symbol in self._listing_watchlist:
                        continue
                    # Skip listings older than 2× the hold window — truly stale coins.
                    # Using 2× (24h) rather than 1× because blog RSS listed_at is the
                    # announcement time, which can be hours before the coin goes live.
                    listing_age_h = (now - listing.get("listed_at", now)) / 3600
                    if listing_age_h > self._listing_hold_hours * 2:
                        self.logger.debug(
                            "NEW LISTING: %s listed %.1fh ago — older than 2× hold window, skipping",
                            symbol, listing_age_h,
                        )
                        continue
                    pair = listing["kraken_pair"]
                    # Get current price from Kraken
                    # Try pair name variants until Kraken returns a price
                    resolved_pair = None
                    initial_price = 0.0
                    for variant in listing.get("pair_variants", [listing["kraken_pair"]]):
                        try:
                            md = self.api_client.get_market_data(variant)
                            if md:
                                key = next(iter(md))
                                initial_price = float(md[key]["c"][0])
                                resolved_pair = variant
                                break
                        except Exception:
                            continue
                    if not resolved_pair or initial_price <= 0:
                        self.logger.debug("NEW LISTING: %s — no Kraken price found, skipping", listing["symbol"])
                        continue
                    listing["kraken_pair"] = resolved_pair  # use confirmed pair name
                    if _add_to_watchlist(self._listing_watchlist, listing, initial_price):
                        self.logger.info(
                            "NEW LISTING DETECTED: %s on Kraken @ %.6f EUR — buying in 15 min if trending up",
                            symbol, initial_price
                        )
                        try:
                            source_label = {
                                "kraken_assetpairs": "Kraken API (live)",
                                "kraken_blog":       "Kraken Blog (early)",
                            }.get(listing.get("source", ""), "Sharpe.ai")
                            _notifier.send(
                                f"[NEW LISTING] {listing.get('name', symbol)[:60]}\n"
                                f"Symbol: {symbol} | Source: {source_label}\n"
                                f"Pair: {resolved_pair} @ {initial_price:.6f} EUR\n"
                                f"Watching 15 min then buy if +0.8% trend"
                            )
                        except Exception:
                            pass
            except Exception as exc:
                self.logger.debug("New listings poll error: %s", exc)

        # Step 2 — manage watchlist entries
        to_remove = []
        for symbol, entry in list(self._listing_watchlist.items()):
            pair = entry.get("kraken_pair", symbol + "EUR")
            try:
                md = self.api_client.get_market_data(pair)
                if not md:
                    if _listing_expired(entry, self._listing_hold_hours):
                        to_remove.append(symbol)
                    continue
                key = next(iter(md))
                current_price = float(md[key]["c"][0])
            except Exception:
                continue

            if entry.get("bought"):
                buy_price = float(entry.get("buy_price") or 0)
                qty = self.position_qty.get(pair, self.holdings.get(pair, 0))

                if buy_price > 0 and current_price > 0 and qty > 0:
                    change_from_buy = (current_price - buy_price) / buy_price * 100

                    # Hard stop loss — dump at loss if down >1.5%
                    if change_from_buy <= -self._listing_stop_loss_pct:
                        self.logger.warning(
                            "NEW LISTING STOP LOSS: %s down %.2f%% from buy — dumping",
                            symbol, change_from_buy,
                        )
                        self.execute_sell_order(pair, current_price,
                                                require_profit_target=False,
                                                reason="LISTING_STOP_LOSS")
                        try:
                            _notifier.send(
                                f"[LISTING STOP] {symbol} dumped at {change_from_buy:.2f}% "
                                f"(buy {buy_price:.6f} → now {current_price:.6f})"
                            )
                        except Exception:
                            pass
                        if pair in self.trade_pairs and pair not in self._core_trade_pairs:
                            self.trade_pairs.remove(pair)
                        to_remove.append(symbol)
                        continue

                    # Update peak price tracker
                    peak = self.peak_prices.get(pair, current_price)
                    self.peak_prices[pair] = max(peak, current_price)
                    peak = self.peak_prices[pair]
                    pullback = (peak - current_price) / peak * 100 if peak > 0 else 0

                    # Profit target — sell slow climbers at +8%
                    if change_from_buy >= 8.0:
                        # Big mover: use trailing stop (0.5% pullback from peak)
                        if pullback >= self._listing_pullback_pct:
                            self.logger.info(
                                "NEW LISTING TRAILING STOP: %s pulled back %.2f%% from peak "
                                "(%.2f%% above buy) — locking in gain",
                                symbol, pullback, change_from_buy,
                            )
                            self.execute_sell_order(pair, current_price,
                                                    require_profit_target=False,
                                                    reason="LISTING_TRAILING_STOP")
                            if pair in self.trade_pairs and pair not in self._core_trade_pairs:
                                self.trade_pairs.remove(pair)
                            to_remove.append(symbol)
                            continue
                    elif change_from_buy >= self._listing_fee_pct:
                        # Slow climber: take profit at +8% target or trailing stop
                        if pullback >= self._listing_pullback_pct:
                            self.logger.info(
                                "NEW LISTING PROFIT TAKE: %s +%.2f%% — selling on pullback",
                                symbol, change_from_buy,
                            )
                            self.execute_sell_order(pair, current_price,
                                                    require_profit_target=False,
                                                    reason="LISTING_PROFIT_TAKE")
                            if pair in self.trade_pairs and pair not in self._core_trade_pairs:
                                self.trade_pairs.remove(pair)
                            to_remove.append(symbol)
                            continue

                # 12-hour window — only force-sell if coin is no longer trending up
                if _listing_expired(entry, self._listing_hold_hours):
                    peak = self.peak_prices.get(pair, current_price)
                    still_trending = (peak > 0 and
                                      (peak - current_price) / peak * 100 < self._listing_pullback_pct)
                    if still_trending and qty > 0:
                        # Still near peak — let trailing stop handle the exit
                        self.logger.info(
                            "NEW LISTING 12h: %s still trending (%.2f%% from peak) — holding, trailing stop active",
                            symbol, (peak - current_price) / peak * 100,
                        )
                    else:
                        self.logger.info(
                            "NEW LISTING EXIT: %s — 12h expired, not trending — force-selling", symbol
                        )
                        if qty > 0:
                            self.execute_sell_order(
                                pair, current_price,
                                require_profit_target=False,
                                reason="NEW_LISTING_12HR_EXIT"
                            )
                        if pair in self.trade_pairs and pair not in self._core_trade_pairs:
                            self.trade_pairs.remove(pair)
                        to_remove.append(symbol)
            else:
                # Check if expired without buying
                if _listing_expired(entry, self._listing_hold_hours):
                    self.logger.info("NEW LISTING EXPIRED (no buy): %s", symbol)
                    to_remove.append(symbol)
                    continue

                # Wait 15 min after detection before buying (let initial price settle)
                minutes_since_detection = (now - entry.get("detected_at", now)) / 60
                if minutes_since_detection < 15:
                    continue

                # Check trend — buy if up 2%+ from detection price
                if _listing_trending_up(entry, current_price, self._listing_trend_pct):
                    existing = self.position_qty.get(pair, self.holdings.get(pair, 0))
                    if existing <= 0:
                        self.logger.info(
                            "NEW LISTING BUY SIGNAL: %s @ %.6f EUR (+%.1f%% from detection)",
                            symbol, current_price,
                            ((current_price - entry["initial_price"]) / entry["initial_price"]) * 100
                        )
                        # Add to trade_pairs so TP/SL and price feeds monitor it
                        if pair not in self.trade_pairs:
                            self.trade_pairs.append(pair)
                            self.pair_prices[pair] = current_price
                            self.logger.info("NEW LISTING: added %s to trade_pairs for monitoring", pair)
                        self._breakout_timestamps[pair] = time.time()
                        self.execute_buy_order(pair, current_price)
                        _mark_bought(self._listing_watchlist, symbol, current_price)

        for symbol in to_remove:
            _remove_listing(self._listing_watchlist, symbol)

        # ── CoinGecko pre-watchlist ───────────────────────────────────────────
        # Poll CoinGecko every 30 min for newly listed coins
        if now - self._coingecko_last_check >= self._coingecko_check_interval:
            self._coingecko_last_check = now
            try:
                new_cg_coins = _fetch_coingecko_new(per_page=100)
                for coin in new_cg_coins:
                    if _add_to_prewatchlist(self._coingecko_prewatchlist, coin):
                        try:
                            _notifier.send(
                                f"[COINGECKO] New coin spotted: {coin['name']} ({coin['symbol']})\n"
                                f"Monitoring Kraken for listing — will auto-buy if listed within 24h"
                            )
                        except Exception:
                            pass
            except Exception as exc:
                self.logger.debug("CoinGecko pre-watchlist poll error: %s", exc)

        # Check pre-watchlist coins against Kraken every 5 min
        if self._coingecko_prewatchlist and \
                now - self._prewatchlist_kraken_check >= self._prewatchlist_kraken_interval:
            self._prewatchlist_kraken_check = now
            try:
                newly_listed = _check_prewatchlist(
                    self._coingecko_prewatchlist, self.api_client
                )
                for listing in newly_listed:
                    symbol = listing["symbol"]
                    if symbol in self._listing_watchlist:
                        continue
                    # Resolve confirmed price
                    md = self.api_client.get_market_data(listing["kraken_pair"])
                    if not md:
                        continue
                    key = next(iter(md), None)
                    if not key:
                        continue
                    initial_price = float(md[key]["c"][0])
                    if initial_price <= 0:
                        continue
                    if _add_to_watchlist(self._listing_watchlist, listing, initial_price):
                        self.logger.info(
                            "COINGECKO→KRAKEN: %s now live as %s @ %.6f — entering 30min wait",
                            symbol, listing["kraken_pair"], initial_price,
                        )
                        try:
                            _notifier.send(
                                f"[NEW LISTING] {listing.get('name', symbol)} ({symbol})\n"
                                f"Pair: {listing['kraken_pair']} @ {initial_price:.6f}\n"
                                f"Source: CoinGecko pre-watchlist\n"
                                f"Buying in 30 min if +2% trend"
                            )
                        except Exception:
                            pass
            except Exception as exc:
                self.logger.debug("Pre-watchlist Kraken check error: %s", exc)

    def analyze_all_pairs(self) -> tuple:
        """Orchestrate the three-phase pair analysis pipeline.

        Calls:
          1. ``_fetch_market_data()``  â€” prices + airbag
          2. ``_generate_signals()``   â€” RSI / BB / ATR signal engine
          3. ``_select_best_pair()``   â€” pick highest-score actionable pair

        Returns (best_pair, best_signal, best_score).
        """
        self._fetch_market_data()
        self._generate_signals()
        return self._select_best_pair()

    def start_trading(self, max_runtime_seconds=None):
        """Run the main trading loop.

        Delegates to four extracted phase methods to keep this method readable:
          _run_initialization()       â€” startup logging, balance fetch, account sync
          _check_market_intelligence() â€” AI panel refresh (background thread)
          _manage_portfolio_risk()    â€” drawdown, bear shield, monthly tracker
          _handle_trade_execution()   â€” BUY/SELL gate and order dispatch
        """
        self._run_initialization(max_runtime_seconds)

        _start_ts = time.time()

        try:
            iteration = 0
            while True:
                iteration += 1
                try:
                    # â”€â”€ Phase 1: AI panel refresh (background thread) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                    self._check_market_intelligence()

                    # â”€â”€ Phase 2: Balance + regime flags â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                    current_balance = self.get_eur_balance()
                    self._last_balance_eur = current_balance
                    self._btc_downtrend = self._is_btc_downtrend()

                    # Detect full market regime and switch strategy accordingly
                    self._current_market_regime = self._detect_market_regime()
                    _rc = self._regime_strategy_config()
                    self.analysis_tool.enable_mr_signals    = _rc.get('enable_mr', True)
                    self.analysis_tool.enable_trend_signals = _rc.get('enable_trend', True)

                    # Force buy/sell demo triggers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                    _data_dir = os.path.join(os.path.dirname(__file__), 'data')
                    if os.path.exists(os.path.join(_data_dir, 'FORCE_BUY')):
                        try:
                            os.remove(os.path.join(_data_dir, 'FORCE_BUY'))
                            _fp = next(
                                (p for p in self.trade_pairs
                                 if not self.holdings.get(p, 0) and not self.short_qty.get(p, 0)),
                                self.trade_pairs[0] if self.trade_pairs else None
                            )
                            if _fp and self.pair_prices.get(_fp, 0) > 0:
                                self.logger.info("FORCE_BUY: %s @ %.4f", _fp, self.pair_prices[_fp])
                                self._breakout_timestamps[_fp] = time.time()
                                self.execute_buy_order(_fp, self.pair_prices[_fp])
                        except Exception as exc:
                            self.logger.warning("FORCE_BUY error: %s", exc)

                    if os.path.exists(os.path.join(_data_dir, 'FORCE_SELL')):
                        try:
                            os.remove(os.path.join(_data_dir, 'FORCE_SELL'))
                            for _fp in list(self.trade_pairs):
                                _fqty = self.position_qty.get(_fp, self.holdings.get(_fp, 0))
                                _fprice = self.pair_prices.get(_fp, 0)
                                if _fqty > 0 and _fprice > 0:
                                    self.logger.info("FORCE_SELL: %s @ %.4f", _fp, _fprice)
                                    self.execute_sell_order(_fp, _fprice, require_profit_target=False)
                        except Exception as exc:
                            self.logger.warning("FORCE_SELL error: %s", exc)

                    # Manual sell for individual pair (from dashboard button)
                    for _fp in list(self.trade_pairs):
                        _ms_file = os.path.join(_data_dir, f'FORCE_SELL_{_fp}')
                        if os.path.exists(_ms_file):
                            try:
                                os.remove(_ms_file)
                                _fqty   = self.position_qty.get(_fp, self.holdings.get(_fp, 0))
                                _fprice = self.pair_prices.get(_fp, 0)
                                if _fqty > 0 and _fprice > 0:
                                    self.logger.info("MANUAL_SELL: %s @ %.4f", _fp, _fprice)
                                    self.execute_sell_order(_fp, _fprice, require_profit_target=False, reason="MANUAL_SELL")
                                else:
                                    self.logger.info("MANUAL_SELL: %s — no position to sell", _fp)
                            except Exception as exc:
                                self.logger.warning("MANUAL_SELL error for %s: %s", _fp, exc)


                    # Manual close for individual short (from dashboard button)
                    for _fp in list(self.trade_pairs):
                        _sc_file = os.path.join(_data_dir, f'FORCE_SHORT_CLOSE_{_fp}')
                        if os.path.exists(_sc_file):
                            try:
                                os.remove(_sc_file)
                                _sqty = self.short_qty.get(_fp, 0.0)
                                _sprice = self.pair_prices.get(_fp, 0)
                                if _sqty > 0 and _sprice > 0:
                                    self.logger.info("MANUAL_SHORT_CLOSE: %s @ %.4f", _fp, _sprice)
                                    self.execute_close_short_order(_fp, _sprice)
                                else:
                                    self.logger.info("MANUAL_SHORT_CLOSE: %s -- no short to close", _fp)
                            except Exception as exc:
                                self.logger.warning("MANUAL_SHORT_CLOSE error for %s: %s", _fp, exc)
                    # â”€â”€ Phase 3: Portfolio risk management â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                    regime_state, pause_state, adjusted_pnl, portfolio_value, holdings_value = self._manage_portfolio_risk(current_balance)

                    # â”€â”€ Stop conditions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                    if current_balance >= self.target_balance_eur:
                        self.logger.info("TARGET REACHED! Balance: %.2f EUR", current_balance)
                        print(f"\nTARGET REACHED! Balance: {current_balance:.2f} EUR")
                        break
                    if max_runtime_seconds and (time.time() - _start_ts) >= max_runtime_seconds:
                        self.logger.info("MAX RUNTIME REACHED (%ds). Balance: %.2f EUR",
                                         max_runtime_seconds, current_balance)
                        print(f"\nMax runtime reached. Final balance: {current_balance:.2f} EUR")
                        break

                    # â”€â”€ Phase 4: Signal generation and pair selection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                    best_pair, best_signal, best_score = self.analyze_all_pairs()
                    self._sync_account_state()
                    self.sentiment_active = (self._scan_news_sentiment()
                                             if self.enable_sentiment_guard else False)

                    # â”€â”€ Phase 5: TP/SL exits and partial exits â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                    risk_pair, risk_type, change = self.check_take_profit_or_stop_loss()
                    if risk_pair:
                        _price = self.pair_prices.get(risk_pair, 0)
                        print(f"\n[{risk_type}] {risk_pair} at {change:.2f}%")
                        if risk_type in ("SHORT_TAKE_PROFIT", "SHORT_STOP_LOSS", "SHORT_TIME_REVIEW"):
                            self.execute_close_short_order(risk_pair, _price)
                        else:
                            self.execute_sell_order(risk_pair, _price,
                                                    require_profit_target=True, reason=risk_type)

                    if self.enable_partial_exit:
                        for _pp in list(self.trade_pairs):
                            if self._partial_exit_done.get(_pp):
                                continue
                            _pp_qty = self.holdings.get(_pp, 0.0)
                            if _pp_qty < self._get_min_volume(_pp):
                                continue
                            _pp_price = self.pair_prices.get(_pp, 0.0)
                            if _pp_price <= 0:
                                continue
                            _pp_pct = self._profit_percent_from_entry(_pp, _pp_price)
                            if _pp_pct is not None and _pp_pct >= self.partial_exit_trigger_pct:
                                self._execute_partial_exit(_pp, _pp_price)

                    # â”€â”€ Status log â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                    label_map = {
                        "XBTEUR": "BTC", "XXBTZEUR": "BTC", "ETHEUR": "ETH",
                        "XETHZEUR": "ETH", "SOLEUR": "SOL", "XRPEUR": "XRP",
                        "XXRPZEUR": "XRP", "ADAEUR": "ADA", "DOTEUR": "DOT",
                    }
                    pair_status = " ".join(
                        f"{label_map.get(p, p[:4])}:{self.pair_signals.get(p, '?')}"
                        for p in self.trade_pairs
                    )
                    status_msg = (
                        f"[{iteration}] {pair_status} | {regime_state}/{pause_state} "
                        f"| Best: {best_pair or 'NONE'} ({best_signal}) "
                        f"| Bal: {current_balance:.2f}EUR | Start: {self.initial_balance_eur:.2f}EUR "
                        f"| NetCF: +{self.net_deposits_eur:.2f}/-{self.net_withdrawals_eur:.2f}EUR "
                        f"| AdjPnL: {adjusted_pnl:+.2f}EUR "
                        f"| TotalPnL: {self.cumulative_pnl_eur(current_balance):+.2f}EUR "
                        f"| Trades: {self.trade_count}"
                    )
                    self.logger.info(status_msg)
                    print(f"\r{status_msg}", end="", flush=True)

                    # â”€â”€ Dashboard status.json â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                    try:
                        _status = {
                            "ts":             datetime.utcnow().isoformat(),
                            "loop":           iteration,
                            "paper_mode":     bool(getattr(self.api_client, 'paper_mode', True)),
                            "balance_eur":    round(float(current_balance), 2),
                            "portfolio_value":round(float(portfolio_value), 2),
                            "holdings_value_eur": round(float(holdings_value), 2),
                            "initial_balance":round(float(getattr(self, 'initial_balance_eur', 100.0)), 2),
                            "adjusted_pnl":   round(float(portfolio_value - getattr(self, 'initial_balance_eur', float(current_balance))), 4),
                            "trade_count":    int(getattr(self, 'trade_count', 0)),
                            "pair_signals":   {str(k): str(v) for k, v in self.pair_signals.items()},
                            "pair_scores":    {str(k): round(float(v), 2) for k, v in self.pair_scores.items()},
                            "best_pair":      str(best_pair) if best_pair else None,
                            "best_signal":    str(best_signal) if best_signal else None,
                            "regime":         str(regime_state),
                            "intelligence_score":  round(float(self._intelligence_score), 2),
                            "model_scores":        {k: round(float(v), 2) for k, v in self._intelligence_model_scores.items()},
                            "model_outputs":       {k: str(v)[:120] for k, v in self._intelligence_model_outputs.items()},
                            "sharpe_funding":      self._sharpe_funding_scores,
                            "sharpe_insider":      self._sharpe_insider_scores,
                            "btc_downtrend":       self._btc_downtrend,
                            "short_mode":          "BEAR (5% NAV)" if self._btc_downtrend else "HEDGE (3% NAV)",
                            "market_regime":       self._current_market_regime,
                            "regime_strategy":     self._regime_strategy_config().get('label', 'RANGING'),
                            "circuit_breaker":     bool(self._circuit_breaker_triggered),
                            "peak_balance":        round(float(getattr(self, 'peak_balance', 0)), 2),
                            "dynamic_tp_pct":      self._dynamic_take_profit_percent(),
                            "dynamic_sl_pct":      self._dynamic_stop_loss_percent(),
                            "kelly_fraction":      round(getattr(self, 'kelly_fraction', 0.1), 3),
                            "kelly_multiplier":    round(max(0.3, min(1.5, getattr(self,'kelly_fraction',0.1)*0.5/0.1)), 2),
                            "correlated_open":     sum(
                                1 for p in self.trade_pairs
                                if (self.position_qty.get(p, 0) or self.holdings.get(p, 0)) >= self._get_min_volume(p)
                            ),
                            "monthly_return_pct":  round(self._monthly_return_pct(portfolio_value), 2),
                            "monthly_start_bal":   round(self._monthly_start_balance, 2),
                            "monthly_target_low":  3.0,
                            "monthly_target_high": 8.0,
                            "breakout_ages_days":  {
                                p: round((time.time() - ts) / 86400, 1)
                                for p, ts in self._breakout_timestamps.items() if ts > 0
                            },
                            "sharpe":         self._sharpe_result.get('sharpe'),
                            "sharpe_verdict": self._sharpe_result.get('verdict', 'insufficient_data'),
                            "sharpe_trending":self._sharpe_result.get('trending', 'stable'),
                            "sharpe_n_trades":self._sharpe_result.get('n_trades', 0),
                            "optimizer":      self._get_optimizer_status(),
                            "open_positions": {},
                            "open_shorts":    {},
                        }
                        for _p in self.trade_pairs:
                            try:
                                _pos_qty = float(self.position_qty.get(_p, 0) or self.holdings.get(_p, 0))
                                if _pos_qty > 0:
                                    _entry = float(self.purchase_prices.get(_p, 0))
                                    _cur   = float(self.pair_prices.get(_p, 0))
                                    _pnl_pct = round(((_cur - _entry) / _entry) * 100, 3) if _entry > 0 else 0.0
                                    _pnl_eur = round((_cur - _entry) * _pos_qty, 4) if _entry > 0 else 0.0
                                    _tp_pct  = self._required_take_profit_percent(_p)
                                    _sl_pct  = self._dynamic_stop_loss_percent()
                                    _tp_price = round(_entry * (1 + _tp_pct / 100), 4) if _entry > 0 else 0
                                    _sl_price = round(_entry * (1 - _sl_pct / 100), 4) if _entry > 0 else 0
                                    _status["open_positions"][_p] = {
                                        "qty":      round(_pos_qty, 8),
                                        "entry":    round(_entry, 4),
                                        "current":  round(_cur, 4),
                                        "pnl_pct":  _pnl_pct,
                                        "pnl_eur":  _pnl_eur,
                                        "tp_price": _tp_price,
                                        "sl_price": _sl_price,
                                        "tp_pct":   round(_tp_pct, 2),
                                        "sl_pct":   round(_sl_pct, 2),
                                    }
                                if float(self.short_qty.get(_p, 0)) > 0:
                                    _s_qty   = float(self.short_qty.get(_p, 0))
                                    _s_entry = float(self.short_entry_prices.get(_p, 0))
                                    _s_cur   = float(self.pair_prices.get(_p, 0))
                                    _s_tp_pct = self._required_take_profit_percent(_p)
                                    _s_sl_pct = self._dynamic_stop_loss_percent()
                                    _s_pnl_pct = round(((_s_entry - _s_cur) / _s_entry) * 100, 3) if _s_entry > 0 else 0.0
                                    _s_pnl_eur = round((_s_entry - _s_cur) * _s_qty, 4) if _s_entry > 0 else 0.0
                                    _status["open_shorts"][_p] = {
                                        "qty":      round(_s_qty, 8),
                                        "entry":    round(_s_entry, 4),
                                        "current":  round(_s_cur, 4),
                                        "pnl_pct":  _s_pnl_pct,
                                        "pnl_eur":  _s_pnl_eur,
                                        "tp_price": round(_s_entry * (1 - _s_tp_pct / 100), 4) if _s_entry > 0 else 0,
                                        "sl_price": round(_s_entry * (1 + _s_sl_pct / 100), 4) if _s_entry > 0 else 0,
                                        "tp_pct":   round(_s_tp_pct, 2),
                                        "sl_pct":   round(_s_sl_pct, 2),
                                    }
                            except Exception:
                                pass
                        # New listing positions
                        _listings_display = {}
                        try:
                            for _sym, _wl in getattr(self, '_listing_watchlist', {}).items():
                                _lpair = _wl.get("kraken_pair", _sym + "EUR")
                                _lcur  = float(self.pair_prices.get(_lpair, 0) or 0)
                                # If pair not in main price feed, fetch directly
                                if _lcur == 0:
                                    try:
                                        _lmd = self.api_client.get_market_data(_lpair)
                                        if _lmd:
                                            _lk = next(iter(_lmd), None)
                                            if _lk:
                                                _lcur = float(_lmd[_lk]["c"][0])
                                    except Exception:
                                        pass
                                _linit = float(_wl.get("initial_price", 0) or 0)
                                _lqty  = float(self.position_qty.get(_lpair, 0) or 0)
                                _lbuy  = float(_wl.get("buy_price") or 0)
                                _listed_at = _wl.get("listed_at", 0)
                                _detected  = _wl.get("detected_at") or time.time()
                                _ref_ts    = (_wl.get("buy_ts") or
                                              (min(_listed_at, _detected) if _listed_at > 0 else _detected))
                                _hours_left = max(0, self._listing_hold_hours - (time.time() - _ref_ts) / 3600)
                                _pnl_pct = round(((_lcur - _lbuy) / _lbuy) * 100, 2) if _lbuy > 0 and _lcur > 0 else 0.0
                                _listings_display[_sym] = {
                                    "pair":        _lpair,
                                    "bought":      _wl.get("bought", False),
                                    "qty":         round(_lqty, 8),
                                    "initial_price": round(_linit, 6),
                                    "buy_price":   round(_lbuy, 6),
                                    "current":     round(_lcur, 6),
                                    "pnl_pct":     _pnl_pct,
                                    "hours_left":  round(_hours_left, 1),
                                    "listed_at":   _wl.get("listed_at", 0),
                                }
                        except Exception:
                            pass
                        _status["new_listings"]     = _listings_display
                        _status["kraken_headlines"] = getattr(self, '_kraken_headlines', [])
                        _status["coingecko_prewatchlist"] = {
                            sym: {
                                "name": e.get("name", sym),
                                "detected_at": e.get("detected_at", 0),
                                "expires_in_min": round(max(0, e.get("expires_at", 0) - time.time()) / 60, 0),
                            }
                            for sym, e in getattr(self, '_coingecko_prewatchlist', {}).items()
                        }

                        # Social sentiment — run every loop (data cached 5 min internally)
                        if _LUNAR_STATUS_AVAILABLE:
                            try:
                                _lc = _fetch_lunar_status(self.trade_pairs)
                                if _lc.get("available"):
                                    _status["lunarcrush"] = {
                                        "combined":     _lc.get("combined", 0),
                                        "fear_greed":   _lc.get("fear_greed"),
                                        "trending_now": _lc.get("trending_now", []),
                                        "coins": {
                                            p: {
                                                "symbol":       v.get("symbol", p),
                                                "is_trending":  v.get("is_trending", False),
                                                "change_24h":   v.get("change_24h", 0),
                                                "signal":       v.get("signal", 0),
                                                "reddit_count": v.get("reddit_count", 0),
                                            }
                                            for p, v in _lc.get("coins", {}).items()
                                        },
                                    }
                                    # Cache combined score for buy gate use
                                    self._lunarcrush_combined = float(_lc.get("combined", 0))
                            except Exception:
                                pass

                        # On-chain data — refresh every loop (data is internally cached 5 min)
                        if _ONCHAIN_AVAILABLE:
                            try:
                                _oc = _fetch_onchain_status()
                                if _oc.get("available") or _oc.get("btc_network"):
                                    _status["onchain"] = {
                                        "btc_score":       _oc.get("btc_network", {}).get("combined_score", 0),
                                        "btc_tx_24h":      _oc.get("btc_network", {}).get("n_tx_24h", 0),
                                        "btc_mempool":     _oc.get("btc_network", {}).get("mempool_size", 0),
                                        "eth_gas":         _oc.get("eth_gas", {}).get("gas_gwei") or _oc.get("eth_gas", {}).get("fast_gas_gwei", 0),
                                        "eth_signal":      _oc.get("eth_gas", {}).get("combined") or _oc.get("eth_gas", {}).get("gas_signal", 0),
                                        "btc_flow_signal": _oc.get("btc_flows", {}).get("combined") or _oc.get("btc_flows", {}).get("flow_signal"),
                                        "combined":        _oc.get("combined_score", 0),
                                    }
                                    # Cache combined score for buy gate use
                                    self._onchain_combined = float(_oc.get("combined_score", 0))
                            except Exception as _oce:
                                self.logger.warning("On-chain status update failed: %s", _oce)

                        # Alpaca positions
                        if _ALPACA_ENABLED and _alpaca_available():
                            try:
                                _ac = _alpaca_client()
                                _apv = _ac.get_portfolio_value()
                                _apos = _ac.get_positions()
                                _status["alpaca"] = {
                                    "portfolio_value": round(_apv, 2),
                                    "market_open": _ac.is_market_open(),
                                    "positions": [
                                        {
                                            "symbol":    p.get("symbol"),
                                            "qty":       float(p.get("qty", 0)),
                                            "market_value": float(p.get("market_value", 0)),
                                            "unrealized_pl": float(p.get("unrealized_pl", 0)),
                                            "unrealized_plpc": round(float(p.get("unrealized_plpc", 0)) * 100, 2),
                                        }
                                        for p in (_apos or [])
                                    ],
                                }
                            except Exception:
                                pass

                        if _HISTORY_DB_AVAILABLE:
                            try:
                                _status["db_stats"] = _get_db_stats()
                            except Exception:
                                pass
                        # Scalper status (if engine is attached via main.py)
                        try:
                            _sc = getattr(self, '_scalper', None)
                            if _sc is not None:
                                _status["scalper"] = _sc.get_status()
                        except Exception:
                            pass
                        # Ichimoku + Gaussian signals for dashboard
                        if _ICHI_AVAILABLE:
                            try:
                                _ichi_status = {}
                                for _ip in self.trade_pairs:
                                    _isig = _ichi_get_signal(_ip, self.api_client)
                                    _ichi_status[_ip] = _isig
                                _status["ichi"] = _ichi_status
                            except Exception:
                                pass
                        with open(os.path.join(os.path.dirname(__file__), 'data', 'bot_status.json'), 'w') as _sf:
                            json.dump(_status, _sf)
                        self._save_balance_state(_status.get("portfolio_value", 0.0))
                        self.logger.debug("Dashboard status written (loop %d)", iteration)
                    except Exception as exc:
                        self.logger.warning("bot_status.json write failed: %s", exc)

                    # â”€â”€ Periodic metrics (every 10 loops) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                    if iteration % 10 == 0:
                        if _HISTORY_DB_AVAILABLE:
                            try:
                                _record_bot_snapshot(
                                    ts=datetime.utcnow().isoformat(), loop=iteration,
                                    balance=float(current_balance),
                                    trade_count=int(self.trade_count),
                                    sharpe=self._sharpe_result.get('sharpe'),
                                    verdict=self._sharpe_result.get('verdict', 'insufficient_data'),
                                    regime=str(regime_state),
                                    intel_score=float(self._intelligence_score),
                                    signals=dict(self.pair_signals),
                                    paper_mode=bool(getattr(self.api_client, 'paper_mode', True)),
                                )
                            except Exception:
                                pass
                        metric_parts = [
                            f"{p}: WR {(m.get('wins',0)/max(m.get('closed',1),1))*100:.0f}% "
                            f"avg {m.get('sum_pnl',0)/max(m.get('closed',1),1):.2f}EUR"
                            for p in self.trade_pairs
                            if (m := self.trade_metrics.get(p, {})) and m.get('closed', 0) > 0
                        ]
                        if metric_parts:
                            self.logger.info("METRICS | " + " | ".join(metric_parts))
                        try:
                            self._auto_cancel_old_maker_orders()
                        except Exception as exc:
                            self.logger.debug("Auto-cancel check failed: %s", exc)

                    # â”€â”€ Phase 6: Trade execution â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                    # New listings strategy (runs every loop, polls API every 10 min)
                    self._handle_new_listing_cycle()

                    # Alpaca correlated stocks (mirrors strong Kraken signals)
                    self._handle_alpaca_correlations(best_pair, best_signal, best_score)

                    self._handle_trade_execution(best_pair, best_signal, best_score)

                    # Config hot-reload â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                    if (datetime.now() - self.last_config_reload).total_seconds() >= self.config_reload_interval:
                        self.reload_config()

                except Exception as e:
                    self.logger.error(
                        f"Unhandled error in trading loop (iteration {iteration}): {e}",
                        exc_info=True,
                    )
                finally:
                    # This MUST run on every pass through the loop body --
                    # including all of the `continue` statements scattered
                    # through the buy/sell gate checks above. Previously this
                    # sleep lived after the try/except as a sibling statement,
                    # so every `continue` jumped straight back to `while True`
                    # and skipped it entirely. That caused the loop to spin

                    _sd_notify_watchdog()
                    time.sleep(self.loop_interval_sec)

        except KeyboardInterrupt:
            final_balance = self.get_eur_balance()
            self.logger.info(f"Bot stopped by user. Final balance: {final_balance:.2f} EUR")
            print(f"\nTrading bot stopped. Final Balance: {final_balance:.2f} EUR")

    # â”€â”€ Trade finalisation helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _persist_position_meta(self, meta: dict) -> None:
        """Atomically write a position entry to JSON file + PostgreSQL (dual-write)."""
        pair = meta.get('pair', '')
        try:
            os.makedirs(os.path.dirname(self.data_purchase_prices_path), exist_ok=True)
            existing: dict = {}
            if os.path.exists(self.data_purchase_prices_path):
                try:
                    with open(self.data_purchase_prices_path, 'r', encoding='utf-8') as fh:
                        existing = json.load(fh)
                except (json.JSONDecodeError, OSError):
                    existing = {}
            existing[pair] = meta
            tmp = self.data_purchase_prices_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(existing, fh, separators=(',', ':'), ensure_ascii=False)
            os.replace(tmp, self.data_purchase_prices_path)
        except OSError as exc:
            self.logger.warning("Could not persist position meta for %s: %s", pair, exc)
        # Dual-write to PostgreSQL
        if _PG_AVAILABLE:
            _mode = 'paper' if getattr(self.api_client, 'paper_mode', True) else 'live'
            _pg.save_position(
                pair=pair,
                qty=float(meta.get('qty', 0)),
                entry_price=float(
                    meta.get('last_buy') or
                    meta.get('price') or
                    meta.get('entry_price') or 0
                ),
                mode=_mode,
                meta=meta,
            )

    def _remove_position_meta(self, pair: str) -> None:
        """Atomically remove a position entry from the purchase-prices state file.
        Replaces duplicated removal logic in execute_sell_order and
        execute_close_short_order."""
        try:
            if not os.path.exists(self.data_purchase_prices_path):
                return
            try:
                with open(self.data_purchase_prices_path, 'r', encoding='utf-8') as fh:
                    existing = json.load(fh)
            except (json.JSONDecodeError, OSError):
                return
            if pair not in existing:
                return
            del existing[pair]
            tmp = self.data_purchase_prices_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(existing, fh, separators=(',', ':'), ensure_ascii=False)
            os.replace(tmp, self.data_purchase_prices_path)
        except OSError as exc:
            self.logger.warning("Could not remove position meta for %s: %s", pair, exc)
        # Dual-delete from PostgreSQL
        if _PG_AVAILABLE:
            _mode = 'paper' if getattr(self.api_client, 'paper_mode', True) else 'live'
            _pg.delete_position(pair=pair, mode=_mode)

    def _finalise_trade(self, ttype: str, pair: str, volume: float, price: float,
                        pnl_eur: float = 0.0, reason: str = '',
                        extra: dict = None) -> None:
        """
        Consolidate all post-execution steps common across every trade type:
          1. Increment trade count and update cooldown timestamps
          2. Journal to CSV + JSONL + history DB (via _journal_trade)
          3. Log a summary line
          4. Send Telegram notification

        Call this AFTER all position-specific state updates are complete.
        Replaces ~20 lines of duplicated boilerplate in each execute_* method.
        """
        now_ts = time.time()
        self.trade_count += 1
        self.last_trade_at[pair] = now_ts
        self.last_global_trade_at = now_ts
        self._save_cooldown_state()

        # Journal (CSV + JSONL + history DB)
        self._journal_trade(
            ttype, pair, volume, price, pnl_eur,
            reason or f'{ttype}_EXECUTED',
            extra=extra or {},
        )

        # Console summary
        notional  = volume * price
        pnl_str   = f" | P&L: {pnl_eur:+.2f} EUR" if pnl_eur != 0 else ""
        print(f"\n[{ttype}] {volume:.6f} {pair} (~{notional:.2f} EUR){pnl_str} - Trade #{self.trade_count}")
        self.logger.info("%s FINALISED: %s %.6f @ %.4f EUR%s (trade #%d)",
                         ttype, pair, volume, price, pnl_str, self.trade_count)

        # Telegram notification â€” format varies by trade type
        sign = "[+]" if pnl_eur >= 0 else "ðŸ”´"
        lev  = getattr(self, 'short_leverage', '2')
        ext  = extra or {}

        if ttype == 'BUY':
            msg = (f"[+] <b>BUY</b> #{self.trade_count}\n"
                   f"Pair: {pair}\n"
                   f"Volume: {volume:.6f}  (~{notional:.2f} EUR)\n"
                   f"Price: {price:.4f} EUR")
        elif ttype == 'SELL':
            msg = (f"{sign} <b>SELL</b> #{self.trade_count}\n"
                   f"Pair: {pair}\n"
                   f"Volume: {volume:.6f}  (~{notional:.2f} EUR)\n"
                   f"Price: {price:.4f} EUR\n"
                   f"P&amp;L est.: {pnl_eur:+.2f} EUR")
        elif ttype == 'SHORT_OPEN':
            msg = (f"ðŸ”» <b>SHORT OPEN</b> #{self.trade_count}\n"
                   f"Pair: {pair}\n"
                   f"Volume: {volume:.6f}  (~{notional:.2f} EUR)\n"
                   f"Price: {price:.4f} EUR  |  Leverage: {lev}x")
        elif ttype == 'SHORT_CLOSE':
            entry   = ext.get('entry', 0)
            pnl_pct = ext.get('pnl_pct', 0)
            msg = (f"{sign} <b>SHORT CLOSE</b> #{self.trade_count}\n"
                   f"Pair: {pair}\n"
                   f"Volume: {volume:.6f}  (~{notional:.2f} EUR)\n"
                   f"Entry: {entry:.4f} EUR  |  Exit: {price:.4f} EUR\n"
                   f"P&amp;L est.: {pnl_eur:+.2f} EUR ({pnl_pct:+.2f}%)")
        else:
            msg = (f"{sign} <b>{ttype}</b> #{self.trade_count}\n"
                   f"Pair: {pair}\nVolume: {volume:.6f}\nPrice: {price:.4f} EUR")

        try:
            _notifier.send(msg)
        except Exception:
            pass

    def _journal_trade(self, ttype, pair, volume, price, pnl_eur, reason, extra=None):
        """Journal a trade: append CSV and JSONL entries with safety measures.

        CSV write and JSONL append are independent to avoid single-point failures.
        """
        # CSV journaling (best-effort)
        try:
            import csv, os, datetime, json
            os.makedirs(os.path.dirname(self.journal_path), exist_ok=True)
            header = ['ts', 'type', 'pair', 'volume', 'price', 'pnl_eur', 'reason', 'extra']
            exists = os.path.exists(self.journal_path)
            with open(self.journal_path, 'a', newline='') as fh:
                writer = csv.writer(fh)
                if not exists:
                    writer.writerow(header)
                row = [datetime.datetime.utcnow().isoformat(), ttype, pair, f"{volume:.8f}", f"{price:.6f}", f"{pnl_eur:.6f}", reason, str(extra or '')]
                writer.writerow(row)
        except Exception as e:
            self.logger.error(f"Error writing trade journal CSV: {e}")

        # JSONL structured journaling (use locked append helper with fallback)
        try:
            os.makedirs(os.path.dirname(self.json_journal_path), exist_ok=True)
            j = {
                'ts': datetime.datetime.utcnow().isoformat(),
                'type': ttype,
                'pair': pair,
                'volume': float(volume),
                'price': float(price),
                'pnl_eur': float(pnl_eur),
                'reason': reason,
                'extra': extra or {},
                'balance_eur': float(self.get_eur_balance()),
                'consecutive_losses': int(self.consecutive_losses),
            }
            # include current drawdown if available
            try:
                peak = float(getattr(self, 'peak_balance', j['balance_eur']))
                if peak > 0:
                    j['current_drawdown_pct'] = round(((peak - j['balance_eur']) / peak) * 100.0, 2)
            except Exception:
                pass

            ok = False
            try:
                ok = append_jsonl_locked(self.json_journal_path, j)
            except Exception:
                ok = False

            if not ok:
                # fallback: plain append (best-effort)
                try:
                    with open(self.json_journal_path, 'a', encoding='utf-8') as jf:
                        jf.write(json.dumps(j) + "\n")
                except Exception as e:
                    self.logger.error(f"Error writing JSON trade log fallback: {e}")

            # â”€â”€ Write to persistent history DB â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            if _HISTORY_DB_AVAILABLE:
                try:
                    _record_trade(
                        ts=j.get('ts', ''),
                        ttype=ttype,
                        pair=pair,
                        qty=float(volume),
                        price=float(price),
                        pnl_eur=float(pnl_eur),
                        balance_after=float(j.get('balance_eur', 0)),
                        paper_mode=bool(getattr(self.api_client, 'paper_mode', True)),
                        reason=reason or '',
                    )
                    if ttype in ('SELL', 'SHORT_CLOSE') and pnl_eur != 0:
                        _outcome = f"{'WIN' if pnl_eur > 0 else 'LOSS'} {pnl_eur:+.4f}EUR on {pair}"
                        _update_ai_outcome(_outcome)
                except Exception:
                    pass

            # â”€â”€ Retrospectively mark the last intelligence log entry â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            if ttype in ('SELL', 'SHORT_CLOSE'):
                try:
                    _intel_log_path = os.path.join(os.path.dirname(__file__), 'data', 'intelligence_log.jsonl')
                    if os.path.exists(_intel_log_path):
                        with open(_intel_log_path, 'r', encoding='utf-8') as _ilf:
                            _il_lines = _ilf.readlines()
                        if _il_lines:
                            _last = json.loads(_il_lines[-1])
                            if _last.get('market_outcome') == 'pending':
                                _outcome = f"{'WIN' if pnl_eur > 0 else 'LOSS' if pnl_eur < 0 else 'FLAT'} {pnl_eur:+.4f}EUR on {pair}"
                                _last['market_outcome'] = _outcome
                                _il_lines[-1] = json.dumps(_last) + '\n'
                                with open(_intel_log_path, 'w', encoding='utf-8') as _ilf:
                                    _ilf.writelines(_il_lines)
                except Exception:
                    pass

            # â”€â”€ Sharpe + optimizer update on closed trades â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            if ttype in ('SELL', 'SHORT_CLOSE') and _OPTIMIZER_AVAILABLE:
                try:
                    self._closed_trades_count += 1
                    # Update Kelly fraction from latest trade history
                    self.kelly_fraction = self._calculate_kelly_fraction()
                    # Recalculate Sharpe every 5 closed trades (not just optimizer intervals)
                    if self._closed_trades_count % 5 == 0 or self._closed_trades_count < 10:
                        self._sharpe_result = _calculate_sharpe(self.json_journal_path)
                        s = self._sharpe_result
                        self.logger.info(
                            f"Sharpe update: {s.get('sharpe')} | "
                            f"verdict={s.get('verdict')} | trending={s.get('trending')} | "
                            f"n={s.get('n_trades')}"
                        )
                        if self._optimizer_enabled and self.config_path:
                            opt = _run_optimizer(self.config_path, self._sharpe_result)
                            if opt.get('action') not in ('none', 'waiting'):
                                self.logger.info(f"Optimizer: {opt.get('action')} â€” {opt.get('detail','')}")
                                if opt.get('new_experiment'):
                                    self.logger.info(f"Optimizer new experiment: {opt['new_experiment']}")
                                # Reload config so the changed parameter takes effect
                                try:
                                    self.reload_config()
                                except Exception:
                                    pass
                except Exception as _oe:
                    self.logger.debug(f"Optimizer hook error: {_oe}")

        except Exception as e:
            self.logger.error(f"Error writing JSON trade log: {e}")

    def execute_buy_order(self, pair, price):
        """Place a post-only (maker) spot BUY order for *pair* at *price*.

        Position size is determined by ``_get_dynamic_trade_amount_eur()``
        (allocation % of available EUR, ATR-scaled, regime-adjusted).
        After a successful fill the ATR stop level is initialised and the
        trade is journalled to CSV and JSONL.  Rejects if available EUR is
        below ``min_trade_eur``.
        """
        try:
            available_eur = self._available_eur_for_buy()
            min_trade_eur = float(self.config.get('risk_management', {}).get('min_trade_eur', 10.0))
            planned_eur = self._get_dynamic_trade_amount_eur(pair, available_eur)
            # Early Notional-Guard: avoid attempting orders below the configured
            # min_auto_scale_notional which the execution layer may reject/scale.
            min_auto_notional = float(self.config.get('risk_management', {}).get('min_auto_scale_notional', 1.0))
            if planned_eur < min_auto_notional:
                self.logger.info(
                    f"BUY skipped for {pair}: planned notional {planned_eur:.2f} EUR < min_auto_scale_notional {min_auto_notional:.2f}"
                )
                return

            if planned_eur < min_trade_eur:
                self.logger.info(f"BUY skipped for {pair}: insufficient free EUR ({available_eur:.2f})")
                return

            volume = self._calculate_volume(pair, price, available_eur=planned_eur)
            self.logger.info(f"Placing BUY order (MAKER/POST-ONLY): {volume:.6f} {pair} at {price:.2f} EUR")

            # --- Preflight: spread and depth checks (fail-closed) ---
            try:
                exec_cfg = self.config.get('execution', {}) if isinstance(self.config, dict) else {}
                max_spread_pct = float(exec_cfg.get('max_spread_pct', 0.5))
                min_book_fill_ratio = float(exec_cfg.get('min_book_fill_ratio', 0.5))
                ob = self.api_client.get_order_book(pair, count=3)
                if not ob:
                    self.logger.warning(f"BUY skipped for {pair}: orderbook unavailable (fail-closed)")
                    return
                data_key = next((k for k in ob if k != 'last'), None)
                if not data_key:
                    self.logger.warning(f"BUY skipped for {pair}: orderbook empty (fail-closed)")
                    return
                asks = ob[data_key].get('asks', [])
                bids = ob[data_key].get('bids', [])
                if not asks or not bids:
                    self.logger.warning(f"BUY skipped for {pair}: insufficient orderbook depth (fail-closed)")
                    return
                best_ask = float(asks[0][0])
                best_ask_vol = float(asks[0][1])
                best_bid = float(bids[0][0])
                mid = (best_ask + best_bid) / 2.0 if best_bid and best_ask else None
                if mid is None:
                    self.logger.warning(f"BUY skipped for {pair}: cannot compute midprice (fail-closed)")
                    return
                spread_pct = ((best_ask - best_bid) / mid) * 100.0
                planned_notional = planned_eur
                if spread_pct > max_spread_pct:
                    self.logger.info(f"BUY skipped for {pair}: spread too wide ({spread_pct:.2f}% > {max_spread_pct}%)")
                    return
                # ensure top ask size in EUR is sufficient for at least a fraction of planned trade
                if (best_ask * best_ask_vol) < (planned_notional * min_book_fill_ratio):
                    self.logger.info(f"BUY skipped for {pair}: top ask depth insufficient for planned size ({best_ask*best_ask_vol:.2f} EUR < {planned_notional*min_book_fill_ratio:.2f} EUR)")
                    return
            except Exception as e:
                # Fail-open in paper mode — don't block simulated orders due to API hiccups.
                # In live mode, keep fail-closed to protect against bad fills.
                is_paper = bool(getattr(self.api_client, 'paper_mode', False))
                if is_paper:
                    self.logger.debug(f"Preflight skipped in paper mode ({pair}): {e}")
                else:
                    self.logger.warning(f"Preflight checks failed for BUY {pair}: {e}")
                    return

            prev_qty = self.holdings.get(pair, 0.0)
            result = self._place_live_order(pair=pair, direction='buy', volume=volume, price=price, post_only=True)
            if result:
                # Wait for confirmation that the fill landed (or fallback provided a fill_price)
                confirmed = False
                fill_price = None
                try:
                    if isinstance(result, dict) and 'fill_price' in result:
                        fill_price = float(result.get('fill_price'))
                        confirmed = True
                    elif isinstance(result, dict) and result.get('simulated'):
                        # paper mode may include simulated fill_price
                        fill_price = float(result.get('fill_price')) if result.get('fill_price') else price
                        confirmed = True
                except Exception:
                    fill_price = None
                # Poll account state for up to 15s to confirm holdings changed
                if not confirmed:
                    timeout = 15
                    waited = 0
                    while waited < timeout:
                        time.sleep(1)
                        waited += 1
                        try:
                            self._sync_account_state(force_history=True)
                        except Exception:
                            pass
                        new_qty = self.holdings.get(pair, 0.0)
                        if new_qty - prev_qty >= (volume * 0.95):
                            confirmed = True
                            break
                        # if there is an open order, continue waiting
                        if self._has_open_order(pair, 'buy'):
                            continue
                    # end poll
                if not confirmed:
                    # Treat as pending/unfilled; do not journal state changes
                    remaining = self.holdings.get(pair, 0.0)
                    self.logger.warning(
                        f"BUY order for {pair} accepted but not confirmed (prev={prev_qty:.8f} now={remaining:.8f}). Skipping journal/state update."
                    )
                    return

                # Confirmed â€” update position-specific state
                now_ts = time.time()
                self.peak_prices[pair] = max(self.peak_prices.get(pair, 0.0), price)
                if self.entry_timestamps.get(pair) is None:
                    self.entry_timestamps[pair] = int(now_ts)
                self._partial_exit_done[pair] = False
                self._sync_account_state(force_history=True)
                if self.enable_atr_stop:
                    atr = self._compute_atr(pair)
                    if atr is not None:
                        init_stop = max(0.0, price - (atr * self.atr_multiplier))
                        self.stop_info[pair] = {'stop_price': init_stop, 'type': 'ATR'}
                        self.logger.info("ATR stop for %s: %.4f (atr=%.4f)", pair, init_stop, atr)
                self._persist_position_meta({
                    'pair': pair, 'side': 'long',
                    'qty': float(volume),
                    'entry_price_eur': float(fill_price or price),
                    'fees_eur': float(result.get('fee', 0) if isinstance(result, dict) else 0.0),
                    'notional_eur': float(volume * (fill_price or price)),
                    'entry_ts': int(now_ts),
                })
                self.logger.info("BUY ORDER SUCCESS: %s", result)
                self._finalise_trade('BUY', pair, volume, fill_price or price, 0.0,
                                     'BUY_EXECUTED',
                                     extra={'fill_price': fill_price, 'expected_price': price})
            else:
                self.logger.error(f"BUY ORDER FAILED for {pair}")
        except Exception as e:
            self.logger.error(f"Error executing buy order: {e}", exc_info=True)

    def execute_sell_order(self, pair, price, require_profit_target=True, reason=None):
        """Place a post-only (maker) spot SELL order to close the long position.

        When ``require_profit_target=True`` (default), the sell is blocked
        unless ``_can_sell_profit_target()`` passes (i.e. profit â‰¥ required TP
        after slippage buffer).  Pass ``require_profit_target=False`` for
        emergency exits (airbag, bear shield, time-stop).

        Clears position state, updates trade metrics, and journals the trade.
        """
        try:
            # Use position_qty as primary source â€” holdings can be zeroed by
            # _sync_account_state in paper mode even when a position exists
            volume = self.position_qty.get(pair, 0) or self.holdings.get(pair, 0)
            min_vol = self._get_min_volume(pair)
            if volume < min_vol:
                self.logger.info(f"SELL skipped for {pair}: no holdings")
                return

            if self._has_open_order(pair, 'sell'):
                self.logger.info(f"SELL skipped for {pair}: sell order already open")
                return

            if require_profit_target and not self._can_sell_profit_target(pair, price):
                pp = self._profit_percent_from_entry(pair, price)
                pp_str = f"{pp:.2f}" if pp is not None else 'n/a'
                self.logger.info(
                    f"SELL blocked for {pair}: target {self.take_profit_percent:.2f}% not reached ({pp_str}%)"
                )
                return

            avg_entry = self.purchase_prices.get(pair, 0.0)
            est_profit_pct = self._profit_percent_from_entry(pair, price)
            est_profit_eur = (price - avg_entry) * volume if avg_entry > 0 else 0.0

            self.logger.info(f"Placing SELL order (MAKER/POST-ONLY): {volume:.6f} {pair} at {price:.2f} EUR")
            prev_qty = self.holdings.get(pair, 0.0)
            result = self._place_live_order(pair=pair, direction='sell', volume=volume, price=price, post_only=True)
            if result:
                # Wait briefly for the exchange to reflect the sell (or for a provided fill_price)
                confirmed = False
                fill_price = None
                try:
                    if isinstance(result, dict) and 'fill_price' in result:
                        fill_price = float(result.get('fill_price'))
                        confirmed = True
                    elif isinstance(result, dict) and result.get('simulated'):
                        fill_price = float(result.get('fill_price')) if result.get('fill_price') else price
                        confirmed = True
                except Exception:
                    fill_price = None
                if not confirmed:
                    timeout = 15
                    waited = 0
                    while waited < timeout:
                        time.sleep(1)
                        waited += 1
                        try:
                            self._sync_account_state(force_history=True)
                        except Exception:
                            pass
                        remaining_volume = self.holdings.get(pair, 0.0)
                        if prev_qty - remaining_volume >= (volume * 0.95):
                            confirmed = True
                            break
                        # if open sell orders still present, keep waiting
                        if self._has_open_order(pair, 'sell'):
                            continue
                if not confirmed:
                    self.logger.warning(
                        f"SELL order for {pair} accepted but not confirmed (prev={prev_qty:.8f} now={self.holdings.get(pair,0.0):.8f}). Skipping journal/state update."
                    )
                    return

                # Confirmed â€” update position-specific state then finalise
                self.purchase_prices[pair] = 0.0
                self.position_qty[pair]    = 0.0
                self.peak_prices[pair]     = 0.0
                self.entry_timestamps[pair] = None
                self._partial_exit_done[pair] = False
                self.stop_info.pop(pair, None)
                self._update_trade_metrics(pair, est_profit_eur)
                self._remove_position_meta(pair)
                self.logger.info("SELL ORDER SUCCESS: %s", result)
                self.logger.info("SELL PNL %s: %.2f EUR (%.2f%%)",
                                 pair, est_profit_eur,
                                 est_profit_pct if est_profit_pct is not None else 0)
                self._finalise_trade('SELL', pair, volume, fill_price or price,
                                     est_profit_eur, reason or 'SELL_EXECUTED',
                                     extra={'fill_price': fill_price, 'expected_price': price})
            else:
                self.logger.error(f"SELL ORDER FAILED for {pair}")
        except Exception as e:
            self.logger.error(f"Error executing sell order: {e}", exc_info=True)

    def execute_open_short_order(self, pair, price):
        """Open a leveraged short position on *pair* at *price*.

        Uses the configured ``short_leverage`` (default 2Ã—) via Kraken margin.
        Position notional is capped at ``max_short_notional_eur``.  Only placed
        when no short is already open for this pair.  Blocked when
        ``enable_live_shorts`` is False in config.
        """
        try:
            if not self.enable_live_shorts:
                return
            if self.short_qty.get(pair, 0.0) > 0:
                return

            # Two-tier short sizing (TR-GC inspired):
            # BEAR short (BTC in downtrend) â†' 5% of balance, bigger conviction
            # HEDGE short (neutral/up trend) â†' 3% of balance, defensive
            _balance = self._available_eur_for_buy() + sum(
                self.short_qty.get(p, 0) * self.pair_prices.get(p, 0)
                for p in self.trade_pairs
            )
            _nav = max(_balance, self.get_eur_balance() or _balance)
            if self._btc_downtrend:
                short_type = "BEAR"
                notional = _nav * 0.05   # 5% of NAV â€” BTC regime confirms downtrend
            else:
                short_type = "HEDGE"
                notional = _nav * 0.03   # 3% of NAV â€” defensive hedge short
            notional = min(self.max_short_notional_eur, max(notional, self._get_trade_amount_eur() * 0.3))
            if notional <= 0 or price <= 0:
                return
            volume = max(self._get_min_volume(pair), notional / price)
            self.logger.info(
                f"Placing {short_type} SHORT OPEN order: {volume:.6f} {pair} at ~{price:.2f} EUR "
                f"(lev={self.short_leverage}x, nav={_nav:.2f}, notional={notional:.2f})"
            )
            result = self._place_live_order(pair=pair, direction='sell', volume=volume, leverage=self.short_leverage)
            if result:
                now_ts = time.time()
                self.short_qty[pair] = volume
                self.short_entry_prices[pair] = price
                self.entry_timestamps[pair] = int(now_ts)
                self._persist_position_meta({
                    'pair': pair, 'side': 'short',
                    'qty': float(volume),
                    'entry_price_eur': float(price),
                    'fees_eur': float(result.get('fee', 0) if isinstance(result, dict) else 0.0),
                    'notional_eur': float(volume * price),
                    'entry_ts': int(now_ts),
                })
                self.logger.info("SHORT OPEN SUCCESS: %s", result)
                self._finalise_trade('SHORT_OPEN', pair, volume, price, 0.0,
                                     'SHORT_OPEN_EXECUTED',
                                     extra={'notional': notional, 'short_type': short_type})
            else:
                self.logger.error(f"SHORT OPEN FAILED for {pair}")
        except Exception as e:
            self.logger.error(f"Error opening short order: {e}", exc_info=True)

    def execute_close_short_order(self, pair, price):
        """Close an open leveraged short position on *pair* at *price*.

        Places a reduce-only BUY order with the same leverage as the original
        short.  Computes and records estimated P&L: profit when price fell from
        entry, loss when it rose.  Clears short state and journals the trade.
        """
        try:
            qty = self.short_qty.get(pair, 0.0)
            entry = self.short_entry_prices.get(pair, 0.0)
            if qty <= 0 or entry <= 0:
                return
            # compute pnl using leverage-neutral formula (entry - exit) * qty
            pnl_eur = (entry - price) * qty
            pnl_pct = ((entry - price) / entry) * 100.0
            self.logger.info(f"Placing SHORT CLOSE order: {qty:.6f} {pair} at ~{price:.2f} EUR")
            result = self._place_live_order(
                pair=pair,
                direction='buy',
                volume=qty,
                leverage=self.short_leverage,
                reduce_only=True,
            )
            if result:
                self.short_qty[pair] = 0.0
                self.short_entry_prices[pair] = 0.0
                self.entry_timestamps[pair] = None
                self._remove_position_meta(pair)
                self._update_trade_metrics(pair, pnl_eur)
                self.logger.info("SHORT CLOSE SUCCESS: %s | PNL: %.2f EUR (%.2f%%)",
                                 result, pnl_eur, pnl_pct)
                self._finalise_trade('SHORT_CLOSE', pair, qty, price, pnl_eur,
                                     'SHORT_CLOSE_EXECUTED',
                                     extra={'entry': entry, 'exit_price': price, 'pnl_pct': pnl_pct})
            else:
                self.logger.error(f"SHORT CLOSE FAILED for {pair}")
        except Exception as e:
            self.logger.error(f"Error closing short order: {e}", exc_info=True)


class Backtester:
    def __init__(self, api_client, config):
        self.api_client = api_client
        self.config = config
        self.logger = logging.getLogger(__name__)

    def _simulate_fill_price_from_orderbook(self, pair, side, volume, fallback_price=None, depth_count=50):
        """
        Simulate a fill price by consuming the orderbook depth until `volume` base units
        are filled. `side` is 'buy' (consumes asks) or 'sell' (consumes bids).
        Returns the volume-weighted average fill price, or `fallback_price` if
        orderbook unavailable or depth insufficient.
        """
        try:
            ob = self.api_client.get_order_book(pair, count=depth_count)
            if not ob:
                return fallback_price
            data_key = next((k for k in ob if k != 'last'), None)
            if not data_key:
                return fallback_price
            asks = ob[data_key].get('asks', [])
            bids = ob[data_key].get('bids', [])
            # Buying consumes asks (you pay the ask prices), selling consumes bids
            stack = asks if side == 'buy' else bids
            if not stack:
                return fallback_price
            remaining = float(volume)
            vwp_numer = 0.0
            vwp_denom = 0.0
            for level in stack:
                lvl_price = float(level[0])
                lvl_vol = float(level[1])
                take = min(remaining, lvl_vol)
                vwp_numer += take * lvl_price
                vwp_denom += take
                remaining -= take
                if remaining <= 1e-12:
                    break
            if vwp_denom <= 0:
                return fallback_price
            fill_price = vwp_numer / vwp_denom
            # If not fully filled, conservatively adjust towards worst available price
            if remaining > 1e-12:
                worst_price = float(stack[-1][0])
                # push fill price 50% of remaining shortage towards worst price
                fill_price = (fill_price * (1 - 0.5 * (remaining / (remaining + vwp_denom))) + worst_price * (0.5 * (remaining / (remaining + vwp_denom))))
            return fill_price
        except Exception:
            return fallback_price

    def run(self):
        import numpy as np
        from datetime import datetime

        print("Backtesting mode activated.")

        # Parameters
        pairs = self.config['bot_settings'].get('trade_pairs', ['XBTEUR'])
        # backtesting start date: default to 2024-01-01 but allow override via config.backtesting.start_date = 'YYYY-MM-DD'
        bcfg = self.config.get('backtesting', {}) if isinstance(self.config, dict) else {}
        sd = bcfg.get('start_date')
        if sd:
            try:
                # support ISO date strings
                start_date = datetime.fromisoformat(str(sd))
            except Exception:
                try:
                    start_date = datetime.strptime(str(sd), '%Y-%m-%d')
                except Exception:
                    start_date = datetime(2024, 1, 1)
        else:
            start_date = datetime(2024, 1, 1)
        interval = int(bcfg.get('interval', 60))
        initial_balance = float(bcfg.get('initial_balance', 1000.0))

        # Fees / guard configuration
        rm = self.config.get('risk_management', {})
        # Support fees expressed either as percentage (e.g. 0.26 for 0.26%)
        # or as decimal fraction (e.g. 0.0026). Normalize to fraction (0.0026).
        # normalize fee/slippage values using shared utility
        fees_maker_frac = pct_to_frac(rm.get('fees_maker_percent', 0.16))
        fees_taker_frac = pct_to_frac(rm.get('fees_taker_percent', 0.26))
        exit_slippage_frac = pct_to_frac(rm.get('exit_slippage_buffer_pct', 0.35))
        min_net_sell = float(rm.get('min_net_sell_profit_pct', 0.0))
        min_reentry = float(rm.get('min_reentry_profit_pct', 0.0))
        reentry_pairs = [p.upper() for p in rm.get('reentry_guard_pairs', ['VER'])]

        # Fetch OHLC data
        ohlc_data = {}
        for pair in pairs:
            data = self.api_client.get_ohlc_data(pair, interval, int(start_date.timestamp()))
            if not data:
                self.logger.warning(f"No OHLC data for {pair}")
                continue
            # Kraken may return a dict with pair key or a list directly
            if isinstance(data, dict) and pair in data:
                series = data.get(pair, [])
            elif isinstance(data, list):
                series = data
            else:
                series = list(data.values())[0] if isinstance(data, dict) and data else []

            if not series:
                self.logger.warning(f"No usable OHLC series for {pair}")
                continue
            ohlc_data[pair] = series

        if not ohlc_data:
            print("No data available for backtesting.")
            return

        # Simulation state
        balance = initial_balance
        positions = {pair: 0.0 for pair in pairs}
        entry_prices = {pair: 0.0 for pair in pairs}
        entry_costs = {pair: 0.0 for pair in pairs}  # includes buy fee
        last_closed_net = {pair: None for pair in pairs}
        pnls = []
        balances = [initial_balance]
        peak_balance = initial_balance

        analysis = TechnicalAnalysis()
        # Pick the first available series that actually has data
        primary = None
        for p in pairs:
            if p in ohlc_data and ohlc_data[p]:
                primary = p
                break
        if primary is None:
            # fallback to any available key
            primary = next(iter(ohlc_data.keys()))
            self.logger.warning(f"Primary pair {pairs[0]} had no data; using {primary} instead")
        series_len = len(ohlc_data[primary])

        for i in range(series_len):
            # Use close price
            price = float(ohlc_data[primary][i][4])
            market_data = {primary: {'c': [price]}}
            signal, score = analysis.generate_signal_with_score(market_data)

            if signal == 'BUY' and positions[primary] == 0:
                # Re-entry guard (if configured for this pair)
                try:
                    if any(g in primary.upper() for g in reentry_pairs) and last_closed_net.get(primary) is not None:
                        if last_closed_net[primary] < min_reentry:
                            continue
                except Exception:
                    pass

                # Determine conservative position size (10% of current equity)
                volume = (balance * 0.10) / price if price > 0 else 0.0
                if volume <= 0:
                    continue
                # Simulate realistic fill price using orderbook depth (buy consumes asks)
                fill_price = self._simulate_fill_price_from_orderbook(primary, 'buy', volume, fallback_price=price)
                # Latency model: small price move during execution delay
                try:
                    latency_sec = float(bcfg.get('latency_seconds', 5.0))
                    closes = [float(r[4]) for r in ohlc_data[primary][:i+1]]
                    if len(closes) >= 3:
                        rets = np.diff(closes) / np.array(closes[:-1])
                        per_sec_vol = np.std(rets) / max(1.0, np.sqrt(float(interval)))
                        latency_sigma = per_sec_vol * np.sqrt(latency_sec)
                    else:
                        latency_sigma = 0.0
                    if latency_sigma > 0 and fill_price is not None:
                        fill_price = float(fill_price) * (1.0 + float(np.random.normal(0.0, latency_sigma)))
                except Exception:
                    pass

                cost = volume * (fill_price if fill_price is not None else price)
                buy_fee = cost * fees_maker_frac
                positions[primary] = volume
                entry_prices[primary] = (fill_price if fill_price is not None else price)
                entry_costs[primary] = cost + buy_fee
                balance -= (cost + buy_fee)

            elif signal == 'SELL' and positions[primary] > 0:
                # Simulate exit fill price using orderbook depth (sell consumes bids)
                fill_price = self._simulate_fill_price_from_orderbook(primary, 'sell', positions[primary], fallback_price=price)
                # Latency model for exit
                try:
                    latency_sec = float(bcfg.get('latency_seconds', 5.0))
                    closes = [float(r[4]) for r in ohlc_data[primary][:i+1]]
                    if len(closes) >= 3:
                        rets = np.diff(closes) / np.array(closes[:-1])
                        per_sec_vol = np.std(rets) / max(1.0, np.sqrt(float(interval)))
                        latency_sigma = per_sec_vol * np.sqrt(latency_sec)
                    else:
                        latency_sigma = 0.0
                    if latency_sigma > 0 and fill_price is not None:
                        fill_price = float(fill_price) * (1.0 + float(np.random.normal(0.0, latency_sigma)))
                except Exception:
                    pass

                # Apply conservative exit slippage buffer on top of simulated fill if configured
                if fill_price is None:
                    sell_price_effective = price * (1.0 - exit_slippage_frac)
                else:
                    sell_price_effective = fill_price * (1.0 - exit_slippage_frac)

                gross_pct = ((sell_price_effective - entry_prices[primary]) / entry_prices[primary]) * 100.0 if entry_prices[primary] > 0 else 0.0
                fees_total_pct = (fees_maker_frac + fees_taker_frac) * 100.0
                net_pct = gross_pct - fees_total_pct

                # Respect configured minimum net sell profit when set
                if min_net_sell > 0 and net_pct < min_net_sell:
                    # skip this sell; let position run
                    pass
                else:
                    proceeds_gross = positions[primary] * sell_price_effective
                    sell_fee = proceeds_gross * fees_taker_frac
                    proceeds_net = proceeds_gross - sell_fee
                    balance += proceeds_net
                    pnl = proceeds_net - entry_costs[primary]
                    pnls.append(pnl)
                    last_closed_net[primary] = net_pct
                    positions[primary] = 0.0
                    entry_prices[primary] = 0.0
                    entry_costs[primary] = 0.0

            # update portfolio value
            current_balance = balance + sum(positions[p] * price for p in positions)
            balances.append(current_balance)
            peak_balance = max(peak_balance, current_balance)

        # Calculate performance metrics
        try:
            returns = np.diff(balances) / balances[:-1]
            total_return = (balances[-1] - initial_balance) / initial_balance
            sharpe = np.mean(returns) / np.std(returns) if np.std(returns) > 0 else 0
            downside_returns = returns[returns < 0]
            sortino = np.mean(returns) / np.std(downside_returns) if len(downside_returns) > 0 else 0
        except Exception:
            total_return = (balances[-1] - initial_balance) / initial_balance
            sharpe = 0
            sortino = 0

        print(f"Total Return: {total_return:.2%}")
        print(f"Sharpe Ratio: {sharpe:.2f}")
        print(f"Sortino Ratio: {sortino:.2f}")
        try:
            max_drawdown = max((max(balances[:i+1]) - balances[i]) / max(balances[:i+1]) for i in range(1, len(balances)))
        except Exception:
            max_drawdown = 0.0
        print(f"Max Drawdown: {max_drawdown:.2%}")
        print(f"Total Trades: {len(pnls)}")
        print(f"Win Rate: {sum(1 for p in pnls if p > 0) / len(pnls):.2%}" if pnls else "Win Rate: N/A")

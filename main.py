# Kraken Automated Trading Bot
# Main Script

import os
import sys
import logging
import atexit
import argparse
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv

# Load .env BEFORE importing any module that reads env vars at module level
load_dotenv()

from kraken_interface import KrakenAPI
from trading_bot import TradingBot, Backtester
from utils import load_config, validate_config

try:
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None

# Parse CLI args up front, before config loading, so --paper can affect
# which config file gets loaded (config.paper.toml vs the live config.toml).
_parser = argparse.ArgumentParser(description="Kraken Automated Trading Bot")
_parser.add_argument("--backtest", action="store_true", help="Run backtesting mode.")
_parser.add_argument("--test", action="store_true", help="Run test mode (check API connection).")
_parser.add_argument("--paper", action="store_true", help="Run in paper/dry-run mode (no live orders).")
_parser.add_argument(
    "--config",
    default=None,
    help="Path to a specific config TOML file. If not given, defaults to "
         "config.paper.toml when --paper is set, otherwise config.toml.",
)
_parser.add_argument(
    "--duration",
    type=int,
    default=None,
    help="Stop the bot automatically after this many seconds. Mainly useful "
         "for fixed-length paper-mode test sessions. If omitted, runs until "
         "the target balance is reached or Ctrl+C.",
)
args = _parser.parse_args()

# ── Safety gate: paper mode is the default ────────────────────────────────────
# To go live you must BOTH: remove --paper flag AND set LIVE_TRADING_ENABLED=true
if not args.paper and os.environ.get("LIVE_TRADING_ENABLED", "").lower() != "true":
    print("=" * 60)
    print("SAFETY: Defaulting to PAPER MODE.")
    print("Set LIVE_TRADING_ENABLED=true to enable live trading.")
    print("=" * 60)
    args.paper = True

if args.config:
    CONFIG_PATH = args.config
elif args.paper:
    CONFIG_PATH = "config.paper.toml"
else:
    CONFIG_PATH = "config.toml"

INSTANCE = os.environ.get("KRAKEN_INSTANCE", "paper" if args.paper else "live")
LOCK_FILE = f"/tmp/kraken_bot_{INSTANCE}.lock"
_lock_fp = None


def acquire_single_instance_lock():
    global _lock_fp
    if fcntl is None:
        return
    _lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fp.write(str(os.getpid()))
        _lock_fp.flush()
    except BlockingIOError:
        print("Another kraken_bot instance is already running. Exiting.")
        sys.exit(1)


def release_lock():
    global _lock_fp
    try:
        if _lock_fp and fcntl is not None:
            fcntl.flock(_lock_fp.fileno(), fcntl.LOCK_UN)
            _lock_fp.close()
    except Exception:
        pass


atexit.register(release_lock)
acquire_single_instance_lock()

try:
    config = load_config(CONFIG_PATH)
except FileNotFoundError:
    print(f"Error: Configuration file '{CONFIG_PATH}' not found.")
    sys.exit(1)
except Exception as e:
    print(f"Error loading configuration: {e}")
    sys.exit(1)

if not validate_config(config):
    print("Warning: Configuration validation failed. Some settings may be missing.")

log_dir = Path(config['logging'].get('log_file_path', 'logs/bot_activity.log')).parent
log_dir.mkdir(parents=True, exist_ok=True)

log_file = config['logging']['log_file_path'] if config['logging'].get('log_to_file', True) else None

root_logger = logging.getLogger()
root_logger.setLevel(config['logging'].get('log_level', 'INFO'))
_fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

if log_file:
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        _fh = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,  # 5 MB per file
            backupCount=5,
            encoding='utf-8',
        )
        _fh.setFormatter(_fmt)
        root_logger.addHandler(_fh)
    except Exception as e:
        print(f"Warning: Could not configure log file {log_file}: {e}")

_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
root_logger.addHandler(_sh)

logger = logging.getLogger(__name__)
api_key = os.getenv('KRAKEN_API_KEY', '')
api_secret = os.getenv('KRAKEN_API_SECRET', '')

if not api_key or not api_secret:
    logger.warning("API credentials not configured. Set KRAKEN_API_KEY and KRAKEN_API_SECRET.")
    print("WARNING: Kraken API credentials are not configured.")

# Defer KrakenAPI and TradingBot instantiation until here, now that args
# (parsed above, before config loading) and config are both available.
kraken = None
trading_bot = None

if __name__ == "__main__":
    # Start web dashboard on PORT env var (defaults to 8080)
    try:
        from dashboard import start_dashboard
        _dash_port = int(os.environ.get("PORT", 8080))
        start_dashboard(port=_dash_port)
    except Exception as _de:
        logger.warning("Dashboard failed to start: %s", _de)

    # Instantiate Kraken client with optional paper/dry-run mode.
    # In paper mode, seed the simulated balance from the paper config's
    # initial_balance so get_account_balance() reports something sane
    # instead of hitting Kraken with credentials that may be blank/unused.
    _paper_initial_balance = float(
        config.get('bot_settings', {}).get('trade_amounts', {}).get('initial_balance', 100.0)
    )
    kraken = KrakenAPI(
        api_key=api_key,
        api_secret=api_secret,
        paper_mode=args.paper,
        paper_initial_balance_eur=_paper_initial_balance,
    )
    # config_path=CONFIG_PATH ensures reload_config() (called automatically
    # every config_reload_interval seconds from the main loop) keeps
    # re-reading THIS config file. Without this, a --paper run would
    # silently drift to live config.toml settings after the first reload.
    trading_bot = TradingBot(kraken, config, config_path=CONFIG_PATH)

    # Start scalping engine (paper mode only for now, runs concurrently)
    _scalper = None
    if args.paper:
        try:
            from core.scalper import ScalperEngine
            _ws_feed = getattr(trading_bot, 'ws_feed', None)
            _data_dir = os.path.join(os.path.dirname(__file__), 'data')
            _scalper = ScalperEngine(
                kraken_api=kraken,
                paper_mode=True,
                data_dir=_data_dir,
                ws_feed=_ws_feed,
            )
            _scalper.start()
            trading_bot._scalper = _scalper  # expose for status writes
        except Exception as _se:
            logger.warning("Scalper failed to start: %s", _se)

    if args.test:
        logger.info("Running test mode...")
        print("Testing Kraken API connection...")
        balance = kraken.get_account_balance()
        if balance is not None: 
            print("[OK] Successfully connected to Kraken API")
            print(f"Account balance: {balance}")
        else:
            print("[ERROR] Failed to connect to Kraken API")
        sys.exit(0)
    elif args.backtest:
        logger.info("Starting backtesting...")
        backtester = Backtester(kraken, config)
        backtester.run()
    else:
        logger.info("Starting live trading...") 
        print("Starting Kraken Trading Bot...")
        trading_bot.start_trading(max_runtime_seconds=args.duration)

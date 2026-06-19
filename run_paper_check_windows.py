#!/usr/bin/env python3
"""
Windows-native quick paper-mode smoke check.

Instantiates the bot against config.paper.toml (paper_mode=True, blank
credentials) and prints a couple of effective settings, WITHOUT starting
the trading loop. Use this before run_paper_session_windows.py to sanity
check that the paper config is being read correctly.

Usage:
    python run_paper_check_windows.py
"""
import sys
import toml
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from kraken_interface import KrakenAPI
from trading_bot import TradingBot

CONFIG_PAPER_PATH = str(BASE / 'config.paper.toml')

if not Path(CONFIG_PAPER_PATH).exists():
    print(f"ERROR: paper config not found at {CONFIG_PAPER_PATH}")
    sys.exit(1)

cfg = toml.load(CONFIG_PAPER_PATH)
initial_balance = float(cfg.get('bot_settings', {}).get('trade_amounts', {}).get('initial_balance', 100.0))

kraken = KrakenAPI(api_key='', api_secret='', paper_mode=True, paper_initial_balance_eur=initial_balance)
bot = TradingBot(kraken, cfg, config_path=CONFIG_PAPER_PATH)

print(f"paper_mode (on api_client)     = {kraken.paper_mode}")
print(f"paper_initial_balance_eur      = {kraken._paper_balance_eur}")
print(f"bot.config_path                = {bot.config_path}")
print(f"trade_pairs                    = {bot.trade_pairs}")
print(f"allocation_per_trade_percent   = {cfg.get('bot_settings', {}).get('allocation_per_trade_percent', 'MISSING')}")
print(f"max_short_notional_eur         = {cfg.get('shorting', {}).get('max_short_notional_eur', 'MISSING')}")
print(f"data_purchase_prices_path      = {bot.data_purchase_prices_path}")
print()
print("If bot.config_path above does NOT end in config.paper.toml, the")
print("hot-reload-overwrites-paper-config bug is not fixed -- stop and check trading_bot.py.")

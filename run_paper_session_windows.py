#!/usr/bin/env python3
"""
Windows-native paper-mode session runner.

Launches a paper-mode TradingBot in a child process for a fixed duration,
then terminates it and summarizes trade events.

Differences from the original run_paper_session.py:
  - No hardcoded /home/felix/tradingbot path. Uses this script's own
    directory (BASE), so it works wherever you've checked the bot out
    (e.g. D:\\tradingbot).
  - Resolves the venv's python.exe correctly on Windows (Scripts\\python.exe)
    instead of assuming a Linux-style venv/bin/python layout.
  - Passes config_path=<...>/config.paper.toml into TradingBot() so the
    paper config survives hot-reloads (see trading_bot.py fix). Without
    this, the bot silently reloads the LIVE config.toml after
    config_reload_interval seconds (60s by default) and your paper test
    stops testing what you think it's testing.
  - Blank API key/secret + paper_mode=True, same as the original: no real
    AddOrder/CancelOrder calls are made (see kraken_interface.py).

Usage:
    python run_paper_session_windows.py --duration 300
"""
import argparse
import subprocess
import sys
import time
import os
import json
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--duration', type=int, default=300, help='Seconds to run the paper session for')
args = p.parse_args()
DURATION = args.duration

BASE = Path(__file__).resolve().parent
LOGS = BASE / 'logs'
LOGS.mkdir(exist_ok=True)
CHILD_LOG = LOGS / 'paper_session_child.log'
SUMMARY_LOG = LOGS / 'paper_session_summary.txt'

# Resolve the venv's python.exe. On Windows it's Scripts\python.exe, on
# Linux/macOS it's bin/python. Fall back to sys.executable (the
# interpreter currently running this script) if VIRTUAL_ENV isn't set --
# that's the most reliable choice when you're already inside an activated
# venv, which the (venv) PS prompt confirms you are.
venv = os.getenv('VIRTUAL_ENV', '')
if venv:
    candidate = Path(venv) / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    python_exe = str(candidate) if candidate.exists() else sys.executable
else:
    python_exe = sys.executable

CONFIG_PAPER_PATH = str(BASE / 'config.paper.toml')
BOT_DIR = str(BASE)

# Child code: import TradingBot, instantiate with paper config, start trading loop.
# BOT_DIR / CONFIG_PAPER_PATH are baked in as literals below (not read from
# the child's own env) so there's no ambiguity about which checkout/config
# the child process uses.
child_code = f"""
import sys, toml, os
sys.path.insert(0, {BOT_DIR!r})
from kraken_interface import KrakenAPI
from trading_bot import TradingBot

config_path = {CONFIG_PAPER_PATH!r}
cfg = toml.load(config_path)

initial_balance = float(cfg.get('bot_settings', {{}}).get('trade_amounts', {{}}).get('initial_balance', 100.0))

# Blank credentials + paper_mode=True: place_order() simulates fills locally
# and never reaches the real Kraken AddOrder endpoint. cancel_order() also
# checks paper_mode directly now (defense in depth, doesn't rely solely on
# blank creds failing auth). get_account_balance()/get_open_orders()/
# get_ledgers()/get_trade_history() are also short-circuited in paper_mode
# so the bot doesn't waste the whole session retrying private calls that
# are guaranteed to fail with blank credentials.
kraken = KrakenAPI(api_key='', api_secret='', paper_mode=True, paper_initial_balance_eur=initial_balance)

# config_path is passed through so reload_config() keeps re-reading THIS
# paper config every cycle, instead of falling back to the live config.toml.
bot = TradingBot(kraken, cfg, config_path=config_path)
print('PAPER_CHILD_STARTED')
bot.start_trading()
"""

print(f"Using python: {python_exe}")
print(f"Bot dir: {BOT_DIR}")
print(f"Paper config: {CONFIG_PAPER_PATH}")
if not Path(CONFIG_PAPER_PATH).exists():
    print(f"ERROR: paper config not found at {CONFIG_PAPER_PATH}")
    sys.exit(1)

with open(CHILD_LOG, 'ab') as outfh:
    proc = subprocess.Popen(
        [python_exe, '-u', '-c', child_code],
        stdout=outfh, stderr=outfh,
        cwd=BOT_DIR,
    )
    try:
        start_ts = time.time()
        while True:
            if proc.poll() is not None:
                break
            if time.time() - start_ts >= DURATION:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
                break
            time.sleep(1)
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass

# Summarize trade events if any
events = []
jsonl = LOGS / 'trade_events.jsonl'
if jsonl.exists():
    try:
        for ln in jsonl.read_text(encoding='utf-8').splitlines():
            try:
                events.append(json.loads(ln))
            except Exception:
                continue
    except Exception:
        pass

with open(SUMMARY_LOG, 'w') as fh:
    fh.write(f'Ran paper session for {DURATION} seconds.\n')
    fh.write(f'Bot dir: {BOT_DIR}\n')
    fh.write(f'Paper config: {CONFIG_PAPER_PATH}\n')
    fh.write(f'Child log: {CHILD_LOG}\n')
    fh.write(f'Trade events parsed: {len(events)}\n')
    if events:
        profits = [e.get('profit_eur') for e in events if e.get('profit_eur') is not None]
        slippages = [e.get('slippage_pct') for e in events if e.get('slippage_pct') is not None]
        fh.write(f'Trades: {len(events)}\n')
        fh.write(f'Avg profit EUR: {sum(profits)/len(profits) if profits else 0.0}\n')
        fh.write(f'Avg slippage %: {sum(slippages)/len(slippages) if slippages else 0.0}\n')

print('PAPER_SESSION_DONE')
print('Child log:', CHILD_LOG)
print('Summary log:', SUMMARY_LOG)

#!/usr/bin/env python3
"""
Windows-native paper-mode session runner -- LIVE CONSOLE VERSION.

Same as run_paper_session_windows.py, but instead of redirecting the child
process's output to a log file silently in the background, this version
streams it straight to your terminal in real time so you can watch the
bot's loop iterations (and any [PAPER] simulated fills) as they happen.

Output is also tee'd to logs/paper_session_child.log so you keep a
permanent record even if you weren't watching every line scroll by.

Usage:
    python run_paper_session_live.py --duration 3600
    (Ctrl+C also stops it early and still writes the summary.)
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

venv = os.getenv('VIRTUAL_ENV', '')
if venv:
    candidate = Path(venv) / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    python_exe = str(candidate) if candidate.exists() else sys.executable
else:
    python_exe = sys.executable

CONFIG_PAPER_PATH = str(BASE / 'config.paper.toml')
BOT_DIR = str(BASE)

if not Path(CONFIG_PAPER_PATH).exists():
    print(f"ERROR: paper config not found at {CONFIG_PAPER_PATH}")
    sys.exit(1)

import toml
cfg = toml.load(CONFIG_PAPER_PATH)
initial_balance = float(cfg.get('bot_settings', {}).get('trade_amounts', {}).get('initial_balance', 100.0))

child_code = f"""
import sys, toml, os
sys.path.insert(0, {BOT_DIR!r})
from kraken_interface import KrakenAPI
from trading_bot import TradingBot

config_path = {CONFIG_PAPER_PATH!r}
cfg = toml.load(config_path)
initial_balance = {initial_balance!r}

kraken = KrakenAPI(api_key='', api_secret='', paper_mode=True, paper_initial_balance_eur=initial_balance)
bot = TradingBot(kraken, cfg, config_path=config_path)
print('PAPER_CHILD_STARTED', flush=True)
bot.start_trading()
"""

print(f"Using python: {python_exe}")
print(f"Bot dir: {BOT_DIR}")
print(f"Paper config: {CONFIG_PAPER_PATH}")
print(f"Running for {DURATION} seconds ({DURATION/60:.1f} min). Ctrl+C to stop early.")
print(f"Live output below, also being saved to: {CHILD_LOG}")
print("=" * 70)

start_ts = time.time()
log_fh = open(CHILD_LOG, 'a', encoding='utf-8', errors='replace')

proc = subprocess.Popen(
    [python_exe, '-u', '-c', child_code],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    cwd=BOT_DIR, text=True, bufsize=1,
)

try:
    while True:
        line = proc.stdout.readline()
        if line:
            sys.stdout.write(line)
            sys.stdout.flush()
            log_fh.write(line)
            log_fh.flush()
        if proc.poll() is not None and not line:
            break
        if time.time() - start_ts >= DURATION:
            print("=" * 70)
            print(f"Duration ({DURATION}s) reached, stopping bot...")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            break
except KeyboardInterrupt:
    print("\n" + "=" * 70)
    print("Ctrl+C received, stopping bot early...")
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
finally:
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    log_fh.close()

elapsed = time.time() - start_ts

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
    fh.write(f'Ran paper session for {elapsed:.0f} seconds (requested {DURATION}s).\n')
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

print("=" * 70)
print('PAPER_SESSION_DONE')
print(f'Ran for {elapsed:.0f}s, {len(events)} trade event(s) recorded.')
print('Child log:', CHILD_LOG)
print('Summary log:', SUMMARY_LOG)

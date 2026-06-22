"""
Railway Health Monitor
=======================
Runs locally and monitors the Railway deployment every 5 minutes.
Sends a Telegram alert if Railway is unreachable for 2+ consecutive checks.

To use:
  1. Set RAILWAY_URL in .env (your Railway public dashboard URL)
  2. Run: python monitor_railway.py
  3. Leave it running in a terminal or as a Windows scheduled task

If Railway goes down:
  - You get a Telegram alert
  - Start the local Docker bot: docker-compose up -d
  - When Railway recovers, stop local: docker-compose down
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

RAILWAY_URL     = os.getenv("RAILWAY_URL", "").rstrip("/")
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL  = 300   # 5 minutes
ALERT_AFTER     = 2     # alert after 2 consecutive failures


def send_telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print(f"[Telegram] {msg}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg},
            timeout=8,
        )
    except Exception:
        pass


def check_railway() -> bool:
    if not RAILWAY_URL:
        print("RAILWAY_URL not set in .env — cannot monitor")
        return True
    try:
        r = requests.get(f"{RAILWAY_URL}/health", timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def main():
    print(f"Monitoring Railway at {RAILWAY_URL}")
    print(f"Checking every {CHECK_INTERVAL // 60} minutes")
    print("Ctrl+C to stop\n")

    failures = 0
    was_down = False

    while True:
        ok = check_railway()
        ts = time.strftime("%H:%M:%S")

        if ok:
            if was_down:
                msg = f"[RAILWAY RECOVERED] Bot is back online at {ts}"
                print(msg)
                send_telegram(f"RAILWAY RECOVERED - bot back online")
                was_down = False
            else:
                print(f"[{ts}] Railway OK")
            failures = 0
        else:
            failures += 1
            print(f"[{ts}] Railway DOWN (failure #{failures})")

            if failures >= ALERT_AFTER and not was_down:
                was_down = True
                msg = (
                    f"RAILWAY DOWN - {failures} consecutive failures\n"
                    f"Start local backup: docker-compose up -d\n"
                    f"Dashboard: http://localhost:8080"
                )
                print(f"\n*** ALERT: {msg} ***\n")
                send_telegram(msg)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()

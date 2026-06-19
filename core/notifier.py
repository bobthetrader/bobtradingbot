"""
Telegram Notifier
==================
Sends trade alerts to a Telegram chat via the Bot API.

Required environment variables:
  TELEGRAM_BOT_TOKEN  — from @BotFather
  TELEGRAM_CHAT_ID    — your personal chat ID

If either variable is missing the notifier silently skips so the
bot keeps running without notifications.
"""

import logging
import os
import requests

logger = logging.getLogger(__name__)

_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send(message: str) -> bool:
    """Send a plain-text message to the configured Telegram chat.

    Returns True on success, False on any failure or misconfiguration.
    Never raises — the bot must keep running even if Telegram is down.
    """
    token   = os.getenv("TELEGRAM_BOT_TOKEN", _TOKEN)
    chat_id = os.getenv("TELEGRAM_CHAT_ID", _CHAT_ID)

    if not token or not chat_id:
        logger.debug("Telegram not configured — skipping notification")
        return False

    try:
        url  = _API_URL.format(token=token)
        resp = requests.post(
            url,
            json={
                "chat_id":    chat_id,
                "text":       message,
                "parse_mode": "HTML",
            },
            timeout=8,
        )
        if resp.status_code == 200:
            return True
        logger.debug("Telegram API returned %s: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        logger.debug("Telegram send failed: %s", exc)
        return False

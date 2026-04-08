"""
Shared Telegram notifier for all bots.

Environment variables:
    TELEGRAM_BOT_TOKEN — from @BotFather
    TELEGRAM_CHAT_ID   — your chat/group id

Usage:
    from telegram_notify import notify
    notify("bot started", tag="arb1")
"""

import os
import time
import urllib.parse
import urllib.request

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

_LAST_ERROR_TS = 0.0


def notify(message: str, tag: str = "", silent: bool = False) -> bool:
    """Send a message to Telegram. Non-fatal on failure.

    Returns True on success, False otherwise. Rate-limits repeated error
    logs so a broken Telegram config doesn't spam the bot's stdout.
    """
    global _LAST_ERROR_TS

    if not BOT_TOKEN or not CHAT_ID:
        return False

    prefix = f"[{tag}] " if tag else ""
    text = f"{prefix}{message}"[:4000]

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "disable_notification": "true" if silent else "false",
    }).encode()

    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        now = time.time()
        if now - _LAST_ERROR_TS > 300:
            print(f"[telegram] failed: {e}")
            _LAST_ERROR_TS = now
        return False


def notify_critical(message: str, tag: str = "") -> bool:
    """Loud notification (not silent) for critical events."""
    return notify(message, tag=tag, silent=False)


if __name__ == "__main__":
    ok = notify("telegram_notify test ok", tag="test")
    print("sent" if ok else "failed (check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")

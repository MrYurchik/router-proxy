"""
router_proxy/notify.py — Telegram notifications for Go down/recovered.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from env.
Falls back to stdout if env vars are missing.
"""

import os
import logging
import httpx

logger = logging.getLogger("router_proxy.notify")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


async def _telegram_send(message: str) -> bool:
    """Send a Telegram message. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info(f"[notify/telegram skipped — no token/chat_id] {message}")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
            })
            if resp.status_code == 200:
                logger.info(f"[notify/telegram sent] {message[:80]}")
                return True
            else:
                logger.warning(f"[notify/telegram failed {resp.status_code}] {resp.text[:200]}")
                return False
    except Exception as e:
        logger.warning(f"[notify/telegram exception] {e}")
        return False


async def notify(message: str) -> bool:
    """Send notification. Returns True if successfully delivered."""
    success = await _telegram_send(message)
    if not success:
        # Always log to stdout as fallback
        logger.info(f"[notify/stdout] {message}")
    return success


async def notify_go_down(status: int, role: str):
    msg = (
        f"⚠️ <b>OpenCode Go down</b> (HTTP {status}) on role '<b>{role}</b>'.\n"
        f"Hermes proxy fell back to $2-cap OpenRouter.\n"
        f"Check Go billing/subscription."
    )
    await notify(msg)


async def notify_go_recovered():
    msg = "✅ <b>OpenCode Go back up.</b> Paid roles switched back to Go."
    await notify(msg)

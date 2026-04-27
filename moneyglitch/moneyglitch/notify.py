"""Fire-and-forget Telegram notifications from the parser via the bot token.

Uses the Bot API directly so the parser doesn't depend on the aiogram process.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


async def notify(bot_token: str, user_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": user_id, "text": text, "parse_mode": "HTML"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            await c.post(url, json=payload)
    except Exception as e:  # noqa: BLE001 — notify must never raise
        log.warning("notify failed: %s", e)

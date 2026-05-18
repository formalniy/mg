"""Direct Telegram Bot API helpers used outside the aiogram process.

The parser opens trades and sends the initial position card itself (so the
user sees confirmation with no inter-process delay) — these helpers let it
send and capture message IDs without pulling in aiogram.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


async def _post(token: str, method: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = API.format(token=token, method=method)
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(url, json=payload)
            data = r.json()
            if not data.get("ok"):
                log.warning("telegram %s error: %s", method, data)
                return None
            return data.get("result")
    except Exception as e:  # noqa: BLE001 — must never raise
        log.warning("telegram %s failed: %s", method, e)
        return None


async def notify(bot_token: str, user_id: int, text: str) -> None:
    await _post(bot_token, "sendMessage", {
        "chat_id": user_id,
        "text": text,
        "parse_mode": "HTML",
    })


async def send_message(
    bot_token: str,
    chat_id: int,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Send a message; return the resulting message_id (or None on failure)."""
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    res = await _post(bot_token, "sendMessage", payload)
    if not res:
        return None
    mid = res.get("message_id")
    return int(mid) if mid is not None else None


async def edit_message(
    bot_token: str,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> bool:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    res = await _post(bot_token, "editMessageText", payload)
    return res is not None


def close_button_markup(callback_data: str) -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🔴 Закрыть позицию", "callback_data": callback_data}]
        ]
    }


async def broadcast_card(
    bot_token: str,
    user_ids: Iterable[int],
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, int]]:
    """Send `text` to each user_id; return list of {chat_id, message_id}
    entries for the messages that posted successfully."""
    out: List[Dict[str, int]] = []
    for uid in user_ids:
        mid = await send_message(bot_token, uid, text, reply_markup)
        if mid is not None:
            out.append({"chat_id": int(uid), "message_id": int(mid)})
    return out

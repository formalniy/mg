"""Telethon parser for @durov.

Why event-driven, not polling: Telethon receives MTProto updates pushed by
Telegram as soon as a message is published, so latency is bounded only by
the network round-trip — strictly lower than any GetHistory poll loop. The
client also auto-reconnects on transport errors with no exponential backoff
configured by us.

Novelty check is the cheapest possible: a single integer comparison
(`event.message.id <= last_seen_id`). Telegram message IDs are monotonic
within a channel.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any, Dict

from telethon import TelegramClient, events

from .mexc import MexcError, MexcFutures
from .notify import notify
from .state import load_state

log = logging.getLogger(__name__)

CHANNEL = "durov"
TON_RE = re.compile(r"\bTON\b")  # case-sensitive: the ticker is uppercase


def has_ton(text: str) -> bool:
    return bool(text) and TON_RE.search(text) is not None


async def run_parser(config: Dict[str, Any]) -> None:
    tg = config["telegram"]
    api_id = int(tg["api_id"])
    api_hash = str(tg["api_hash"])
    session = str(tg.get("session") or "moneyglitch")

    mexc_cfg = config["mexc"]
    symbol = str(mexc_cfg.get("symbol") or "TON_USDT")
    open_type = int(mexc_cfg.get("open_type") or 1)
    mexc = MexcFutures(str(mexc_cfg["api_key"]), str(mexc_cfg["secret"]))

    bot_cfg = config["bot"]
    bot_token = str(bot_cfg["token"])
    user_id = int(bot_cfg["user_id"])

    client = TelegramClient(session, api_id, api_hash)

    last_seen = {"id": 0}
    trade_lock = asyncio.Lock()

    @client.on(events.NewMessage(chats=[CHANNEL]))
    async def handler(event):
        msg = event.message
        if msg.id <= last_seen["id"]:
            return
        last_seen["id"] = msg.id

        text = msg.message or ""
        if not has_ton(text):
            return

        log.info("TON match in #%d", msg.id)
        st = load_state()
        if not st["enabled"]:
            await notify(
                bot_token, user_id,
                f"⚠️ Найдено <b>TON</b> в посте #{msg.id}, торговля выключена.",
            )
            return

        if trade_lock.locked():
            log.info("trade in progress, skipping #%d", msg.id)
            return

        async with trade_lock:
            try:
                res = await mexc.open_long_market(
                    symbol=symbol,
                    amount_usd=float(st["amount_usd"]),
                    leverage=int(st["leverage"]),
                    stop_loss_pct=float(st["stop_loss_pct"]),
                    open_type=open_type,
                )
                snippet = (text[:120] + "…") if len(text) > 120 else text
                await notify(
                    bot_token, user_id,
                    "✅ <b>Открыт лонг TONUSDT</b>\n"
                    f"Пост #{msg.id}\n"
                    f"Сумма: {st['amount_usd']} USD · Плечо: {st['leverage']}x · SL: {st['stop_loss_pct']}%\n"
                    f"Order: <code>{html.escape(str(res.get('data')))}</code>\n"
                    f"Текст: <i>{html.escape(snippet)}</i>",
                )
            except MexcError as e:
                await notify(bot_token, user_id, f"❌ MEXC: <code>{html.escape(str(e))}</code>")
            except Exception as e:  # noqa: BLE001
                log.exception("trade failed")
                await notify(bot_token, user_id, f"❌ Ошибка: <code>{html.escape(str(e))}</code>")

    await client.start()
    log.info("parser connected; listening @%s", CHANNEL)
    try:
        await client.run_until_disconnected()
    finally:
        await mexc.aclose()

"""Telethon parser.

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
from typing import Any, Dict, Iterable, List, Union

from telethon import TelegramClient, events
from telethon.tl.functions.channels import JoinChannelRequest

from .bybit import BybitError, BybitFutures, format_diagnostics_html
from .notify import notify
from .state import load_state

log = logging.getLogger(__name__)

TON_RE = re.compile(r"\bTON\b")  # case-sensitive: the ticker is uppercase


def has_ton(text: str) -> bool:
    return bool(text) and TON_RE.search(text) is not None


def normalize_user_ids(bot_cfg: Dict[str, Any]) -> List[int]:
    """Accept `user_ids: [...]` (preferred) or legacy `user_id: <int>`."""
    raw: Iterable[Any] = bot_cfg.get("user_ids") or []
    if not raw and bot_cfg.get("user_id") not in (None, 0):
        raw = [bot_cfg["user_id"]]
    return [int(x) for x in raw if int(x) != 0]


def parse_channel(value: Any) -> Union[str, int]:
    """`@durov`/`durov` → "durov"; numeric (incl. -100…) → int."""
    if isinstance(value, int):
        return value
    s = str(value or "").strip().lstrip("@")
    if not s:
        raise ValueError("telegram.channel is empty in config")
    if s.lstrip("-").isdigit():
        return int(s)
    return s


async def _broadcast(bot_token: str, user_ids: Iterable[int], text: str) -> None:
    await asyncio.gather(*(notify(bot_token, uid, text) for uid in user_ids))


async def run_parser(config: Dict[str, Any]) -> None:
    tg = config["telegram"]
    api_id = int(tg["api_id"])
    api_hash = str(tg["api_hash"])
    session = str(tg.get("session") or "moneyglitch")
    channel = parse_channel(tg.get("channel", "durov"))

    ex_cfg = config["bybit"]
    symbol = str(ex_cfg.get("symbol") or "TONUSDT")
    isolated = bool(ex_cfg.get("isolated", True))
    bybit = BybitFutures(str(ex_cfg["api_key"]), str(ex_cfg["secret"]))

    bot_cfg = config["bot"]
    bot_token = str(bot_cfg["token"])
    user_ids = normalize_user_ids(bot_cfg)
    if not user_ids:
        log.warning("no bot.user_ids configured — notifications will be silent")

    client = TelegramClient(session, api_id, api_hash)

    last_seen = {"id": 0}
    trade_lock = asyncio.Lock()

    @client.on(events.NewMessage(chats=[channel]))
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
            await _broadcast(
                bot_token, user_ids,
                f"⚠️ Найдено <b>TON</b> в посте #{msg.id}, торговля выключена.",
            )
            return

        if trade_lock.locked():
            log.info("trade in progress, skipping #%d", msg.id)
            return

        async with trade_lock:
            try:
                res = await bybit.open_long_market(
                    symbol=symbol,
                    amount_usd=float(st["amount_usd"]),
                    leverage=int(st["leverage"]),
                    stop_loss_pct=float(st["stop_loss_pct"]),
                    isolated=isolated,
                )
                snippet = (text[:120] + "…") if len(text) > 120 else text
                await _broadcast(
                    bot_token, user_ids,
                    f"✅ <b>Открыт лонг {html.escape(symbol)}</b>\n"
                    f"Пост #{msg.id}\n"
                    f"Сумма: {st['amount_usd']} USD · Плечо: {st['leverage']}x · SL: {st['stop_loss_pct']}%\n"
                    f"Order: <code>{html.escape(str(res.get('result')))}</code>\n"
                    f"Текст: <i>{html.escape(snippet)}</i>",
                )
            except BybitError as e:
                await _broadcast(bot_token, user_ids, f"❌ Bybit: <code>{html.escape(str(e))}</code>")
                try:
                    diag = await bybit.gather_diagnostics(symbol)
                    await _broadcast(bot_token, user_ids, format_diagnostics_html(diag))
                except Exception as de:  # noqa: BLE001 — diagnostics must never mask the original error
                    log.warning("diagnostics gather failed: %s", de)
            except Exception as e:  # noqa: BLE001
                log.exception("trade failed")
                await _broadcast(bot_token, user_ids, f"❌ Ошибка: <code>{html.escape(str(e))}</code>")

    await client.start()

    # NewMessage events only fire for channels the account has joined.
    # JoinChannelRequest is idempotent — safe to call if already a member.
    try:
        await client(JoinChannelRequest(channel))
    except Exception as e:
        log.warning(
            "could not join channel %s (%s) — if it's private, join manually "
            "with the parser account; NewMessage events will be silent until then",
            channel, e,
        )

    log.info("parser connected; listening @%s", channel)
    try:
        await client.run_until_disconnected()
    finally:
        await bybit.aclose()

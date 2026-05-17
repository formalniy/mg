"""Telethon parser.

Why event-driven, not polling: Telethon receives MTProto updates pushed by
Telegram as soon as a message is published, so latency is bounded only by
the network round-trip — strictly lower than any GetHistory poll loop.

When TON is matched, every enabled account opens its own trade in parallel.
After a successful open, the parser sends the initial position card (with
a "close" inline button) directly via Bot API and records the resulting
message IDs into state.position. The bot process then takes over the live
update loop and the close-button handler.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import Any, Dict, List, Optional, Union

from telethon import TelegramClient, events
from telethon.tl.functions.channels import JoinChannelRequest

from .accounts import AccountConfig, load_accounts
from .ai import AIError, ai_config_ready, ask_ai_decision, load_ai_config
from .bybit import BybitError, BybitFutures, format_diagnostics_html
from .notify import broadcast_card, close_button_markup, notify
from .state import load_account, set_position

log = logging.getLogger(__name__)

TON_RE = re.compile(r"\bTON\b")  # case-sensitive: the ticker is uppercase


def has_ton(text: str) -> bool:
    return bool(text) and TON_RE.search(text) is not None


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


def render_open_card(
    account: AccountConfig,
    *,
    entry_price: str,
    qty: str,
    sl_price: str,
    tp_price: Optional[str],
    leverage: int,
    amount_usd: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    neutral_tp_price: Optional[str] = None,
    neutral_tp_qty: Optional[str] = None,
) -> str:
    tp_line = (
        f"TP:      <b>{html.escape(tp_price)}</b> (+{take_profit_pct}% маржи)\n"
        if tp_price else "TP:      —\n"
    )
    neutral_line = (
        f"🧮 TP-фи: <b>{html.escape(neutral_tp_price)}</b> · "
        f"qty <b>{html.escape(neutral_tp_qty or '?')}</b>\n"
        if neutral_tp_price else ""
    )
    return (
        f"🟢 <b>LONG {html.escape(account.symbol)}</b> · <code>{html.escape(account.name)}</code>\n"
        f"Entry:   <b>{html.escape(entry_price)}</b>\n"
        f"Current: <b>{html.escape(entry_price)}</b>  (—)\n"
        f"SL:      <b>{html.escape(sl_price)}</b> (-{stop_loss_pct}% маржи)\n"
        f"{tp_line}"
        f"{neutral_line}"
        f"Qty:     <b>{html.escape(qty)}</b> · плечо {leverage}x · маржа {amount_usd} USD\n"
        f"PnL:     —\n"
        f"Баланс:  —\n"
        f"Время:   00:00"
    )


async def _run_ai_once(post_text: str) -> Dict[str, Any]:
    """Run the global AI decision once per post.

    The AI is a top-level resource (one config on the VPS, one HTTP call per
    post). Per-user opt-in for the gate lives in `state.ai_enabled`; this
    function does not consult per-account state.

    Returns a status dict consumed by `_trade_for_account`:
        {"status": "ok",        "decision": 0|1}
        {"status": "disabled"}                    — ai_config.json missing/incomplete
        {"status": "error",     "reason": str}    — call failed or unparseable
    """
    cfg = load_ai_config()
    if not ai_config_ready(cfg):
        return {"status": "disabled"}
    try:
        decision = await ask_ai_decision(cfg, post_text)
    except AIError as e:
        return {"status": "error", "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        log.exception("global AI call failed")
        return {"status": "error", "reason": str(e)}
    return {"status": "ok", "decision": decision}


async def _trade_for_account(
    account: AccountConfig,
    bybit: BybitFutures,
    bot_token: str,
    msg_id: int,
    snippet: str,
    ai_result: Dict[str, Any],
) -> None:
    """Run one open-trade flow for a single account.

    `ai_result` is the shared, top-level AI decision for this post (see
    `_run_ai_once`). The account consults it only if its own `ai_enabled`
    flag is set; otherwise the post fires the trade unconditionally (same
    behavior as before AI was added).
    """
    st = load_account(account.name)
    if not st["enabled"]:
        await asyncio.gather(*(
            notify(bot_token, uid,
                   f"⚠️ Найдено <b>TON</b> в посте #{msg_id}, торговля для "
                   f"<code>{html.escape(account.name)}</code> выключена.")
            for uid in account.user_ids
        ))
        return

    if (st.get("position") or {}).get("symbol"):
        # Already in position; skip new open.
        log.info("account %s: position already open, skipping #%d", account.name, msg_id)
        return

    # AI gate: only consulted when this account opted in via ai_enabled.
    # Failure mode is conservative — any AI error skips the trade so an AI
    # outage cannot accidentally fire a leveraged long.
    if st.get("ai_enabled"):
        status = ai_result.get("status")
        if status == "disabled":
            await asyncio.gather(*(
                notify(bot_token, uid,
                       f"🧠 <code>{html.escape(account.name)}</code> · "
                       f"Пост #{msg_id} — пропущен (фильтр ИИ включён у вас, "
                       f"но <code>ai_config.json</code> не настроен на VPS).")
                for uid in account.user_ids
            ))
            return
        if status == "error":
            await asyncio.gather(*(
                notify(bot_token, uid,
                       f"🧠 <code>{html.escape(account.name)}</code> · "
                       f"Пост #{msg_id} — пропущен (нейросеть: "
                       f"<code>{html.escape(str(ai_result.get('reason') or ''))}</code>)")
                for uid in account.user_ids
            ))
            return
        decision = int(ai_result.get("decision") or 0)
        if decision != 1:
            await asyncio.gather(*(
                notify(bot_token, uid,
                       f"🧠 <code>{html.escape(account.name)}</code> · "
                       f"Пост #{msg_id} — нейросеть ответила <b>0</b>, пропускаю.")
                for uid in account.user_ids
            ))
            return
        await asyncio.gather(*(
            notify(bot_token, uid,
                   f"🧠 <code>{html.escape(account.name)}</code> · "
                   f"Пост #{msg_id} — нейросеть ответила <b>1</b>, открываю сделку.")
            for uid in account.user_ids
        ))

    try:
        result = await bybit.open_long_market(
            symbol=account.symbol,
            amount_usd=float(st["amount_usd"]),
            leverage=int(st["leverage"]),
            stop_loss_pct=float(st["stop_loss_pct"]),
            take_profit_pct=float(st.get("take_profit_pct") or 0.0),
            isolated=account.isolated,
            fee_neutralize=bool(st.get("fee_neutralize_enabled") or False),
        )
    except BybitError as e:
        await asyncio.gather(*(
            notify(bot_token, uid,
                   f"❌ <code>{html.escape(account.name)}</code> · "
                   f"Bybit: <code>{html.escape(str(e))}</code>")
            for uid in account.user_ids
        ))
        try:
            diag = await bybit.gather_diagnostics(account.symbol)
            txt = format_diagnostics_html(diag)
            await asyncio.gather(*(notify(bot_token, uid, txt) for uid in account.user_ids))
        except Exception as de:  # noqa: BLE001
            log.warning("diagnostics gather failed for %s: %s", account.name, de)
        return
    except Exception as e:  # noqa: BLE001
        log.exception("trade failed for %s", account.name)
        await asyncio.gather(*(
            notify(bot_token, uid,
                   f"❌ <code>{html.escape(account.name)}</code> · "
                   f"Ошибка: <code>{html.escape(str(e))}</code>")
            for uid in account.user_ids
        ))
        return

    text = render_open_card(
        account,
        entry_price=result["entry_price"],
        qty=result["qty"],
        sl_price=result["sl_price"],
        tp_price=result.get("tp_price"),
        leverage=int(st["leverage"]),
        amount_usd=float(st["amount_usd"]),
        stop_loss_pct=float(st["stop_loss_pct"]),
        take_profit_pct=float(st.get("take_profit_pct") or 0.0),
        neutral_tp_price=result.get("neutral_tp_price"),
        neutral_tp_qty=result.get("neutral_tp_qty"),
    )
    markup = close_button_markup(f"close:{account.name}")
    messages = await broadcast_card(bot_token, account.user_ids, text, markup)

    set_position(account.name, {
        "side": "Long",
        "symbol": account.symbol,
        "qty": result["qty"],
        "entry_price": result["entry_price"],
        "sl_price": result["sl_price"],
        "tp_price": result.get("tp_price"),
        "leverage": int(st["leverage"]),
        "amount_usd": float(st["amount_usd"]),
        "stop_loss_pct": float(st["stop_loss_pct"]),
        "take_profit_pct": float(st.get("take_profit_pct") or 0.0),
        "fee_neutralize_enabled": bool(st.get("fee_neutralize_enabled") or False),
        "neutral_tp_price": result.get("neutral_tp_price"),
        "neutral_tp_qty": result.get("neutral_tp_qty"),
        "neutral_tp_order_id": result.get("neutral_tp_order_id"),
        "main_tp_order_id": result.get("main_tp_order_id"),
        "opened_at_ms": int(time.time() * 1000),
        "messages": messages,
        "closing": False,
        "post_id": msg_id,
        "snippet": snippet,
    })
    log.info("account %s: opened position, %d messages tracked", account.name, len(messages))


async def run_parser(config: Dict[str, Any]) -> None:
    tg = config["telegram"]
    api_id = int(tg["api_id"])
    api_hash = str(tg["api_hash"])
    session = str(tg.get("session") or "moneyglitch")
    channel = parse_channel(tg.get("channel", "durov"))

    bot_cfg = config["bot"]
    bot_token = str(bot_cfg["token"])

    accounts = load_accounts(config)
    if not accounts:
        raise RuntimeError("no accounts configured")
    bybit_clients: Dict[str, BybitFutures] = {
        a.name: BybitFutures(a.api_key, a.secret) for a in accounts
    }
    log.info("loaded %d account(s): %s", len(accounts), [a.name for a in accounts])

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
        snippet = (text[:120] + "…") if len(text) > 120 else text

        if trade_lock.locked():
            log.info("trade fan-out in progress, skipping #%d", msg.id)
            return

        async with trade_lock:
            # AI runs once per post at the top level. Each account decides
            # whether to use the result via its own ai_enabled flag. The
            # call happens regardless of who has it enabled — for any
            # subscribed account we still want the gate available
            # immediately with no per-account fan-out cost.
            ai_result = await _run_ai_once(text)
            log.info("AI gate result for #%d: %s", msg.id, ai_result.get("status"))
            await asyncio.gather(*(
                _trade_for_account(
                    a, bybit_clients[a.name], bot_token, msg.id, snippet, ai_result,
                )
                for a in accounts
            ), return_exceptions=True)

    await client.start()
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
        await asyncio.gather(*(c.aclose() for c in bybit_clients.values()), return_exceptions=True)


def normalize_user_ids(bot_cfg: Dict[str, Any]) -> List[int]:
    """Legacy helper retained for any out-of-tree callers."""
    raw = bot_cfg.get("user_ids") or []
    if not raw and bot_cfg.get("user_id") not in (None, 0):
        raw = [bot_cfg["user_id"]]
    return [int(x) for x in raw if int(x) != 0]

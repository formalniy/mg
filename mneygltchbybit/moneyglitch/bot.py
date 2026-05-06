"""Telegram control bot. Russian UI.

Each Telegram user is routed to one account (config.accounts[].user_ids).
Settings (amount/leverage/SL/enable) live per-account.

Live position card: when the parser opens a trade, it stores a `position`
block in state with a list of (chat_id, message_id) entries. This bot runs
a background task that, once per second, fetches the current ticker for
each account-with-position and edits every tracked message with the latest
price + PnL + elapsed time.

Close button: clicking "🔴 Закрыть позицию" (callback `close:<account>`)
fires a market reduceOnly order, computes the realized PnL from the exit
price, edits all tracked messages with the final summary, and clears the
position from state.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .accounts import AccountConfig, account_for_user, load_accounts
from .bybit import BybitError, BybitFutures
from .state import load_account, patch_position, set_position, update_account

log = logging.getLogger(__name__)

LIVE_INTERVAL_SECONDS = 1.0


class Form(StatesGroup):
    amount = State()
    leverage = State()
    stop = State()


def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 Сумма (USD)", callback_data="set_amount"),
            InlineKeyboardButton(text="📊 Плечо", callback_data="set_leverage"),
        ],
        [InlineKeyboardButton(text="🛑 Стоп-лосс (%)", callback_data="set_stop")],
        [
            InlineKeyboardButton(text="▶️ Включить", callback_data="enable"),
            InlineKeyboardButton(text="⏸ Остановить", callback_data="disable"),
        ],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="status")],
    ])


def close_kb(account_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Закрыть позицию", callback_data=f"close:{account_name}")]
    ])


def status_text(account: AccountConfig, st: Dict[str, Any]) -> str:
    flag = "✅ ВКЛЮЧЕНА" if st["enabled"] else "⏸ ВЫКЛЮЧЕНА"
    pos = st.get("position")
    pos_line = ""
    if pos and pos.get("symbol"):
        pos_line = (
            f"\n📌 Позиция: <b>{pos.get('side', '?')} {html.escape(str(pos.get('symbol', '')))}</b> "
            f"qty={html.escape(str(pos.get('qty', '?')))} entry={html.escape(str(pos.get('entry_price', '?')))}"
        )
    return (
        f"<b>MoneyGlitch · {html.escape(account.symbol)} (perp)</b> · "
        f"<code>{html.escape(account.name)}</code>\n"
        f"Торговля: <b>{flag}</b>\n"
        f"Сумма: <b>{st['amount_usd']}</b> USD\n"
        f"Плечо: <b>{st['leverage']}x</b>\n"
        f"Стоп-лосс: <b>{st['stop_loss_pct']}%</b>"
        f"{pos_line}\n\n"
        "Параметры применяются к следующей сделке."
    )


def _fmt_elapsed(opened_at_ms: int) -> str:
    elapsed = max(0, int(time.time() * 1000 - int(opened_at_ms)))
    s = elapsed // 1000
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{ss:02d}"
    return f"{m:02d}:{ss:02d}"


def _pnl_usd(entry: Decimal, current: Decimal, qty: Decimal, side: str) -> Decimal:
    if side.lower().startswith("l"):
        return (current - entry) * qty
    return (entry - current) * qty


def render_live_card(account: AccountConfig, pos: Dict[str, Any], current_price: float) -> str:
    entry = Decimal(str(pos.get("entry_price") or "0"))
    qty = Decimal(str(pos.get("qty") or "0"))
    cur = Decimal(str(current_price))
    side = str(pos.get("side") or "Long")
    sl = str(pos.get("sl_price") or "—")
    leverage = int(pos.get("leverage") or 1)
    amount_usd = float(pos.get("amount_usd") or 0)

    pnl = _pnl_usd(entry, cur, qty, side)
    price_pct = (cur - entry) / entry * 100 if entry > 0 else Decimal(0)
    margin_pct = pnl / Decimal(str(amount_usd)) * 100 if amount_usd else Decimal(0)
    arrow = "▲" if cur >= entry else "▼"
    sign = "+" if pnl >= 0 else ""

    return (
        f"🟢 <b>{html.escape(side.upper())} {html.escape(account.symbol)}</b> · "
        f"<code>{html.escape(account.name)}</code>\n"
        f"Entry:   <b>{entry}</b>\n"
        f"Current: <b>{cur}</b>  {arrow} {price_pct:+.2f}%\n"
        f"SL:      <b>{html.escape(sl)}</b>\n"
        f"Qty:     <b>{qty}</b> · плечо {leverage}x · маржа {amount_usd} USD\n"
        f"PnL:     <b>{sign}{pnl:.4f} USD</b>  ({margin_pct:+.2f}% от маржи)\n"
        f"Время:   {_fmt_elapsed(int(pos.get('opened_at_ms') or 0))}"
    )


def render_closed_card(
    account: AccountConfig,
    pos: Dict[str, Any],
    exit_price: float,
    closed_at_ms: int,
) -> str:
    entry = Decimal(str(pos.get("entry_price") or "0"))
    qty = Decimal(str(pos.get("qty") or "0"))
    ex = Decimal(str(exit_price))
    side = str(pos.get("side") or "Long")
    leverage = int(pos.get("leverage") or 1)
    amount_usd = float(pos.get("amount_usd") or 0)

    pnl = _pnl_usd(entry, ex, qty, side)
    price_pct = (ex - entry) / entry * 100 if entry > 0 else Decimal(0)
    margin_pct = pnl / Decimal(str(amount_usd)) * 100 if amount_usd else Decimal(0)
    sign = "+" if pnl >= 0 else ""

    duration_ms = max(0, int(closed_at_ms) - int(pos.get("opened_at_ms") or closed_at_ms))
    s = duration_ms // 1000
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    dur = f"{h:02d}:{m:02d}:{ss:02d}" if h else f"{m:02d}:{ss:02d}"

    return (
        f"🔚 <b>ЗАКРЫТО · {html.escape(side.upper())} {html.escape(account.symbol)}</b> · "
        f"<code>{html.escape(account.name)}</code>\n"
        f"Entry:    <b>{entry}</b>\n"
        f"Exit:     <b>{ex}</b>  ({price_pct:+.2f}%)\n"
        f"Qty:      <b>{qty}</b> · плечо {leverage}x · маржа {amount_usd} USD\n"
        f"PnL:      <b>{sign}{pnl:.4f} USD</b>  ({margin_pct:+.2f}% от маржи)\n"
        f"Длит-ть:  {dur}"
    )


async def _safe_edit(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as e:
        # "message is not modified" / "message to edit not found" — non-fatal
        if "not modified" in str(e).lower():
            return
        log.debug("edit failed chat=%s msg=%s: %s", chat_id, message_id, e)
    except Exception as e:  # noqa: BLE001
        log.debug("edit failed chat=%s msg=%s: %s", chat_id, message_id, e)


async def _safe_edit_callback_msg(q: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await q.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass


def build_dispatcher(
    bot: Bot,
    accounts: List[AccountConfig],
    bybit_clients: Dict[str, BybitFutures],
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    def acct_for_user_id(uid: Optional[int]) -> Optional[AccountConfig]:
        if uid is None:
            return None
        return account_for_user(accounts, uid)

    def acct_for_msg(m: Message) -> Optional[AccountConfig]:
        return acct_for_user_id(m.from_user.id if m.from_user else None)

    def acct_for_cb(q: CallbackQuery) -> Optional[AccountConfig]:
        return acct_for_user_id(q.from_user.id if q.from_user else None)

    @dp.message(Command("start"))
    async def cmd_start(m: Message, state: FSMContext) -> None:
        a = acct_for_msg(m)
        if not a:
            return
        await state.clear()
        await m.answer(status_text(a, load_account(a.name)),
                       reply_markup=main_kb(), parse_mode="HTML")

    @dp.message(Command("status"))
    async def cmd_status(m: Message) -> None:
        a = acct_for_msg(m)
        if not a:
            return
        await m.answer(status_text(a, load_account(a.name)),
                       reply_markup=main_kb(), parse_mode="HTML")

    @dp.callback_query()
    async def on_cb(q: CallbackQuery, state: FSMContext) -> None:
        a = acct_for_cb(q)
        if not a:
            await q.answer("Доступ запрещён", show_alert=True)
            return
        data = q.data or ""

        if data.startswith("close:"):
            target = data.split(":", 1)[1]
            if target != a.name:
                await q.answer("Эта позиция не относится к вашему аккаунту.", show_alert=True)
                return
            await _handle_close(bot, a, bybit_clients[a.name], q)
            return

        st = load_account(a.name)
        if data == "status":
            await _safe_edit_callback_msg(q, status_text(a, st), main_kb())
        elif data == "enable":
            st = update_account(a.name, enabled=True)
            await _safe_edit_callback_msg(q, status_text(a, st), main_kb())
            await q.answer("Торговля включена")
            return
        elif data == "disable":
            st = update_account(a.name, enabled=False)
            await _safe_edit_callback_msg(q, status_text(a, st), main_kb())
            await q.answer("Торговля выключена")
            return
        elif data == "set_amount":
            await state.set_state(Form.amount)
            await q.message.answer("Введите сумму в USD (например, <code>50</code>):", parse_mode="HTML")
        elif data == "set_leverage":
            await state.set_state(Form.leverage)
            await q.message.answer("Введите плечо целым числом, 1–200 (например, <code>10</code>):", parse_mode="HTML")
        elif data == "set_stop":
            await state.set_state(Form.stop)
            await q.message.answer("Введите стоп-лосс в %, 0–100 (например, <code>5</code>):", parse_mode="HTML")

        await q.answer()

    @dp.message(Form.amount)
    async def in_amount(m: Message, state: FSMContext) -> None:
        a = acct_for_msg(m)
        if not a:
            return
        try:
            v = float((m.text or "").replace(",", ".").strip())
            if v <= 0 or v > 1_000_000:
                raise ValueError
        except ValueError:
            await m.answer("Некорректная сумма. Введите положительное число:")
            return
        update_account(a.name, amount_usd=v)
        await state.clear()
        await m.answer(f"💰 Сумма: <b>{v} USD</b>", parse_mode="HTML", reply_markup=main_kb())

    @dp.message(Form.leverage)
    async def in_lev(m: Message, state: FSMContext) -> None:
        a = acct_for_msg(m)
        if not a:
            return
        try:
            v = int((m.text or "").strip())
            if not (1 <= v <= 200):
                raise ValueError
        except ValueError:
            await m.answer("Введите целое число от 1 до 200:")
            return
        update_account(a.name, leverage=v)
        await state.clear()
        await m.answer(f"📊 Плечо: <b>{v}x</b>", parse_mode="HTML", reply_markup=main_kb())

    @dp.message(Form.stop)
    async def in_stop(m: Message, state: FSMContext) -> None:
        a = acct_for_msg(m)
        if not a:
            return
        try:
            v = float((m.text or "").replace(",", ".").strip())
            if not (0 < v < 100):
                raise ValueError
        except ValueError:
            await m.answer("Введите число больше 0 и меньше 100:")
            return
        update_account(a.name, stop_loss_pct=v)
        await state.clear()
        await m.answer(f"🛑 Стоп-лосс: <b>{v}%</b>", parse_mode="HTML", reply_markup=main_kb())

    return dp


async def _handle_close(
    bot: Bot,
    account: AccountConfig,
    bybit: BybitFutures,
    q: CallbackQuery,
) -> None:
    pos = load_account(account.name).get("position") or {}
    if not pos.get("symbol"):
        await q.answer("Позиция уже закрыта.", show_alert=False)
        return
    if pos.get("closing"):
        await q.answer("Закрытие уже выполняется.", show_alert=False)
        return

    patch_position(account.name, closing=True)
    await q.answer("Закрываю…")

    side = str(pos.get("side") or "Long")
    position_side = "Buy" if side.lower().startswith("l") else "Sell"
    qty = str(pos.get("qty") or "0")

    try:
        await bybit.close_position_market(account.symbol, qty, position_side=position_side)
    except BybitError as e:
        patch_position(account.name, closing=False)
        await q.message.answer(
            f"❌ <code>{html.escape(account.name)}</code> · "
            f"Закрытие не удалось: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
        return
    except Exception as e:  # noqa: BLE001
        patch_position(account.name, closing=False)
        log.exception("close failed")
        await q.message.answer(
            f"❌ <code>{html.escape(account.name)}</code> · "
            f"Ошибка закрытия: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
        return

    # Read the actual fill price: ticker is the closest fast proxy.
    try:
        tk = await bybit.ticker(account.symbol)
        exit_price = float(tk.get("lastPrice") or tk.get("markPrice") or 0)
    except Exception:  # noqa: BLE001
        exit_price = float(pos.get("entry_price") or 0)

    closed_at_ms = int(time.time() * 1000)
    final_text = render_closed_card(account, pos, exit_price, closed_at_ms)
    for entry in pos.get("messages", []) or []:
        await _safe_edit(
            bot,
            int(entry["chat_id"]),
            int(entry["message_id"]),
            final_text,
            reply_markup=None,
        )

    set_position(account.name, None)


async def _live_tick(
    bot: Bot,
    account: AccountConfig,
    bybit: BybitFutures,
) -> None:
    st = load_account(account.name)
    pos = st.get("position")
    if not pos or not pos.get("symbol") or pos.get("closing"):
        return
    try:
        tk = await bybit.ticker(account.symbol)
    except Exception as e:  # noqa: BLE001
        log.debug("ticker failed for %s: %s", account.name, e)
        return
    last = float(tk.get("lastPrice") or tk.get("markPrice") or 0)
    if last <= 0:
        return
    text = render_live_card(account, pos, last)
    kb = close_kb(account.name)
    for entry in pos.get("messages", []) or []:
        await _safe_edit(
            bot,
            int(entry["chat_id"]),
            int(entry["message_id"]),
            text,
            reply_markup=kb,
        )


async def live_updater(
    bot: Bot,
    accounts: List[AccountConfig],
    bybit_clients: Dict[str, BybitFutures],
) -> None:
    while True:
        try:
            await asyncio.gather(*(
                _live_tick(bot, a, bybit_clients[a.name]) for a in accounts
            ), return_exceptions=True)
        except Exception as e:  # noqa: BLE001
            log.warning("live updater iteration error: %s", e)
        await asyncio.sleep(LIVE_INTERVAL_SECONDS)


async def run_bot(config: Dict[str, Any]) -> None:
    bot_cfg = config["bot"]
    bot = Bot(token=str(bot_cfg["token"]))

    accounts = load_accounts(config)
    if not accounts:
        raise RuntimeError("config has no accounts configured")
    if not any(a.user_ids for a in accounts):
        raise RuntimeError("no user_ids configured for any account")

    bybit_clients: Dict[str, BybitFutures] = {
        a.name: BybitFutures(a.api_key, a.secret) for a in accounts
    }
    dp = build_dispatcher(bot, accounts, bybit_clients)

    log.info("bot polling started; accounts=%s", [a.name for a in accounts])
    live_task = asyncio.create_task(live_updater(bot, accounts, bybit_clients))
    try:
        await dp.start_polling(bot)
    finally:
        live_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await live_task
        await asyncio.gather(
            *(c.aclose() for c in bybit_clients.values()),
            return_exceptions=True,
        )

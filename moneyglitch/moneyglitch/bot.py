"""Telegram control bot. Russian UI. Owner-gated by config bot.user_id."""
from __future__ import annotations

import logging
from typing import Any, Dict

from aiogram import Bot, Dispatcher
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

from .state import load_state, save_state

log = logging.getLogger(__name__)


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


def status_text(st: Dict[str, Any]) -> str:
    flag = "✅ ВКЛЮЧЕНА" if st["enabled"] else "⏸ ВЫКЛЮЧЕНА"
    return (
        "<b>MoneyGlitch · TONUSDT (perp)</b>\n"
        f"Торговля: <b>{flag}</b>\n"
        f"Сумма: <b>{st['amount_usd']}</b> USD\n"
        f"Плечо: <b>{st['leverage']}x</b>\n"
        f"Стоп-лосс: <b>{st['stop_loss_pct']}%</b>\n\n"
        "Параметры применяются к следующей сделке."
    )


def build_dispatcher(owner_id: int) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    def is_owner_msg(m: Message) -> bool:
        return m.from_user is not None and m.from_user.id == owner_id

    def is_owner_cb(q: CallbackQuery) -> bool:
        return q.from_user is not None and q.from_user.id == owner_id

    @dp.message(Command("start"))
    async def cmd_start(m: Message, state: FSMContext) -> None:
        if not is_owner_msg(m):
            return
        await state.clear()
        await m.answer(status_text(load_state()), reply_markup=main_kb(), parse_mode="HTML")

    @dp.message(Command("status"))
    async def cmd_status(m: Message) -> None:
        if not is_owner_msg(m):
            return
        await m.answer(status_text(load_state()), reply_markup=main_kb(), parse_mode="HTML")

    @dp.callback_query()
    async def on_cb(q: CallbackQuery, state: FSMContext) -> None:
        if not is_owner_cb(q):
            await q.answer("Доступ запрещён", show_alert=True)
            return
        data = q.data or ""
        st = load_state()

        if data == "status":
            await _safe_edit(q, status_text(st))
        elif data == "enable":
            st["enabled"] = True
            save_state(st)
            await _safe_edit(q, status_text(st))
            await q.answer("Торговля включена")
            return
        elif data == "disable":
            st["enabled"] = False
            save_state(st)
            await _safe_edit(q, status_text(st))
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
        if not is_owner_msg(m):
            return
        try:
            v = float((m.text or "").replace(",", ".").strip())
            if v <= 0 or v > 1_000_000:
                raise ValueError
        except ValueError:
            await m.answer("Некорректная сумма. Введите положительное число:")
            return
        st = load_state()
        st["amount_usd"] = v
        save_state(st)
        await state.clear()
        await m.answer(f"💰 Сумма: <b>{v} USD</b>", parse_mode="HTML", reply_markup=main_kb())

    @dp.message(Form.leverage)
    async def in_lev(m: Message, state: FSMContext) -> None:
        if not is_owner_msg(m):
            return
        try:
            v = int((m.text or "").strip())
            if not (1 <= v <= 200):
                raise ValueError
        except ValueError:
            await m.answer("Введите целое число от 1 до 200:")
            return
        st = load_state()
        st["leverage"] = v
        save_state(st)
        await state.clear()
        await m.answer(f"📊 Плечо: <b>{v}x</b>", parse_mode="HTML", reply_markup=main_kb())

    @dp.message(Form.stop)
    async def in_stop(m: Message, state: FSMContext) -> None:
        if not is_owner_msg(m):
            return
        try:
            v = float((m.text or "").replace(",", ".").strip())
            if not (0 < v < 100):
                raise ValueError
        except ValueError:
            await m.answer("Введите число больше 0 и меньше 100:")
            return
        st = load_state()
        st["stop_loss_pct"] = v
        save_state(st)
        await state.clear()
        await m.answer(f"🛑 Стоп-лосс: <b>{v}%</b>", parse_mode="HTML", reply_markup=main_kb())

    return dp


async def _safe_edit(q: CallbackQuery, text: str) -> None:
    try:
        await q.message.edit_text(text, reply_markup=main_kb(), parse_mode="HTML")
    except Exception:
        # Telegram raises when the rendered text is identical; ignore.
        pass


async def run_bot(config: Dict[str, Any]) -> None:
    bot_cfg = config["bot"]
    bot = Bot(token=str(bot_cfg["token"]))
    dp = build_dispatcher(int(bot_cfg["user_id"]))
    log.info("bot polling started")
    await dp.start_polling(bot)

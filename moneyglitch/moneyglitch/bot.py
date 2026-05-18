"""Telegram control bot. Russian UI.

Each Telegram user is routed to one account (config.accounts[].user_ids).
Settings (amount/leverage/SL/enable) live per-account.

Live position card: when the parser opens a trade, it stores a `position`
block in state with a list of (chat_id, message_id) entries. This bot runs
a background task that, once per second, fetches the current ticker for
each account-with-position and edits every tracked message with the latest
price + PnL + elapsed time.

Close button: clicking "🔴 Закрыть позицию" (callback `close:<account>`)
fires a market close-side order, computes the realized PnL from the exit
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
from .ai import AI_CONFIG_PATH, ai_config_status
from .mexc import (
    MEXCError,
    MEXCFutures,
    OPEN_TYPE_CROSS,
    OPEN_TYPE_ISOLATED,
    _find_usdt,
)
from .state import load_account, patch_position, set_position, update_account

log = logging.getLogger(__name__)

LIVE_INTERVAL_SECONDS = 1.0
BALANCE_INTERVAL_SECONDS = 1.0
# fee_rate rarely changes; refetch once per hour at most.
FEE_CACHE_TTL_MS = 60 * 60 * 1000
# /start balance call is on-demand; reuse a fresh cached value within ~10s
# so back-to-back status refreshes don't spam wallet-balance.
STATUS_BALANCE_TTL_MS = 10_000

# Per-account cached USDT wallet snapshot. Populated by balance_updater on
# its own cadence so the live tick never blocks on a wallet API call.
# Schema: {<account>: {"equity": str, "wallet": str, "available": str, "ts": int}}
_BALANCE_CACHE: Dict[str, Dict[str, Any]] = {}

# Per-account cached taker fee rate. {<account>: {"fee": Decimal, "ts": int}}
_FEE_CACHE: Dict[str, Dict[str, Any]] = {}


class Form(StatesGroup):
    amount = State()
    leverage = State()
    stop = State()
    take_profit = State()
    sell_pct_1 = State()
    sell_pct_2 = State()
    sell_pct_3 = State()
    sell_pct_4 = State()


def _fmt_pct(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{f:g}"


def _sell_pcts(st: Dict[str, Any]) -> List[float]:
    out: List[float] = []
    for i in range(1, 5):
        try:
            out.append(float(st.get(f"sell_pct_{i}") or 0))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


def main_kb(st: Dict[str, Any]) -> InlineKeyboardMarkup:
    fn_label = (
        "🧮 Нейтрализация: ВКЛ" if st.get("fee_neutralize_enabled")
        else "🧮 Нейтрализация: ВЫКЛ"
    )
    p = _sell_pcts(st)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 Сумма (USD)", callback_data="set_amount"),
            InlineKeyboardButton(text="📊 Плечо", callback_data="set_leverage"),
        ],
        [
            InlineKeyboardButton(text="🛑 Стоп-лосс (% маржи)", callback_data="set_stop"),
            InlineKeyboardButton(text="🎯 Тейк-профит (% маржи)", callback_data="set_take_profit"),
        ],
        [InlineKeyboardButton(text=fn_label, callback_data="toggle_fn")],
        [
            InlineKeyboardButton(text=f"💸 Продажа №1: {_fmt_pct(p[0])}%", callback_data="set_sell_1"),
            InlineKeyboardButton(text=f"💸 Продажа №2: {_fmt_pct(p[1])}%", callback_data="set_sell_2"),
        ],
        [
            InlineKeyboardButton(text=f"💸 Продажа №3: {_fmt_pct(p[2])}%", callback_data="set_sell_3"),
            InlineKeyboardButton(text=f"💸 Продажа №4: {_fmt_pct(p[3])}%", callback_data="set_sell_4"),
        ],
        [
            InlineKeyboardButton(text="▶️ Включить", callback_data="enable"),
            InlineKeyboardButton(text="⏸ Остановить", callback_data="disable"),
        ],
        [InlineKeyboardButton(text="🧠 Нейронка", callback_data="ai_menu")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="status")],
    ])


def ai_kb(st: Dict[str, Any]) -> InlineKeyboardMarkup:
    on_label = "🧠 Фильтр ИИ: ВКЛ" if st.get("ai_enabled") else "🧠 Фильтр ИИ: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=on_label, callback_data="ai_toggle")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="ai_menu")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="ai_back")],
    ])


def _mask_key(key: str) -> str:
    k = str(key or "")
    if not k:
        return "—"
    if len(k) <= 8:
        return "•" * len(k)
    return f"{k[:4]}…{k[-4:]}"


def _ai_provider_label(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p == "openrouter":
        return "OpenRouter"
    if p in ("huggingface", "hf"):
        return "Hugging Face"
    return provider or "—"


def _ai_cfg_state_line(status: Dict[str, Any]) -> str:
    """One-line human description of why the global AI config is or isn't
    usable. Shown verbatim in the bot's «Нейронка» menu."""
    state = status.get("state")
    if state == "ready":
        return "✅ настроена"
    if state == "missing":
        return (
            f"⚠️ файл <code>{html.escape(str(AI_CONFIG_PATH))}</code> "
            "не найден"
        )
    if state == "unreadable":
        reason = html.escape(str(status.get("reason") or ""))
        return f"⚠️ файл не читается: <code>{reason}</code>"
    if state == "invalid_json":
        reason = html.escape(str(status.get("reason") or ""))
        return f"⚠️ невалидный JSON: <code>{reason}</code>"
    if state == "not_object":
        return "⚠️ корень JSON — не объект"
    if state == "incomplete":
        miss = ", ".join(status.get("missing") or [])
        return f"⚠️ пустые поля: <code>{html.escape(miss)}</code>"
    return "⚠️ неизвестное состояние"


def ai_text(account: AccountConfig, st: Dict[str, Any]) -> str:
    flag = "✅ ВКЛ" if st.get("ai_enabled") else "⏸ ВЫКЛ"
    status = ai_config_status()
    cfg = status.get("cfg") or {}
    provider = _ai_provider_label(str(cfg.get("provider") or ""))
    model = str(cfg.get("model") or "—")
    prompt = str(cfg.get("system_prompt") or "")
    if len(prompt) > 200:
        prompt = prompt[:200] + "…"
    prompt_line = html.escape(prompt) if prompt else "—"
    return (
        f"<b>🧠 Нейронка</b> · <code>{html.escape(account.name)}</code>\n"
        f"Фильтр у вас: <b>{flag}</b>\n\n"
        f"<b>Глобальные настройки</b> (на VPS, файл "
        f"<code>{html.escape(str(AI_CONFIG_PATH))}</code>)\n"
        f"Состояние: {_ai_cfg_state_line(status)}\n"
        f"Провайдер: <b>{html.escape(provider)}</b>\n"
        f"Модель: <code>{html.escape(model)}</code>\n"
        f"API ключ: <code>{html.escape(_mask_key(str(cfg.get('api_key') or '')))}</code>\n"
        f"Промпт: {prompt_line}\n\n"
        "Если фильтр <b>ВКЛ</b>, ваша сделка откроется только когда нейросеть "
        "вернёт <code>1</code> по этому посту. При <b>ВЫКЛ</b> работает обычное "
        "правило: открываем сделку при каждом посте с TON (если торговля включена)."
    )


def close_kb(account_name: str, sell_pcts: List[float]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"💸 {_fmt_pct(sell_pcts[0])}%", callback_data=f"sell:{account_name}:1"),
            InlineKeyboardButton(text=f"💸 {_fmt_pct(sell_pcts[1])}%", callback_data=f"sell:{account_name}:2"),
        ],
        [
            InlineKeyboardButton(text=f"💸 {_fmt_pct(sell_pcts[2])}%", callback_data=f"sell:{account_name}:3"),
            InlineKeyboardButton(text=f"💸 {_fmt_pct(sell_pcts[3])}%", callback_data=f"sell:{account_name}:4"),
        ],
        [InlineKeyboardButton(text="🔴 Закрыть позицию", callback_data=f"close:{account_name}")]
    ])


def _status_balance_line(account_name: str) -> str:
    bal = _BALANCE_CACHE.get(account_name)
    if not bal:
        return "Баланс: <b>—</b>"
    eq = bal.get("equity")
    av = bal.get("available")
    parts = []
    if eq is not None:
        parts.append(f"<b>{eq}</b> USDT")
    if av is not None:
        parts.append(f"доступно <b>{av}</b>")
    return "Баланс: " + (" · ".join(parts) if parts else "<b>—</b>")


def _fee_line(account_name: str, amount_usd: float, leverage: int) -> str:
    cached = _FEE_CACHE.get(account_name)
    if not cached:
        return "Комиссия откр.+закр.: <b>—</b>"
    fee = cached["fee"]  # Decimal
    notional = Decimal(str(amount_usd)) * Decimal(int(leverage))
    one_side = notional * fee
    round_trip = one_side * Decimal(2)
    fee_pct = fee * Decimal(100)
    return (
        f"Комиссия откр./закр.: <b>{one_side:.4f}</b> / <b>{one_side:.4f}</b> USDT · "
        f"итого <b>{round_trip:.4f}</b> (тейкер {fee_pct:.4f}%)"
    )


def status_text(account: AccountConfig, st: Dict[str, Any]) -> str:
    flag = "✅ ВКЛЮЧЕНА" if st["enabled"] else "⏸ ВЫКЛЮЧЕНА"
    pos = st.get("position")
    pos_line = ""
    if pos and pos.get("symbol"):
        pos_line = (
            f"\n📌 Позиция: <b>{pos.get('side', '?')} {html.escape(str(pos.get('symbol', '')))}</b> "
            f"qty={html.escape(str(pos.get('qty', '?')))} entry={html.escape(str(pos.get('entry_price', '?')))}"
        )
    tp_pct = float(st.get("take_profit_pct") or 0.0)
    tp_line = f"Тейк-профит: <b>{tp_pct}% маржи</b>" if tp_pct > 0 else "Тейк-профит: <b>выкл</b>"
    fn_on = bool(st.get("fee_neutralize_enabled") or False)
    fn_line = "Нейтрализация: <b>ВКЛ</b>" if fn_on else "Нейтрализация: <b>выкл</b>"
    balance_line = _status_balance_line(account.name)
    fee_line = _fee_line(
        account.name,
        float(st.get("amount_usd") or 0),
        int(st.get("leverage") or 1),
    )
    return (
        f"<b>MoneyGlitch · {html.escape(account.symbol)} (perp)</b> · "
        f"<code>{html.escape(account.name)}</code>\n"
        f"Торговля: <b>{flag}</b>\n"
        f"Сумма: <b>{st['amount_usd']}</b> USD\n"
        f"Плечо: <b>{st['leverage']}x</b>\n"
        f"Стоп-лосс: <b>{st['stop_loss_pct']}% маржи</b>\n"
        f"{tp_line}\n"
        f"{fn_line}\n"
        f"{balance_line}\n"
        f"{fee_line}"
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


def _balance_line(account_name: str) -> str:
    bal = _BALANCE_CACHE.get(account_name)
    if not bal:
        return "Баланс:  —"
    eq = bal.get("equity")
    av = bal.get("available")
    parts = []
    if eq is not None:
        parts.append(f"<b>{eq}</b> USDT")
    if av is not None:
        parts.append(f"avail {av}")
    return "Баланс:  " + (" · ".join(parts) if parts else "—")


def render_live_card(account: AccountConfig, pos: Dict[str, Any], current_price: float) -> str:
    entry = Decimal(str(pos.get("entry_price") or "0"))
    qty = Decimal(str(pos.get("qty") or "0"))
    cur = Decimal(str(current_price))
    side = str(pos.get("side") or "Long")
    sl = str(pos.get("sl_price") or "—")
    tp = pos.get("tp_price")
    leverage = int(pos.get("leverage") or 1)
    amount_usd = float(pos.get("amount_usd") or 0)
    tp_pct = float(pos.get("take_profit_pct") or 0.0)
    sl_pct = float(pos.get("stop_loss_pct") or 0.0)

    pnl = _pnl_usd(entry, cur, qty, side)
    price_pct = (cur - entry) / entry * 100 if entry > 0 else Decimal(0)
    margin_pct = pnl / Decimal(str(amount_usd)) * 100 if amount_usd else Decimal(0)
    arrow = "▲" if cur >= entry else "▼"
    sign = "+" if pnl >= 0 else ""

    sl_line = f"SL:      <b>{html.escape(sl)}</b>"
    if sl_pct:
        sl_line += f" (-{sl_pct}% маржи)"
    tp_line = (
        f"TP:      <b>{html.escape(str(tp))}</b>"
        + (f" (+{tp_pct}% маржи)" if tp_pct else "")
        if tp else "TP:      —"
    )
    neutral_tp = pos.get("neutral_tp_price")
    neutral_qty = pos.get("neutral_tp_qty")
    neutral_line = (
        f"\n🧮 TP-фи: <b>{html.escape(str(neutral_tp))}</b> · "
        f"qty <b>{html.escape(str(neutral_qty or '?'))}</b>"
        if neutral_tp else ""
    )

    return (
        f"🟢 <b>{html.escape(side.upper())} {html.escape(account.symbol)}</b> · "
        f"<code>{html.escape(account.name)}</code>\n"
        f"Entry:   <b>{entry}</b>\n"
        f"Current: <b>{cur}</b>  {arrow} {price_pct:+.2f}%\n"
        f"{sl_line}\n"
        f"{tp_line}{neutral_line}\n"
        f"Qty:     <b>{qty}</b> · плечо {leverage}x · маржа {amount_usd} USD\n"
        f"PnL:     <b>{sign}{pnl:.4f} USD</b>  ({margin_pct:+.2f}% от маржи)\n"
        f"{_balance_line(account.name)}\n"
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
        f"{_balance_line(account.name)}\n"
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


def _open_type_for(account: AccountConfig) -> int:
    return OPEN_TYPE_ISOLATED if account.isolated else OPEN_TYPE_CROSS


def build_dispatcher(
    bot: Bot,
    accounts: List[AccountConfig],
    mexc_clients: Dict[str, MEXCFutures],
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
        await ensure_status_data(a, mexc_clients[a.name])
        st = load_account(a.name)
        await m.answer(
            status_text(a, st),
            reply_markup=main_kb(st),
            parse_mode="HTML",
        )

    @dp.message(Command("status"))
    async def cmd_status(m: Message) -> None:
        a = acct_for_msg(m)
        if not a:
            return
        await ensure_status_data(a, mexc_clients[a.name])
        st = load_account(a.name)
        await m.answer(
            status_text(a, st),
            reply_markup=main_kb(st),
            parse_mode="HTML",
        )

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
            await _handle_close(bot, a, mexc_clients[a.name], q)
            return

        if data.startswith("sell:"):
            parts = data.split(":")
            if len(parts) != 3 or parts[1] != a.name:
                await q.answer("Кнопка не относится к вашему аккаунту.", show_alert=True)
                return
            try:
                idx = int(parts[2])
            except ValueError:
                await q.answer("Некорректная кнопка.", show_alert=True)
                return
            if not (1 <= idx <= 4):
                await q.answer("Некорректная кнопка.", show_alert=True)
                return
            await _handle_partial_sell(bot, a, mexc_clients[a.name], q, idx)
            return

        st = load_account(a.name)
        if data == "status":
            await ensure_status_data(a, mexc_clients[a.name])
            st = load_account(a.name)
            await _safe_edit_callback_msg(q, status_text(a, st), main_kb(st))
        elif data == "enable":
            st = update_account(a.name, enabled=True)
            await _safe_edit_callback_msg(q, status_text(a, st), main_kb(st))
            await q.answer("Торговля включена")
            return
        elif data == "disable":
            st = update_account(a.name, enabled=False)
            await _safe_edit_callback_msg(q, status_text(a, st), main_kb(st))
            await q.answer("Торговля выключена")
            return
        elif data == "toggle_fn":
            new_val = not bool(st.get("fee_neutralize_enabled") or False)
            st = update_account(a.name, fee_neutralize_enabled=new_val)
            await _safe_edit_callback_msg(q, status_text(a, st), main_kb(st))
            await q.answer(
                "Нейтрализация комиссии: ВКЛ" if new_val
                else "Нейтрализация комиссии: ВЫКЛ"
            )
            return
        elif data == "set_amount":
            await state.set_state(Form.amount)
            await q.message.answer("Введите сумму в USD (например, <code>50</code>):", parse_mode="HTML")
        elif data == "set_leverage":
            await state.set_state(Form.leverage)
            await q.message.answer("Введите плечо целым числом, 1–200 (например, <code>10</code>):", parse_mode="HTML")
        elif data == "set_stop":
            await state.set_state(Form.stop)
            await q.message.answer(
                "Введите стоп-лосс в % от маржи (с плечом), 0–100 "
                "(например, <code>20</code> при плече 50x = -0.4% к цене):",
                parse_mode="HTML",
            )
        elif data == "set_take_profit":
            await state.set_state(Form.take_profit)
            await q.message.answer(
                "Введите тейк-профит в % от маржи (с плечом), 0–10000 "
                "(0 — отключить, например, <code>200</code> при плече 50x = +4% к цене):",
                parse_mode="HTML",
            )
        elif data == "ai_menu":
            await _safe_edit_callback_msg(q, ai_text(a, st), ai_kb(st))
        elif data == "ai_back":
            await _safe_edit_callback_msg(q, status_text(a, st), main_kb(st))
        elif data == "ai_toggle":
            new_val = not bool(st.get("ai_enabled") or False)
            st = update_account(a.name, ai_enabled=new_val)
            await _safe_edit_callback_msg(q, ai_text(a, st), ai_kb(st))
            await q.answer("Фильтр ИИ: ВКЛ" if new_val else "Фильтр ИИ: ВЫКЛ")
            return
        elif data.startswith("set_sell_"):
            try:
                idx = int(data.split("_")[-1])
            except ValueError:
                await q.answer("Некорректная кнопка.", show_alert=True)
                return
            if not (1 <= idx <= 4):
                await q.answer("Некорректная кнопка.", show_alert=True)
                return
            await state.set_state(getattr(Form, f"sell_pct_{idx}"))
            await q.message.answer(
                f"Введите % продажи для кнопки №{idx} (0.1–100, например <code>50</code>):",
                parse_mode="HTML",
            )

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
        st = update_account(a.name, amount_usd=v)
        await state.clear()
        await m.answer(
            f"💰 Сумма: <b>{v} USD</b>",
            parse_mode="HTML",
            reply_markup=main_kb(st),
        )

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
        st = update_account(a.name, leverage=v)
        await state.clear()
        await m.answer(
            f"📊 Плечо: <b>{v}x</b>",
            parse_mode="HTML",
            reply_markup=main_kb(st),
        )

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
        st = update_account(a.name, stop_loss_pct=v)
        await state.clear()
        await m.answer(
            f"🛑 Стоп-лосс: <b>{v}% маржи</b>",
            parse_mode="HTML",
            reply_markup=main_kb(st),
        )

    @dp.message(Form.take_profit)
    async def in_tp(m: Message, state: FSMContext) -> None:
        a = acct_for_msg(m)
        if not a:
            return
        try:
            v = float((m.text or "").replace(",", ".").strip())
            if not (0 <= v <= 10000):
                raise ValueError
        except ValueError:
            await m.answer("Введите число от 0 до 10000 (0 — отключить):")
            return
        st = update_account(a.name, take_profit_pct=v)
        await state.clear()
        label = f"<b>{v}% маржи</b>" if v > 0 else "<b>выкл</b>"
        await m.answer(
            f"🎯 Тейк-профит: {label}",
            parse_mode="HTML",
            reply_markup=main_kb(st),
        )

    async def _in_sell_pct(m: Message, state: FSMContext, idx: int) -> None:
        a = acct_for_msg(m)
        if not a:
            return
        try:
            v = float((m.text or "").replace(",", ".").strip())
            if not (0 < v <= 100):
                raise ValueError
        except ValueError:
            await m.answer("Введите число от 0.1 до 100:")
            return
        st = update_account(a.name, **{f"sell_pct_{idx}": v})
        await state.clear()
        await m.answer(
            f"💸 Кнопка продажи №{idx}: <b>{v:g}%</b>",
            parse_mode="HTML",
            reply_markup=main_kb(st),
        )

    @dp.message(Form.sell_pct_1)
    async def in_sell_1(m: Message, state: FSMContext) -> None:
        await _in_sell_pct(m, state, 1)

    @dp.message(Form.sell_pct_2)
    async def in_sell_2(m: Message, state: FSMContext) -> None:
        await _in_sell_pct(m, state, 2)

    @dp.message(Form.sell_pct_3)
    async def in_sell_3(m: Message, state: FSMContext) -> None:
        await _in_sell_pct(m, state, 3)

    @dp.message(Form.sell_pct_4)
    async def in_sell_4(m: Message, state: FSMContext) -> None:
        await _in_sell_pct(m, state, 4)

    return dp


async def _handle_close(
    bot: Bot,
    account: AccountConfig,
    mexc: MEXCFutures,
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
    leverage = int(pos.get("leverage") or 10)
    open_type = _open_type_for(account)

    # `vol` is what MEXC needs (integer contracts). Tracked at open time; we
    # also re-read live below to capture any partial fill that already
    # reduced size (e.g. fee-neutralize TP fired).
    vol = str(pos.get("vol") or "0")
    contract_size = Decimal(str(pos.get("contract_size") or "1"))
    position_id = pos.get("position_id")

    # Cancel any pending fee-neutralize / main-TP close-side Limit orders
    # before market-closing — leftover Limits would otherwise sit on the
    # book and could fire on a later position. cancel-all is idempotent.
    try:
        await mexc.cancel_all_orders(account.symbol)
    except Exception as e:  # noqa: BLE001
        log.debug("pre-close cancel_all_orders failed for %s: %s", account.name, e)

    try:
        try:
            live_pos = await mexc.get_open_position(account.symbol)
            if live_pos and int(Decimal(str(live_pos.get("size") or "0"))) > 0:
                vol = str(live_pos.get("size"))
                pid = live_pos.get("positionId")
                if pid is not None:
                    try:
                        position_id = int(pid)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001
            log.debug("live position read failed for %s: %s", account.name, e)

        await mexc.close_position_market(
            account.symbol,
            vol,
            position_side=position_side,
            leverage=leverage,
            open_type=open_type,
            position_id=position_id if position_id is not None else None,
        )
    except MEXCError as e:
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
        tk = await mexc.ticker(account.symbol)
        exit_price = float(tk.get("lastPrice") or tk.get("fairPrice") or 0)
    except Exception:  # noqa: BLE001
        exit_price = float(pos.get("entry_price") or 0)

    # Use the live qty (vol * contractSize) for the closed-card PnL so a
    # partial fee-neutralize fill is accounted for correctly.
    try:
        live_qty = str(Decimal(vol) * contract_size)
        pos_for_card = {**pos, "qty": live_qty}
    except Exception:  # noqa: BLE001
        pos_for_card = pos

    closed_at_ms = int(time.time() * 1000)
    final_text = render_closed_card(account, pos_for_card, exit_price, closed_at_ms)
    for entry in pos.get("messages", []) or []:
        await _safe_edit(
            bot,
            int(entry["chat_id"]),
            int(entry["message_id"]),
            final_text,
            reply_markup=None,
        )

    set_position(account.name, None)


async def _handle_partial_sell(
    bot: Bot,
    account: AccountConfig,
    mexc: MEXCFutures,
    q: CallbackQuery,
    button_idx: int,
) -> None:
    st = load_account(account.name)
    pos = st.get("position") or {}
    if not pos.get("symbol"):
        await q.answer("Позиция уже закрыта.", show_alert=False)
        return
    if pos.get("closing"):
        await q.answer("Закрытие уже выполняется.", show_alert=False)
        return

    pct = float(st.get(f"sell_pct_{button_idx}") or 0)
    if pct <= 0 or pct > 100:
        await q.answer(
            "Сначала задайте % для этой кнопки в /start.", show_alert=True,
        )
        return

    # 100% behaves identically to the existing close-position flow — let
    # that one handle SL/TP order cleanup and the closed-card rendering.
    if pct >= 100:
        await _handle_close(bot, account, mexc, q)
        return

    await q.answer(f"Продаю {pct:g}%…")

    side = str(pos.get("side") or "Long")
    position_side = "Buy" if side.lower().startswith("l") else "Sell"
    leverage = int(pos.get("leverage") or 10)
    open_type = _open_type_for(account)

    try:
        live_pos = await mexc.get_open_position(account.symbol)
    except Exception as e:  # noqa: BLE001
        await q.message.answer(
            f"❌ <code>{html.escape(account.name)}</code> · "
            f"Не удалось прочитать позицию: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
        return
    if not live_pos or int(Decimal(str(live_pos.get("size") or "0"))) <= 0:
        await q.answer("Позиция уже закрыта.", show_alert=False)
        return
    live_vol = int(Decimal(str(live_pos.get("size"))))
    live_position_id = live_pos.get("positionId")

    try:
        info = await mexc.instrument_info(account.symbol)
    except Exception as e:  # noqa: BLE001
        await q.message.answer(
            f"❌ <code>{html.escape(account.name)}</code> · "
            f"Не удалось прочитать инструмент: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
        return
    min_vol = int(info.get("minVol") or 1)
    contract_size = Decimal(str(info.get("contractSize") or pos.get("contract_size") or "1"))

    target = Decimal(live_vol) * Decimal(str(pct)) / Decimal(100)
    sell_vol = int(target)  # floor — MEXC vol must be integer contracts
    if sell_vol < min_vol:
        await q.message.answer(
            f"⚠️ <code>{html.escape(account.name)}</code> · "
            f"{pct:g}% от позиции ({live_vol} контр.) меньше минимального объёма "
            f"({min_vol}). Увеличьте % или используйте «Закрыть позицию».",
            parse_mode="HTML",
        )
        return
    if sell_vol >= live_vol:
        await _handle_close(bot, account, mexc, q)
        return

    try:
        await mexc.close_position_market(
            account.symbol,
            str(sell_vol),
            position_side=position_side,
            leverage=leverage,
            open_type=open_type,
            position_id=int(live_position_id) if live_position_id is not None else None,
        )
    except MEXCError as e:
        await q.message.answer(
            f"❌ <code>{html.escape(account.name)}</code> · "
            f"Частичная продажа не удалась: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
        return
    except Exception as e:  # noqa: BLE001
        log.exception("partial sell failed")
        await q.message.answer(
            f"❌ <code>{html.escape(account.name)}</code> · "
            f"Ошибка продажи: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
        return

    # Best-effort fill price for the realized-pnl notification — ticker is
    # the closest fast proxy and matches the existing close flow.
    try:
        tk = await mexc.ticker(account.symbol)
        exit_price = Decimal(str(tk.get("lastPrice") or tk.get("fairPrice") or 0))
    except Exception:  # noqa: BLE001
        exit_price = Decimal(str(pos.get("entry_price") or 0))
    entry = Decimal(str(pos.get("entry_price") or 0))
    sold_qty = Decimal(sell_vol) * contract_size
    realized = (
        (exit_price - entry) * sold_qty
        if position_side == "Buy"
        else (entry - exit_price) * sold_qty
    )
    sign = "+" if realized >= 0 else ""

    remaining_vol = live_vol - sell_vol
    remaining_qty = Decimal(remaining_vol) * contract_size
    patch_position(account.name, qty=str(remaining_qty), vol=str(remaining_vol))

    await q.message.answer(
        f"💸 <code>{html.escape(account.name)}</code> · "
        f"Продано <b>{sold_qty}</b> ({pct:g}% / {sell_vol} контр.) "
        f"по ~<b>{exit_price}</b> · "
        f"PnL: <b>{sign}{realized:.4f} USDT</b>\n"
        f"Осталось в позиции: <b>{remaining_qty}</b> ({remaining_vol} контр.)",
        parse_mode="HTML",
    )


async def _live_tick(
    bot: Bot,
    account: AccountConfig,
    mexc: MEXCFutures,
) -> None:
    st = load_account(account.name)
    pos = st.get("position")
    if not pos or not pos.get("symbol") or pos.get("closing"):
        return
    try:
        tk = await mexc.ticker(account.symbol)
    except Exception as e:  # noqa: BLE001
        log.debug("ticker failed for %s: %s", account.name, e)
        return
    last = float(tk.get("lastPrice") or tk.get("fairPrice") or 0)
    if last <= 0:
        return
    text = render_live_card(account, pos, last)
    kb = close_kb(account.name, _sell_pcts(st))
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
    mexc_clients: Dict[str, MEXCFutures],
) -> None:
    while True:
        try:
            await asyncio.gather(*(
                _live_tick(bot, a, mexc_clients[a.name]) for a in accounts
            ), return_exceptions=True)
        except Exception as e:  # noqa: BLE001
            log.warning("live updater iteration error: %s", e)
        await asyncio.sleep(LIVE_INTERVAL_SECONDS)


async def _refresh_balance(account: AccountConfig, mexc: MEXCFutures) -> None:
    try:
        wb = await mexc.wallet_balance("USDT")
    except Exception as e:  # noqa: BLE001
        log.debug("wallet_balance failed for %s: %s", account.name, e)
        return
    usdt = _find_usdt(wb)
    if not usdt:
        return
    _BALANCE_CACHE[account.name] = {
        "equity": usdt.get("equity"),
        "wallet": usdt.get("walletBalance"),
        "available": usdt.get("availableToWithdraw") or usdt.get("availableBalance"),
        "ts": int(time.time() * 1000),
    }


async def _refresh_fee_rate(account: AccountConfig, mexc: MEXCFutures) -> None:
    try:
        fee = await mexc.fee_rate(account.symbol)
    except Exception as e:  # noqa: BLE001
        log.debug("fee_rate failed for %s: %s", account.name, e)
        return
    _FEE_CACHE[account.name] = {"fee": fee, "ts": int(time.time() * 1000)}


async def ensure_status_data(account: AccountConfig, mexc: MEXCFutures) -> None:
    """Make sure the balance + fee caches are populated before rendering
    the status card. Reuses fresh values to avoid hammering the API on
    back-to-back /start or refresh clicks."""
    now = int(time.time() * 1000)
    tasks = []
    bal = _BALANCE_CACHE.get(account.name)
    if not bal or now - int(bal.get("ts") or 0) > STATUS_BALANCE_TTL_MS:
        tasks.append(_refresh_balance(account, mexc))
    fee = _FEE_CACHE.get(account.name)
    if not fee or now - int(fee.get("ts") or 0) > FEE_CACHE_TTL_MS:
        tasks.append(_refresh_fee_rate(account, mexc))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _balance_tick(account: AccountConfig, mexc: MEXCFutures) -> None:
    pos = (load_account(account.name) or {}).get("position")
    if not pos or not pos.get("symbol"):
        # Idle → no need for high-cadence updates. The /start refresh path
        # still updates _BALANCE_CACHE via ensure_status_data on demand.
        return
    await _refresh_balance(account, mexc)


async def balance_updater(
    accounts: List[AccountConfig],
    mexc_clients: Dict[str, MEXCFutures],
) -> None:
    """Refresh USDT wallet snapshot per account on its own cadence.

    Decoupled from live_updater so a slow wallet_balance call never delays
    the price/PnL message edit. Only runs the API call when a position is
    open — idle accounts incur zero load."""
    while True:
        try:
            await asyncio.gather(*(
                _balance_tick(a, mexc_clients[a.name]) for a in accounts
            ), return_exceptions=True)
        except Exception as e:  # noqa: BLE001
            log.warning("balance updater iteration error: %s", e)
        await asyncio.sleep(BALANCE_INTERVAL_SECONDS)


async def run_bot(config: Dict[str, Any]) -> None:
    bot_cfg = config["bot"]
    bot = Bot(token=str(bot_cfg["token"]))

    accounts = load_accounts(config)
    if not accounts:
        raise RuntimeError("config has no accounts configured")
    if not any(a.user_ids for a in accounts):
        raise RuntimeError("no user_ids configured for any account")

    mexc_clients: Dict[str, MEXCFutures] = {
        a.name: MEXCFutures(a.api_key, a.secret) for a in accounts
    }
    dp = build_dispatcher(bot, accounts, mexc_clients)

    log.info("bot polling started; accounts=%s", [a.name for a in accounts])
    live_task = asyncio.create_task(live_updater(bot, accounts, mexc_clients))
    bal_task = asyncio.create_task(balance_updater(accounts, mexc_clients))
    try:
        await dp.start_polling(bot)
    finally:
        for t in (live_task, bal_task):
            t.cancel()
        for t in (live_task, bal_task):
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await asyncio.gather(
            *(c.aclose() for c in mexc_clients.values()),
            return_exceptions=True,
        )

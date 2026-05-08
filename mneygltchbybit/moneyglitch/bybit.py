"""Minimal Bybit v5 perpetual futures client (USDT-margined / linear).

Uses api.bybit.com endpoints with HMAC-SHA256 signing as documented at
https://bybit-exchange.github.io/docs/v5/intro

Sign rule (v5):
    sign_payload = timestamp_ms + apiKey + recvWindow + (queryString | rawJsonBody)
    signature    = hex(HMAC_SHA256(secret, sign_payload))

Only category=linear (USDT perpetuals) is used.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

_async_sleep = asyncio.sleep

log = logging.getLogger(__name__)

import httpx

BASE_URL = "https://api.bybit.com"
RECV_WINDOW = "5000"
CATEGORY = "linear"

# Fallback taker fee for linear perps (Bybit VIP0). Used only when
# /v5/account/fee-rate fails — the live account rate is always tried first
# so VIP/promo accounts get accurate sizing.
TAKER_FEE_FALLBACK = Decimal("0.00055")


class BybitError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.payload = payload


def _decimals(step: str) -> int:
    exp = Decimal(step).as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def _qty_step_floor(qty: float, step: str) -> str:
    s = Decimal(step)
    q = Decimal(str(qty))
    floored = (q // s) * s
    if floored <= 0:
        floored = s
    return f"{floored:.{_decimals(step)}f}"


def _round_to_tick(price: float, tick: str) -> str:
    t = Decimal(tick)
    p = Decimal(str(price))
    rounded = (p / t).quantize(Decimal("1")) * t
    return f"{rounded:.{_decimals(tick)}f}"


class BybitFutures:
    def __init__(self, api_key: str, secret: str, timeout: float = 8.0, recv_window: str = RECV_WINDOW):
        self.api_key = api_key
        self.secret = secret.encode()
        self.recv_window = recv_window
        self.client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)

    async def aclose(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _request_time() -> str:
        return str(int(time.time() * 1000))

    def _sign(self, ts: str, payload: str) -> str:
        msg = f"{ts}{self.api_key}{self.recv_window}{payload}".encode()
        return hmac.new(self.secret, msg, hashlib.sha256).hexdigest()

    def _headers(self, ts: str, payload: str, json_body: bool) -> Dict[str, str]:
        h = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "X-BAPI-SIGN": self._sign(ts, payload),
            "X-BAPI-SIGN-TYPE": "2",
        }
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    @staticmethod
    def _check(resp_json: Dict[str, Any]) -> Dict[str, Any]:
        code = resp_json.get("retCode")
        if code not in (0, None):
            msg = resp_json.get("retMsg") or "unknown"
            raise BybitError(
                f"Bybit error {code}: {msg} | raw={resp_json}",
                code=code,
                payload=resp_json,
            )
        return resp_json

    async def _public_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = await self.client.get(path, params=params or {})
        r.raise_for_status()
        return self._check(r.json())

    async def _private_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # v5 GET sign payload uses the query string in alphabetic order, no leading '?'
        items = sorted((params or {}).items())
        qs = "&".join(f"{k}={v}" for k, v in items)
        ts = self._request_time()
        url = path + (f"?{qs}" if qs else "")
        r = await self.client.get(url, headers=self._headers(ts, qs, json_body=False))
        r.raise_for_status()
        return self._check(r.json())

    async def _private_post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        body_str = json.dumps(body, separators=(",", ":"))
        ts = self._request_time()
        r = await self.client.post(path, content=body_str, headers=self._headers(ts, body_str, json_body=True))
        r.raise_for_status()
        return self._check(r.json())

    async def instrument_info(self, symbol: str) -> Dict[str, Any]:
        data = await self._public_get("/v5/market/instruments-info", {"category": CATEGORY, "symbol": symbol})
        rows = (data.get("result") or {}).get("list") or []
        if not rows:
            raise BybitError(f"no instrument info for {symbol}")
        return rows[0]

    async def ticker(self, symbol: str) -> Dict[str, Any]:
        data = await self._public_get("/v5/market/tickers", {"category": CATEGORY, "symbol": symbol})
        rows = (data.get("result") or {}).get("list") or []
        if not rows:
            raise BybitError(f"no ticker for {symbol}")
        return rows[0]

    async def wallet_balance(self, account_type: str = "UNIFIED") -> Dict[str, Any]:
        return await self._private_get("/v5/account/wallet-balance", {"accountType": account_type})

    async def fee_rate(self, symbol: str) -> Decimal:
        """Live taker fee for `symbol` from the account's fee schedule.
        Falls back to TAKER_FEE_FALLBACK if the endpoint errors or the
        symbol is missing from the response."""
        try:
            data = await self._private_get(
                "/v5/account/fee-rate",
                {"category": CATEGORY, "symbol": symbol},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("fee_rate fetch failed for %s, using fallback: %s", symbol, e)
            return TAKER_FEE_FALLBACK
        rows = ((data.get("result") or {}).get("list") or [])
        for row in rows:
            if row.get("symbol") == symbol:
                tf = row.get("takerFeeRate")
                if tf:
                    try:
                        return Decimal(str(tf))
                    except Exception:  # noqa: BLE001
                        break
        return TAKER_FEE_FALLBACK

    async def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        body = {"category": CATEGORY, "symbol": symbol, "orderId": order_id}
        try:
            return await self._private_post("/v5/order/cancel", body)
        except BybitError as e:
            # 110001 — order not found / already filled / already cancelled.
            # Treat as success so callers can blindly cancel on close.
            if e.code in (110001,):
                return {"retCode": 0, "retMsg": "order not found", "_idempotent": True}
            raise

    async def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        """Cancel every open order for `symbol`. Used as a defensive cleanup
        before opening a new position so leftover reduceOnly Limits from a
        prior SL-fired close can't fire on the fresh trade."""
        body = {"category": CATEGORY, "symbol": symbol}
        try:
            return await self._private_post("/v5/order/cancel-all", body)
        except BybitError as e:
            log.warning("cancel_all_orders non-fatal error for %s: %s", symbol, e)
            return {"retCode": 0, "retMsg": str(e), "_swallowed": True}

    async def position_list(self, symbol: str) -> Dict[str, Any]:
        return await self._private_get("/v5/position/list", {"category": CATEGORY, "symbol": symbol})

    async def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        }
        try:
            return await self._private_post("/v5/position/set-leverage", body)
        except BybitError as e:
            # 110043 — leverage not modified; idempotent, treat as success
            if e.code == 110043:
                return {"retCode": 0, "retMsg": "leverage not modified", "_idempotent": True}
            raise

    async def switch_isolated(self, symbol: str, leverage: int, isolated: bool = True) -> Dict[str, Any]:
        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "tradeMode": 1 if isolated else 0,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        }
        try:
            return await self._private_post("/v5/position/switch-isolated", body)
        except BybitError as e:
            # 110026 — already isolated; 110028 — already cross; 110024 — UTA, mode is account-level
            if e.code in (110024, 110026, 110028):
                return {"retCode": 0, "retMsg": "mode not modifiable/already set", "_idempotent": True}
            raise

    async def submit_order(
        self,
        symbol: str,
        side: str,
        qty: str,
        order_type: str = "Market",
        price: Optional[str] = None,
        stop_loss: Optional[str] = None,
        take_profit: Optional[str] = None,
        sl_trigger_by: str = "MarkPrice",
        tp_trigger_by: str = "MarkPrice",
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": qty,
        }
        if price is not None:
            body["price"] = price
        if stop_loss is not None or take_profit is not None:
            body["tpslMode"] = "Full"
        if stop_loss is not None:
            body["stopLoss"] = stop_loss
            body["slTriggerBy"] = sl_trigger_by
            body["slOrderType"] = "Market"
        if take_profit is not None:
            body["takeProfit"] = take_profit
            body["tpTriggerBy"] = tp_trigger_by
            body["tpOrderType"] = "Market"
        if reduce_only:
            body["reduceOnly"] = True
            # GTC is required for reduceOnly Limit orders so they stay on the
            # book until fee-neutral TP price is hit (or we cancel on close).
            if order_type == "Limit":
                body["timeInForce"] = "GTC"
        return await self._private_post("/v5/order/create", body)

    async def open_long_market(
        self,
        symbol: str,
        amount_usd: float,
        leverage: int,
        stop_loss_pct: float,
        take_profit_pct: float = 0.0,
        isolated: bool = True,
        fee_neutralize: bool = False,
    ) -> Dict[str, Any]:
        """Open a market long for `amount_usd` margin at `leverage`x.

        stop_loss_pct and take_profit_pct are interpreted as percentages of
        MARGIN (i.e. with leverage applied), so price moves are divided by
        leverage. Example: leverage=50, take_profit_pct=200 → price target is
        +200/50 = +4% from entry.

        fee_neutralize=True replaces the attached take-profit with a partial
        reduceOnly Limit order that closes ~1/SAFETY of the position at a
        price designed to cover open + this TP + a future full-close fee.
        The remaining qty rides until the user's main TP (also placed as a
        reduceOnly Limit if take_profit_pct > 0), the SL, or a manual close.

        Returns a dict with: order, qty, sl_price, tp_price (main, may be
        None), entry_price, neutral_tp_price, neutral_tp_qty,
        neutral_tp_order_id, main_tp_order_id (last four are None when
        fee_neutralize is False)."""
        info = await self.instrument_info(symbol)
        tk = await self.ticker(symbol)

        lot = info.get("lotSizeFilter") or {}
        prc = info.get("priceFilter") or {}
        qty_step = str(lot.get("qtyStep") or "1")
        min_qty = str(lot.get("minOrderQty") or qty_step)
        tick_size = str(prc.get("tickSize") or "0.0001")

        last_price = float(tk.get("lastPrice") or tk.get("markPrice") or 0)
        if last_price <= 0:
            raise BybitError(f"invalid last price for {symbol}: {tk}")

        lev = max(1, int(leverage))
        # Margin → price conversion: pct/lev because pnl_pct = price_pct * leverage
        sl_price_str = _round_to_tick(
            last_price * (1.0 - (stop_loss_pct / 100.0) / lev), tick_size,
        )
        tp_price_str: Optional[str] = None
        if take_profit_pct and take_profit_pct > 0:
            tp_price_str = _round_to_tick(
                last_price * (1.0 + (take_profit_pct / 100.0) / lev), tick_size,
            )

        notional = amount_usd * lev
        full_qty = _qty_step_floor(notional / last_price, qty_step)
        if Decimal(full_qty) < Decimal(min_qty):
            full_qty = min_qty

        # Defensive cleanup: any leftover reduceOnly Limits from a prior trade
        # that closed via SL would otherwise fire on this fresh position.
        try:
            await self.cancel_all_orders(symbol)
        except Exception as e:  # noqa: BLE001
            log.warning("pre-open cancel_all_orders failed for %s: %s", symbol, e)

        try:
            await self.set_leverage(symbol, lev)
        except BybitError as e:
            log.warning("set_leverage non-fatal error: %s", e)

        if isolated:
            try:
                await self.switch_isolated(symbol, lev, isolated=True)
            except BybitError as e:
                # UTA accounts manage margin mode at account level — non-fatal here
                log.warning("switch_isolated non-fatal error (UTA cross-only?): %s", e)

        # When neutralizing, we open with SL only and place TP(s) as separate
        # reduceOnly Limits below. When not neutralizing, the main TP rides on
        # the entry order as before.
        attached_tp = None if fee_neutralize else tp_price_str
        order = await self.submit_order(
            symbol=symbol,
            side="Buy",
            qty=full_qty,
            order_type="Market",
            stop_loss=sl_price_str,
            take_profit=attached_tp,
            sl_trigger_by="MarkPrice",
            tp_trigger_by="MarkPrice",
        )

        # Read back the resulting position to capture the actual fill price.
        # Bybit fills market orders nearly synchronously, so a small retry
        # window (≤500ms) covers the case where position-list lags the ack.
        entry_price = str(last_price)
        actual_qty = full_qty
        for _ in range(5):
            pos = await self.get_open_position(symbol)
            if pos and Decimal(str(pos.get("size") or "0")) > 0:
                entry_price = str(pos.get("avgPrice") or last_price)
                actual_qty = str(pos.get("size") or full_qty)
                break
            await _async_sleep(0.1)

        result: Dict[str, Any] = {
            "order": order,
            "qty": actual_qty,
            "sl_price": sl_price_str,
            "tp_price": tp_price_str,
            "entry_price": entry_price,
            "neutral_tp_price": None,
            "neutral_tp_qty": None,
            "neutral_tp_order_id": None,
            "main_tp_order_id": None,
        }

        if not fee_neutralize:
            return result

        # Fee-neutralize: place partial reduceOnly Limit at the price where
        # the unrealized PnL on the full position equals 3× open-fee, then
        # close exactly the fraction whose pre-trade equity ($M_partial)
        # equals 3× open-fee — leaving the remaining position with equity
        # equal to the original margin ($M).
        #
        # Derivation (M = margin, L = leverage, f = fee_rate, N = M·L):
        #   3·f_usd = 3·N·f                                 (total est. fees)
        #   TP price: E·(1 + 3·f)                  ← 3·f_usd unrealized at TP
        #   X (partial fraction) = 3·f_usd / (M + 3·f_usd)
        #                        = 3·L·f / (1 + 3·L·f)
        # Leverage cancels out of the price multiplier; it only affects X.
        fee = await self.fee_rate(symbol)
        entry_dec = Decimal(entry_price)
        full_qty_dec = Decimal(actual_qty)

        neutral_price_mult = Decimal(1) + Decimal(3) * fee
        neutral_price_str = _round_to_tick(
            float(entry_dec * neutral_price_mult), tick_size,
        )

        three_l_f = Decimal(3) * Decimal(lev) * fee
        x_fraction = three_l_f / (Decimal(1) + three_l_f)
        partial_qty_raw = full_qty_dec * x_fraction
        partial_qty_str = _qty_step_floor(float(partial_qty_raw), qty_step)
        if Decimal(partial_qty_str) >= full_qty_dec:
            # Step rounding pushed partial up to the whole position; back off
            # one step so a remainder exists for the main TP / SL.
            partial_qty_str = _qty_step_floor(
                float(full_qty_dec - Decimal(qty_step)), qty_step,
            )
        if Decimal(partial_qty_str) < Decimal(min_qty):
            partial_qty_str = min_qty

        neutral_order = await self.submit_order(
            symbol=symbol,
            side="Sell",
            qty=partial_qty_str,
            order_type="Limit",
            price=neutral_price_str,
            reduce_only=True,
        )
        result["neutral_tp_price"] = neutral_price_str
        result["neutral_tp_qty"] = partial_qty_str
        result["neutral_tp_order_id"] = (
            (neutral_order.get("result") or {}).get("orderId")
        )

        if tp_price_str:
            remaining_qty = full_qty_dec - Decimal(partial_qty_str)
            remaining_str = _qty_step_floor(float(remaining_qty), qty_step)
            if Decimal(remaining_str) >= Decimal(min_qty):
                main_order = await self.submit_order(
                    symbol=symbol,
                    side="Sell",
                    qty=remaining_str,
                    order_type="Limit",
                    price=tp_price_str,
                    reduce_only=True,
                )
                result["main_tp_order_id"] = (
                    (main_order.get("result") or {}).get("orderId")
                )

        return result

    async def get_open_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return the first non-zero position for `symbol`, or None."""
        res = await self.position_list(symbol)
        rows = ((res.get("result") or {}).get("list") or [])
        for r in rows:
            try:
                size = Decimal(str(r.get("size") or "0"))
            except Exception:
                size = Decimal(0)
            if size > 0 and r.get("symbol") == symbol:
                return r
        return None

    async def close_position_market(
        self,
        symbol: str,
        qty: str,
        position_side: str = "Buy",
    ) -> Dict[str, Any]:
        """Market-close a position with reduceOnly. position_side is the side
        of the OPEN position ("Buy"=long, "Sell"=short)."""
        body = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": "Sell" if position_side == "Buy" else "Buy",
            "orderType": "Market",
            "qty": qty,
            "reduceOnly": True,
        }
        return await self._private_post("/v5/order/create", body)

    async def gather_diagnostics(self, symbol: str) -> Dict[str, Any]:
        """Collect everything useful for debugging a trade failure."""
        diag: Dict[str, Any] = {"symbol": symbol}

        async def safe(key: str, coro):
            try:
                diag[key] = await coro
            except BybitError as e:
                diag[key] = {"_error": str(e), "_code": e.code}
            except Exception as e:  # noqa: BLE001
                diag[key] = {"_error": f"{type(e).__name__}: {e}"}

        await safe("instrument_info", self.instrument_info(symbol))
        await safe("ticker", self.ticker(symbol))
        await safe("wallet_unified", self.wallet_balance("UNIFIED"))
        await safe("wallet_contract", self.wallet_balance("CONTRACT"))
        await safe("position_list", self.position_list(symbol))
        return diag


def _fmt(v: Any) -> str:
    return html.escape(str(v)) if v is not None else "—"


def _find_usdt(wallet_resp: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(wallet_resp, dict) or "_error" in wallet_resp:
        return None
    rows = ((wallet_resp.get("result") or {}).get("list") or [])
    for row in rows:
        for coin in row.get("coin", []) or []:
            if str(coin.get("coin", "")).upper() == "USDT":
                return coin
    return None


def format_diagnostics_html(diag: Dict[str, Any]) -> str:
    symbol = diag.get("symbol", "?")
    lines: List[str] = [f"🔍 <b>Диагностика Bybit · {_fmt(symbol)}</b>"]

    info = diag.get("instrument_info") or {}
    if isinstance(info, dict) and "_error" not in info:
        lf = info.get("leverageFilter") or {}
        lot = info.get("lotSizeFilter") or {}
        prc = info.get("priceFilter") or {}
        lines.append(
            f"• Instrument: status=<b>{_fmt(info.get('status'))}</b> "
            f"maxLev=<b>{_fmt(lf.get('maxLeverage'))}</b> "
            f"minLev={_fmt(lf.get('minLeverage'))}"
        )
        lines.append(
            f"  qtyStep={_fmt(lot.get('qtyStep'))} "
            f"minQty={_fmt(lot.get('minOrderQty'))} "
            f"tickSize={_fmt(prc.get('tickSize'))}"
        )
    else:
        lines.append(f"• Instrument: ошибка — <code>{_fmt(info.get('_error'))}</code>")

    tk = diag.get("ticker") or {}
    if isinstance(tk, dict) and "_error" not in tk:
        lines.append(
            f"• Ticker: last={_fmt(tk.get('lastPrice'))} mark={_fmt(tk.get('markPrice'))}"
        )
    else:
        lines.append(f"• Ticker: ошибка — <code>{_fmt(tk.get('_error'))}</code>")

    wu = diag.get("wallet_unified") or {}
    wc = diag.get("wallet_contract") or {}
    usdt_unified = _find_usdt(wu)
    usdt_contract = _find_usdt(wc)
    usdt = usdt_unified or usdt_contract
    if usdt:
        kind = "UNIFIED" if usdt_unified else "CONTRACT"
        lines.append(
            f"• USDT ({kind}): wallet={_fmt(usdt.get('walletBalance'))} "
            f"avail={_fmt(usdt.get('availableToWithdraw') or usdt.get('availableBalance'))} "
            f"equity={_fmt(usdt.get('equity'))}"
        )
    else:
        u_err = wu.get("_error") if isinstance(wu, dict) else None
        c_err = wc.get("_error") if isinstance(wc, dict) else None
        lines.append(
            f"• USDT: запись не найдена. UNIFIED: <code>{_fmt(u_err)}</code> "
            f"CONTRACT: <code>{_fmt(c_err)}</code>"
        )

    pl = diag.get("position_list") or {}
    if isinstance(pl, dict) and "_error" not in pl:
        rows = ((pl.get("result") or {}).get("list") or [])
        if not rows:
            lines.append("• Position: нет открытых позиций")
        else:
            for r in rows:
                lines.append(
                    f"• Position [{_fmt(r.get('side'))}]: "
                    f"size={_fmt(r.get('size'))} "
                    f"avgPrice={_fmt(r.get('avgPrice'))} "
                    f"lev={_fmt(r.get('leverage'))} "
                    f"mode={_fmt(r.get('tradeMode'))}"
                )
    else:
        lines.append(f"• Position list: ошибка — <code>{_fmt(pl.get('_error'))}</code>")

    return "\n".join(lines)

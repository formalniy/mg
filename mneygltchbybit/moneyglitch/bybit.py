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
        sl_trigger_by: str = "MarkPrice",
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
        if stop_loss is not None:
            body["stopLoss"] = stop_loss
            body["slTriggerBy"] = sl_trigger_by
            body["slOrderType"] = "Market"
            body["tpslmode"] = "Full"
        return await self._private_post("/v5/order/create", body)

    async def open_long_market(
        self,
        symbol: str,
        amount_usd: float,
        leverage: int,
        stop_loss_pct: float,
        isolated: bool = True,
    ) -> Dict[str, Any]:
        """Open a market long for `amount_usd` margin at `leverage`x with a
        percentage stop-loss below mark price. qty is sized in base currency.

        Returns a dict with: order (raw order create response), qty, sl_price,
        entry_price (filled avgPrice from position list, falls back to last
        ticker if the position isn't visible yet)."""
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

        notional = amount_usd * leverage
        qty = _qty_step_floor(notional / last_price, qty_step)
        if Decimal(qty) < Decimal(min_qty):
            qty = min_qty

        sl_price_str = _round_to_tick(last_price * (1.0 - stop_loss_pct / 100.0), tick_size)

        try:
            await self.set_leverage(symbol, leverage)
        except BybitError as e:
            log.warning("set_leverage non-fatal error: %s", e)

        if isolated:
            try:
                await self.switch_isolated(symbol, leverage, isolated=True)
            except BybitError as e:
                # UTA accounts manage margin mode at account level — non-fatal here
                log.warning("switch_isolated non-fatal error (UTA cross-only?): %s", e)

        order = await self.submit_order(
            symbol=symbol,
            side="Buy",
            qty=qty,
            order_type="Market",
            stop_loss=sl_price_str,
            sl_trigger_by="MarkPrice",
        )

        # Read back the resulting position to capture the actual fill price.
        # Bybit fills market orders nearly synchronously, so a small retry
        # window (≤500ms) covers the case where position-list lags the ack.
        entry_price = str(last_price)
        actual_qty = qty
        for _ in range(5):
            pos = await self.get_open_position(symbol)
            if pos and Decimal(str(pos.get("size") or "0")) > 0:
                entry_price = str(pos.get("avgPrice") or last_price)
                actual_qty = str(pos.get("size") or qty)
                break
            await _async_sleep(0.1)

        return {
            "order": order,
            "qty": actual_qty,
            "sl_price": sl_price_str,
            "entry_price": entry_price,
        }

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

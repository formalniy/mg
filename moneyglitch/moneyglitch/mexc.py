"""Minimal MEXC v1 perpetual futures (Contract) client.

Uses contract.mexc.com endpoints with HMAC-SHA256 signing as documented at
https://mexcdevelop.github.io/apidocs/contract_v1_en/

Sign rule (v1):
    sign_payload = apiKey + request_time_ms + (sorted_query_string | rawJsonBody)
    signature    = hex(HMAC_SHA256(secret, sign_payload))

Headers required on every private call:
    ApiKey:       <api key>
    Request-Time: <unix ms>
    Signature:    <hex hmac>
    Content-Type: application/json    (POST only)

USDT-margined linear perpetuals only. Symbol format is `TONCOIN_USDT`
(MEXC's API name for the TON Open Network futures contract; displayed in
the web UI as "TON_USDT"). Volume is an INTEGER number of contracts;
each contract represents `contractSize` units of the base asset
(see /api/v1/contract/detail).
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

BASE_URL = "https://contract.mexc.com"

# Fallback taker fee for linear perps (MEXC default). Used only when
# /api/v1/contract/detail does not return a takerFeeRate for the symbol.
TAKER_FEE_FALLBACK = Decimal("0.0005")

# Order sides (MEXC futures encoding):
SIDE_OPEN_LONG = 1
SIDE_CLOSE_SHORT = 2
SIDE_OPEN_SHORT = 3
SIDE_CLOSE_LONG = 4

# Order types:
ORDER_TYPE_LIMIT = 1
ORDER_TYPE_MARKET = 5

# Open types (margin mode per position):
OPEN_TYPE_ISOLATED = 1
OPEN_TYPE_CROSS = 2

# Position types (long/short on the same symbol — MEXC supports hedge mode):
POSITION_TYPE_LONG = 1
POSITION_TYPE_SHORT = 2


class MEXCError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.payload = payload


def _decimals(step: str) -> int:
    exp = Decimal(step).as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def _vol_floor(contracts: float) -> str:
    """MEXC futures vol is an integer number of contracts."""
    v = int(Decimal(str(contracts)))
    if v <= 0:
        v = 1
    return str(v)


def _round_to_tick(price: float, tick: str) -> str:
    t = Decimal(tick)
    p = Decimal(str(price))
    rounded = (p / t).quantize(Decimal("1")) * t
    return f"{rounded:.{_decimals(tick)}f}"


class MEXCFutures:
    def __init__(self, api_key: str, secret: str, timeout: float = 8.0):
        self.api_key = api_key
        self.secret = secret.encode()
        self.client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)

    async def aclose(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _request_time() -> str:
        return str(int(time.time() * 1000))

    def _sign(self, ts: str, payload: str) -> str:
        msg = f"{self.api_key}{ts}{payload}".encode()
        return hmac.new(self.secret, msg, hashlib.sha256).hexdigest()

    def _headers(self, ts: str, payload: str, json_body: bool) -> Dict[str, str]:
        h = {
            "ApiKey": self.api_key,
            "Request-Time": ts,
            "Signature": self._sign(ts, payload),
        }
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    @staticmethod
    def _check(resp_json: Dict[str, Any]) -> Dict[str, Any]:
        code = resp_json.get("code")
        ok = resp_json.get("success")
        if (code not in (0, None)) or (ok is False):
            msg = resp_json.get("message") or resp_json.get("msg") or "unknown"
            raise MEXCError(
                f"MEXC error {code}: {msg} | raw={resp_json}",
                code=code,
                payload=resp_json,
            )
        return resp_json

    async def _public_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = await self.client.get(path, params=params or {})
        r.raise_for_status()
        return self._check(r.json())

    async def _private_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # v1 private GET sign payload: sorted query string, no leading '?'
        items = sorted((params or {}).items())
        qs = "&".join(f"{k}={v}" for k, v in items)
        ts = self._request_time()
        url = path + (f"?{qs}" if qs else "")
        r = await self.client.get(url, headers=self._headers(ts, qs, json_body=False))
        r.raise_for_status()
        return self._check(r.json())

    async def _private_post(self, path: str, body: Any) -> Dict[str, Any]:
        body_str = json.dumps(body, separators=(",", ":"))
        ts = self._request_time()
        r = await self.client.post(path, content=body_str, headers=self._headers(ts, body_str, json_body=True))
        r.raise_for_status()
        return self._check(r.json())

    async def instrument_info(self, symbol: str) -> Dict[str, Any]:
        """One contract detail entry. Fields used: contractSize, minVol,
        maxVol, priceUnit (tick), volUnit, maxLeverage, takerFeeRate."""
        data = await self._public_get("/api/v1/contract/detail", {"symbol": symbol})
        rows = data.get("data") or []
        if isinstance(rows, dict):  # some deployments return single object
            return rows
        if not rows:
            raise MEXCError(f"no instrument info for {symbol}")
        for row in rows:
            if row.get("symbol") == symbol:
                return row
        return rows[0]

    async def ticker(self, symbol: str) -> Dict[str, Any]:
        data = await self._public_get("/api/v1/contract/ticker", {"symbol": symbol})
        row = data.get("data") or {}
        if isinstance(row, list):
            row = (row[0] if row else {})
        if not row:
            raise MEXCError(f"no ticker for {symbol}")
        return row

    async def wallet_balance(self, currency: str = "USDT") -> Dict[str, Any]:
        """Single-currency asset record. Mirrors the bybit-version wrapper
        so the diagnostic / balance UI keeps the same call shape."""
        return await self._private_get(f"/api/v1/private/account/asset/{currency}")

    async def fee_rate(self, symbol: str) -> Decimal:
        """Live taker fee for `symbol`. MEXC exposes takerFeeRate on the
        public contract detail, so no private call is needed; falls back to
        TAKER_FEE_FALLBACK if the field is missing or malformed."""
        try:
            info = await self.instrument_info(symbol)
        except Exception as e:  # noqa: BLE001
            log.warning("fee_rate fetch failed for %s, using fallback: %s", symbol, e)
            return TAKER_FEE_FALLBACK
        tf = info.get("takerFeeRate")
        if tf is None:
            return TAKER_FEE_FALLBACK
        try:
            return Decimal(str(tf))
        except Exception:  # noqa: BLE001
            return TAKER_FEE_FALLBACK

    async def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        # MEXC's cancel endpoint takes a JSON array of orderIds and cancels
        # across all symbols the caller has access to.
        try:
            return await self._private_post("/api/v1/private/order/cancel", [str(order_id)])
        except MEXCError as e:
            # order not found / already filled / already cancelled — non-fatal
            if e.code in (2027, 2032, 2033):
                return {"code": 0, "message": "order not found", "_idempotent": True}
            raise

    async def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        """Cancel every open order for `symbol`. Defensive cleanup before
        opening a new position so leftover close-side Limits from a prior
        SL-fired close can't fire on the fresh trade."""
        body = {"symbol": symbol}
        try:
            return await self._private_post("/api/v1/private/order/cancel_all", body)
        except MEXCError as e:
            log.warning("cancel_all_orders non-fatal error for %s: %s", symbol, e)
            return {"code": 0, "message": str(e), "_swallowed": True}

    async def position_list(self, symbol: str) -> Dict[str, Any]:
        return await self._private_get(
            "/api/v1/private/position/open_positions", {"symbol": symbol},
        )

    async def submit_order(
        self,
        symbol: str,
        side: int,
        vol: str,
        leverage: int,
        open_type: int = OPEN_TYPE_ISOLATED,
        order_type: int = ORDER_TYPE_MARKET,
        price: Optional[str] = None,
        stop_loss_price: Optional[str] = None,
        take_profit_price: Optional[str] = None,
        position_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "symbol": symbol,
            "vol": str(vol),
            "leverage": int(leverage),
            "side": int(side),
            "type": int(order_type),
            "openType": int(open_type),
        }
        if price is not None:
            body["price"] = str(price)
        if stop_loss_price is not None:
            body["stopLossPrice"] = str(stop_loss_price)
        if take_profit_price is not None:
            body["takeProfitPrice"] = str(take_profit_price)
        if position_id is not None:
            body["positionId"] = int(position_id)
        return await self._private_post("/api/v1/private/order/submit", body)

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
        close-side Limit order that closes ~X of the position at a price
        designed to cover open + this TP + a future full-close fee. The
        remaining vol rides until the user's main TP (also placed as a
        close-side Limit if take_profit_pct > 0), the SL, or a manual close.

        Returns a dict with: order, qty (in TON), vol (in contracts),
        sl_price, tp_price (main, may be None), entry_price, contract_size,
        neutral_tp_price, neutral_tp_qty, neutral_tp_vol,
        neutral_tp_order_id, main_tp_order_id (last five are None when
        fee_neutralize is False)."""
        info = await self.instrument_info(symbol)
        tk = await self.ticker(symbol)

        contract_size = Decimal(str(info.get("contractSize") or "1"))
        min_vol = int(info.get("minVol") or 1)
        tick_size = str(info.get("priceUnit") or "0.0001")

        last_price = float(tk.get("lastPrice") or tk.get("fairPrice") or 0)
        if last_price <= 0:
            raise MEXCError(f"invalid last price for {symbol}: {tk}")

        lev = max(1, int(leverage))
        sl_price_str = _round_to_tick(
            last_price * (1.0 - (stop_loss_pct / 100.0) / lev), tick_size,
        )
        tp_price_str: Optional[str] = None
        if take_profit_pct and take_profit_pct > 0:
            tp_price_str = _round_to_tick(
                last_price * (1.0 + (take_profit_pct / 100.0) / lev), tick_size,
            )

        # MEXC vol is integer contracts; each contract = `contractSize` of base.
        notional = Decimal(str(amount_usd)) * Decimal(lev)
        per_contract_usd = Decimal(str(last_price)) * contract_size
        if per_contract_usd <= 0:
            raise MEXCError(f"invalid contract notional for {symbol}: price={last_price} cs={contract_size}")
        contracts_raw = notional / per_contract_usd
        vol_int = int(contracts_raw)
        if vol_int < min_vol:
            vol_int = min_vol
        full_vol = str(vol_int)

        # Defensive cleanup: any leftover close-side Limits from a prior trade
        # that closed via SL would otherwise fire on this fresh position.
        try:
            await self.cancel_all_orders(symbol)
        except Exception as e:  # noqa: BLE001
            log.warning("pre-open cancel_all_orders failed for %s: %s", symbol, e)

        attached_tp = None if fee_neutralize else tp_price_str
        order = await self.submit_order(
            symbol=symbol,
            side=SIDE_OPEN_LONG,
            vol=full_vol,
            leverage=lev,
            open_type=OPEN_TYPE_ISOLATED if isolated else OPEN_TYPE_CROSS,
            order_type=ORDER_TYPE_MARKET,
            stop_loss_price=sl_price_str,
            take_profit_price=attached_tp,
        )

        # Read back the position. MEXC settles market orders quickly but
        # position-list can lag the ack — small retry window covers that.
        entry_price = str(last_price)
        actual_vol = full_vol
        position_id: Optional[int] = None
        for _ in range(5):
            pos = await self.get_open_position(symbol)
            if pos and int(pos.get("holdVol") or 0) > 0:
                entry_price = str(pos.get("holdAvgPrice") or pos.get("openAvgPrice") or last_price)
                actual_vol = str(pos.get("holdVol") or full_vol)
                pid = pos.get("positionId")
                if pid is not None:
                    try:
                        position_id = int(pid)
                    except Exception:  # noqa: BLE001
                        position_id = None
                break
            await _async_sleep(0.1)

        actual_qty = str(Decimal(actual_vol) * contract_size)

        result: Dict[str, Any] = {
            "order": order,
            "vol": actual_vol,
            "qty": actual_qty,
            "contract_size": str(contract_size),
            "sl_price": sl_price_str,
            "tp_price": tp_price_str,
            "entry_price": entry_price,
            "position_id": position_id,
            "neutral_tp_price": None,
            "neutral_tp_qty": None,
            "neutral_tp_vol": None,
            "neutral_tp_order_id": None,
            "main_tp_order_id": None,
        }

        if not fee_neutralize:
            return result

        # Fee-neutralize: place a partial close-side Limit at the price where
        # the unrealized PnL on the full position equals 3× open-fee, sized so
        # the closed portion banks exactly 3× the round-trip taker fee.
        fee = await self.fee_rate(symbol)
        entry_dec = Decimal(entry_price)
        full_vol_dec = Decimal(actual_vol)

        neutral_price_mult = Decimal(1) + Decimal(3) * fee
        neutral_price_str = _round_to_tick(
            float(entry_dec * neutral_price_mult), tick_size,
        )

        three_l_f = Decimal(3) * Decimal(lev) * fee
        x_fraction = three_l_f / (Decimal(1) + three_l_f)
        partial_raw = full_vol_dec * x_fraction
        partial_vol_int = int(partial_raw)
        if partial_vol_int < 1:
            partial_vol_int = 1
        if partial_vol_int >= int(full_vol_dec):
            # leave at least 1 contract for the remainder
            partial_vol_int = int(full_vol_dec) - 1
            if partial_vol_int < 1:
                partial_vol_int = 1
        if partial_vol_int < min_vol:
            partial_vol_int = min_vol
        partial_vol_str = str(partial_vol_int)
        partial_qty_str = str(Decimal(partial_vol_int) * contract_size)

        neutral_order = await self.submit_order(
            symbol=symbol,
            side=SIDE_CLOSE_LONG,
            vol=partial_vol_str,
            leverage=lev,
            open_type=OPEN_TYPE_ISOLATED if isolated else OPEN_TYPE_CROSS,
            order_type=ORDER_TYPE_LIMIT,
            price=neutral_price_str,
            position_id=position_id,
        )
        result["neutral_tp_price"] = neutral_price_str
        result["neutral_tp_qty"] = partial_qty_str
        result["neutral_tp_vol"] = partial_vol_str
        result["neutral_tp_order_id"] = neutral_order.get("data")

        if tp_price_str:
            remaining_vol = int(full_vol_dec) - partial_vol_int
            if remaining_vol >= min_vol:
                main_order = await self.submit_order(
                    symbol=symbol,
                    side=SIDE_CLOSE_LONG,
                    vol=str(remaining_vol),
                    leverage=lev,
                    open_type=OPEN_TYPE_ISOLATED if isolated else OPEN_TYPE_CROSS,
                    order_type=ORDER_TYPE_LIMIT,
                    price=tp_price_str,
                    position_id=position_id,
                )
                result["main_tp_order_id"] = main_order.get("data")

        return result

    async def get_open_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return the first non-zero LONG position for `symbol`, or None.

        Translates MEXC's holdVol/holdAvgPrice into Bybit-style `size`/
        `avgPrice` / `side` fields so the rest of the codebase can stay
        exchange-agnostic.
        """
        res = await self.position_list(symbol)
        rows = res.get("data") or []
        for r in rows:
            if r.get("symbol") != symbol:
                continue
            try:
                hold_vol = int(r.get("holdVol") or 0)
            except (TypeError, ValueError):
                hold_vol = 0
            if hold_vol <= 0:
                continue
            ptype = int(r.get("positionType") or 0)
            side = "Buy" if ptype == POSITION_TYPE_LONG else "Sell"
            avg = r.get("holdAvgPrice") or r.get("openAvgPrice")
            return {
                **r,
                "size": str(hold_vol),
                "avgPrice": str(avg) if avg is not None else None,
                "side": side,
            }
        return None

    async def close_position_market(
        self,
        symbol: str,
        vol: str,
        position_side: str = "Buy",
        leverage: int = 10,
        open_type: int = OPEN_TYPE_ISOLATED,
        position_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Market-close a position. position_side is the side of the OPEN
        position ("Buy"=long → side=4 close-long; "Sell"=short → side=2)."""
        side = SIDE_CLOSE_LONG if position_side == "Buy" else SIDE_CLOSE_SHORT
        return await self.submit_order(
            symbol=symbol,
            side=side,
            vol=str(vol),
            leverage=leverage,
            open_type=open_type,
            order_type=ORDER_TYPE_MARKET,
            position_id=position_id,
        )

    async def gather_diagnostics(self, symbol: str) -> Dict[str, Any]:
        """Collect everything useful for debugging a trade failure."""
        diag: Dict[str, Any] = {"symbol": symbol}

        async def safe(key: str, coro):
            try:
                diag[key] = await coro
            except MEXCError as e:
                diag[key] = {"_error": str(e), "_code": e.code}
            except Exception as e:  # noqa: BLE001
                diag[key] = {"_error": f"{type(e).__name__}: {e}"}

        await safe("instrument_info", self.instrument_info(symbol))
        await safe("ticker", self.ticker(symbol))
        await safe("wallet_usdt", self.wallet_balance("USDT"))
        await safe("position_list", self.position_list(symbol))
        return diag


def _fmt(v: Any) -> str:
    return html.escape(str(v)) if v is not None else "—"


def _find_usdt(wallet_resp: Any) -> Optional[Dict[str, Any]]:
    """MEXC returns a flat asset dict (no nested 'coin' list like Bybit).
    Returned shape exposes the same keys the bot's _extract_usdt uses
    (walletBalance / availableBalance / equity)."""
    if not isinstance(wallet_resp, dict) or "_error" in wallet_resp:
        return None
    row = wallet_resp.get("data") or {}
    if not isinstance(row, dict) or not row:
        return None
    if str(row.get("currency", "")).upper() != "USDT":
        return None
    return {
        "coin": "USDT",
        "walletBalance": row.get("cashBalance"),
        "availableBalance": row.get("availableBalance"),
        "availableToWithdraw": row.get("availableBalance"),
        "equity": row.get("equity"),
        "unrealized": row.get("unrealized"),
        "positionMargin": row.get("positionMargin"),
    }


def format_diagnostics_html(diag: Dict[str, Any]) -> str:
    symbol = diag.get("symbol", "?")
    lines: List[str] = [f"🔍 <b>Диагностика MEXC · {_fmt(symbol)}</b>"]

    info = diag.get("instrument_info") or {}
    if isinstance(info, dict) and "_error" not in info:
        lines.append(
            f"• Instrument: state=<b>{_fmt(info.get('state'))}</b> "
            f"maxLev=<b>{_fmt(info.get('maxLeverage'))}</b> "
            f"minLev={_fmt(info.get('minLeverage'))}"
        )
        lines.append(
            f"  contractSize={_fmt(info.get('contractSize'))} "
            f"minVol={_fmt(info.get('minVol'))} "
            f"tick={_fmt(info.get('priceUnit'))} "
            f"taker={_fmt(info.get('takerFeeRate'))}"
        )
    else:
        lines.append(f"• Instrument: ошибка — <code>{_fmt(info.get('_error'))}</code>")

    tk = diag.get("ticker") or {}
    if isinstance(tk, dict) and "_error" not in tk:
        lines.append(
            f"• Ticker: last={_fmt(tk.get('lastPrice'))} fair={_fmt(tk.get('fairPrice'))}"
        )
    else:
        lines.append(f"• Ticker: ошибка — <code>{_fmt(tk.get('_error'))}</code>")

    wu = diag.get("wallet_usdt") or {}
    usdt = _find_usdt(wu)
    if usdt:
        lines.append(
            f"• USDT: wallet={_fmt(usdt.get('walletBalance'))} "
            f"avail={_fmt(usdt.get('availableBalance'))} "
            f"equity={_fmt(usdt.get('equity'))}"
        )
    else:
        err = wu.get("_error") if isinstance(wu, dict) else None
        lines.append(f"• USDT: запись не найдена. <code>{_fmt(err)}</code>")

    pl = diag.get("position_list") or {}
    if isinstance(pl, dict) and "_error" not in pl:
        rows = pl.get("data") or []
        if not rows:
            lines.append("• Position: нет открытых позиций")
        else:
            for r in rows:
                ptype = r.get("positionType")
                side = "Long" if ptype == POSITION_TYPE_LONG else ("Short" if ptype == POSITION_TYPE_SHORT else "?")
                otype = r.get("openType")
                mode = "isolated" if otype == OPEN_TYPE_ISOLATED else ("cross" if otype == OPEN_TYPE_CROSS else "?")
                lines.append(
                    f"• Position [{side}]: "
                    f"vol={_fmt(r.get('holdVol'))} "
                    f"avgPrice={_fmt(r.get('holdAvgPrice') or r.get('openAvgPrice'))} "
                    f"lev={_fmt(r.get('leverage'))} "
                    f"mode={mode}"
                )
    else:
        lines.append(f"• Position list: ошибка — <code>{_fmt(pl.get('_error'))}</code>")

    return "\n".join(lines)

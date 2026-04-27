"""Minimal MEXC perpetual futures client.

Uses contract.mexc.com endpoints with HMAC-SHA256 signing as documented at
https://mexcdevelop.github.io/apidocs/contract_v1_en/

Sign rule:
    sign_payload = api_key + request_time_ms + (queryString | jsonBody)
    signature    = hex( HMAC_SHA256(secret, sign_payload) )

Note on rate limits: MEXC contract trading endpoints allow ~20 requests / 2s
per account. Our flow uses 4 calls per trigger (detail, ticker, leverage,
order), so we are far below the cap.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from typing import Any, Dict, Optional

import httpx

BASE_URL = "https://contract.mexc.com"

SIDE_OPEN_LONG = 1
ORDER_TYPE_MARKET = 5
OPEN_TYPE_ISOLATED = 1
OPEN_TYPE_CROSS = 2
POSITION_TYPE_LONG = 1


class MexcError(RuntimeError):
    pass


class MexcFutures:
    def __init__(self, api_key: str, secret: str, timeout: float = 8.0):
        self.api_key = api_key
        self.secret = secret.encode()
        self.client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)

    async def aclose(self) -> None:
        await self.client.aclose()

    def _sign(self, request_time: str, payload: str) -> str:
        msg = f"{self.api_key}{request_time}{payload}".encode()
        return hmac.new(self.secret, msg, hashlib.sha256).hexdigest()

    def _headers(self, request_time: str, payload: str) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "ApiKey": self.api_key,
            "Request-Time": request_time,
            "Signature": self._sign(request_time, payload),
        }

    @staticmethod
    def _request_time() -> str:
        return str(int(time.time() * 1000))

    @staticmethod
    def _check(resp_json: Dict[str, Any]) -> Dict[str, Any]:
        if resp_json.get("success") is False or resp_json.get("code") not in (0, 200, None):
            raise MexcError(f"MEXC error: {resp_json}")
        return resp_json

    async def _public_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = await self.client.get(path, params=params or {})
        r.raise_for_status()
        return self._check(r.json())

    async def _private_post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        body_str = json.dumps(body, separators=(",", ":"), sort_keys=True)
        rt = self._request_time()
        r = await self.client.post(path, content=body_str, headers=self._headers(rt, body_str))
        r.raise_for_status()
        return self._check(r.json())

    async def contract_detail(self, symbol: str) -> Dict[str, Any]:
        data = await self._public_get("/api/v1/contract/detail", {"symbol": symbol})
        d = data.get("data")
        if isinstance(d, list):
            d = next((x for x in d if x.get("symbol") == symbol), d[0] if d else {})
        if not d:
            raise MexcError(f"no contract detail for {symbol}")
        return d

    async def ticker(self, symbol: str) -> Dict[str, Any]:
        data = await self._public_get("/api/v1/contract/ticker", {"symbol": symbol})
        d = data.get("data")
        if isinstance(d, list):
            d = next((x for x in d if x.get("symbol") == symbol), d[0] if d else {})
        if not d:
            raise MexcError(f"no ticker for {symbol}")
        return d

    async def change_leverage(
        self,
        symbol: str,
        leverage: int,
        open_type: int = OPEN_TYPE_ISOLATED,
        position_type: int = POSITION_TYPE_LONG,
    ) -> Dict[str, Any]:
        body = {
            "leverage": int(leverage),
            "openType": int(open_type),
            "positionType": int(position_type),
            "symbol": symbol,
        }
        return await self._private_post("/api/v1/private/position/change_leverage", body)

    async def submit_order(
        self,
        symbol: str,
        vol: int,
        leverage: int,
        side: int,
        order_type: int,
        open_type: int,
        price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "symbol": symbol,
            "vol": int(vol),
            "leverage": int(leverage),
            "side": int(side),
            "type": int(order_type),
            "openType": int(open_type),
        }
        if price is not None:
            body["price"] = float(price)
        if stop_loss_price is not None:
            body["stopLossPrice"] = float(stop_loss_price)
        return await self._private_post("/api/v1/private/order/submit", body)

    async def open_long_market(
        self,
        symbol: str,
        amount_usd: float,
        leverage: int,
        stop_loss_pct: float,
        open_type: int = OPEN_TYPE_ISOLATED,
    ) -> Dict[str, Any]:
        """Open a market long for `amount_usd` margin at `leverage`x with a
        percentage stop-loss below mark price."""
        detail = await self.contract_detail(symbol)
        ticker = await self.ticker(symbol)

        contract_size = float(detail.get("contractSize") or 1)
        price_scale = int(detail.get("priceScale") or 4)
        last_price = float(ticker.get("lastPrice") or ticker.get("fairPrice"))
        if last_price <= 0:
            raise MexcError(f"invalid last price for {symbol}")

        notional = amount_usd * leverage
        vol = int(math.floor(notional / (last_price * contract_size)))
        if vol < 1:
            vol = 1

        sl_price = round(last_price * (1.0 - stop_loss_pct / 100.0), price_scale)

        await self.change_leverage(
            symbol=symbol,
            leverage=leverage,
            open_type=open_type,
            position_type=POSITION_TYPE_LONG,
        )
        return await self.submit_order(
            symbol=symbol,
            vol=vol,
            leverage=leverage,
            side=SIDE_OPEN_LONG,
            order_type=ORDER_TYPE_MARKET,
            open_type=open_type,
            stop_loss_price=sl_price,
        )

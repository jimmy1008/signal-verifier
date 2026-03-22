"""
OANDA Broker 實作

職責：
- 透過 OANDA v20 REST API 執行交易
- 下單 / 改單 / 平倉 / 查詢持倉
- 信號用的是 OANDA 報價，直接對接最準確

需要：
- OANDA 帳戶（Demo 或 Live）
- API Token（從 OANDA 後台取得）

OANDA Symbol 格式：NAS100_USD, XAU_USD, EUR_JPY 等
CRT SNIPER 格式：NAS100USD, XAUUSD, EURJPY 等
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from src.models import SignalSide
from src.trader.base import BaseBroker, OrderResult, Position, AccountInfo

logger = logging.getLogger(__name__)


# CRT SNIPER symbol → OANDA symbol
OANDA_SYMBOL_MAP = {
    "NAS100USD": "NAS100_USD",
    "XAUUSD": "XAU_USD",
    "XAGUSD": "XAG_USD",
    "EURUSD": "EUR_USD",
    "GBPUSD": "GBP_USD",
    "USDJPY": "USD_JPY",
    "EURJPY": "EUR_JPY",
    "GBPJPY": "GBP_JPY",
    "AUDUSD": "AUD_USD",
    "NZDUSD": "NZD_USD",
    "USDCHF": "USD_CHF",
    "USDCAD": "USD_CAD",
}


def to_oanda_symbol(symbol: str) -> str:
    """CRT SNIPER symbol → OANDA 格式"""
    s = symbol.upper().strip()
    if s in OANDA_SYMBOL_MAP:
        return OANDA_SYMBOL_MAP[s]
    # 嘗試自動轉換：6 字元外匯對 → XXX_YYY
    if len(s) == 6 and s.isalpha():
        return f"{s[:3]}_{s[3:]}"
    return s


class OandaBroker(BaseBroker):
    """
    OANDA v20 REST API 實作

    API 文件: https://developer.oanda.com/rest-live-v20/
    """

    def __init__(
        self,
        api_token: str,
        account_id: str,
        is_live: bool = False,
    ):
        self.api_token = api_token
        self.account_id = account_id

        if is_live:
            self.base_url = "https://api-fxtrade.oanda.com/v3"
        else:
            self.base_url = "https://api-fxpractice.oanda.com/v3"

        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            timeout=30.0,
        )

    async def connect(self) -> bool:
        try:
            resp = await self.client.get(f"/accounts/{self.account_id}")
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"OANDA 已連線: {data['account']['id']}")
            return True
        except Exception as e:
            logger.error(f"OANDA 連線失敗: {e}")
            return False

    async def get_account(self) -> AccountInfo:
        resp = await self.client.get(f"/accounts/{self.account_id}/summary")
        resp.raise_for_status()
        acc = resp.json()["account"]
        return AccountInfo(
            balance=float(acc["balance"]),
            equity=float(acc["NAV"]),
            margin_used=float(acc["marginUsed"]),
            margin_available=float(acc["marginAvailable"]),
            open_positions=int(acc["openPositionCount"]),
            currency=acc["currency"],
        )

    async def get_price(self, symbol: str) -> tuple[float, float]:
        oanda_sym = to_oanda_symbol(symbol)
        resp = await self.client.get(
            f"/accounts/{self.account_id}/pricing",
            params={"instruments": oanda_sym},
        )
        resp.raise_for_status()
        prices = resp.json()["prices"]
        if not prices:
            raise ValueError(f"無法取得 {symbol} 報價")
        bid = float(prices[0]["bids"][0]["price"])
        ask = float(prices[0]["asks"][0]["price"])
        return bid, ask

    async def market_order(
        self,
        symbol: str,
        side: SignalSide,
        units: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> OrderResult:
        oanda_sym = to_oanda_symbol(symbol)

        # OANDA 用正數=買，負數=賣
        order_units = units if side == SignalSide.LONG else -units

        order_body = {
            "order": {
                "type": "MARKET",
                "instrument": oanda_sym,
                "units": str(int(order_units)),
                "timeInForce": "FOK",
            }
        }

        if sl is not None:
            order_body["order"]["stopLossOnFill"] = {
                "price": _format_price(sl, symbol),
                "timeInForce": "GTC",
            }
        if tp is not None:
            order_body["order"]["takeProfitOnFill"] = {
                "price": _format_price(tp, symbol),
                "timeInForce": "GTC",
            }

        try:
            resp = await self.client.post(
                f"/accounts/{self.account_id}/orders",
                json=order_body,
            )
            resp.raise_for_status()
            data = resp.json()

            if "orderFillTransaction" in data:
                fill = data["orderFillTransaction"]
                trade_id = fill.get("tradeOpened", {}).get("tradeID", "")
                return OrderResult(
                    success=True,
                    order_id=fill.get("id", ""),
                    trade_id=trade_id,
                    symbol=symbol,
                    side=side.value,
                    units=abs(float(fill.get("units", 0))),
                    entry_price=float(fill.get("price", 0)),
                    sl_price=sl or 0,
                    tp_price=tp or 0,
                )
            else:
                reason = data.get("orderRejectTransaction", {}).get("rejectReason", "unknown")
                return OrderResult(success=False, symbol=symbol, error=f"order rejected: {reason}")

        except Exception as e:
            logger.error(f"下單失敗 {symbol}: {e}")
            return OrderResult(success=False, symbol=symbol, error=str(e))

    async def modify_trade(
        self,
        trade_id: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        **kwargs,
    ) -> bool:
        body = {}

        if sl is not None:
            body["stopLoss"] = {
                "price": str(sl),
                "timeInForce": "GTC",
            }
        if tp is not None:
            body["takeProfit"] = {
                "price": str(tp),
                "timeInForce": "GTC",
            }

        if not body:
            return True

        try:
            resp = await self.client.put(
                f"/accounts/{self.account_id}/trades/{trade_id}/orders",
                json=body,
            )
            resp.raise_for_status()
            logger.info(f"Trade {trade_id} 已修改: SL={sl}, TP={tp}")
            return True
        except Exception as e:
            logger.error(f"修改 trade {trade_id} 失敗: {e}")
            return False

    async def close_trade(self, trade_id: str) -> bool:
        try:
            resp = await self.client.put(
                f"/accounts/{self.account_id}/trades/{trade_id}/close",
                json={"units": "ALL"},
            )
            resp.raise_for_status()
            logger.info(f"Trade {trade_id} 已平倉")
            return True
        except Exception as e:
            logger.error(f"平倉 {trade_id} 失敗: {e}")
            return False

    async def get_open_positions(self) -> list[Position]:
        resp = await self.client.get(f"/accounts/{self.account_id}/openTrades")
        resp.raise_for_status()
        trades = resp.json().get("trades", [])
        return [_parse_trade(t) for t in trades]

    async def get_position_by_symbol(self, symbol: str) -> Optional[Position]:
        positions = await self.get_open_positions()
        oanda_sym = to_oanda_symbol(symbol)
        for p in positions:
            if p.symbol == oanda_sym:
                return p
        return None


def _parse_trade(trade: dict) -> Position:
    units = float(trade.get("currentUnits", 0))
    return Position(
        trade_id=trade["id"],
        symbol=trade["instrument"],
        side="long" if units > 0 else "short",
        units=abs(units),
        entry_price=float(trade.get("price", 0)),
        current_price=float(trade.get("price", 0)),
        unrealized_pnl=float(trade.get("unrealizedPL", 0)),
        sl_price=float(trade.get("stopLossOrder", {}).get("price", 0)) if trade.get("stopLossOrder") else None,
        tp_price=float(trade.get("takeProfitOrder", {}).get("price", 0)) if trade.get("takeProfitOrder") else None,
    )


def _format_price(price: float, symbol: str) -> str:
    """根據商品格式化價格精度"""
    sym = symbol.upper()
    if "JPY" in sym:
        return f"{price:.3f}"
    elif sym in ("XAUUSD", "XAGUSD", "NAS100USD"):
        return f"{price:.3f}"
    else:
        return f"{price:.5f}"

"""
Binance Futures Broker 實作

職責：
- 透過 Binance USDⓈ-M Futures API 執行交易
- 下單 / 改單 / 平倉 / 查詢持倉
- 加密貨幣永續合約專用

需要：
- Binance 帳戶 + Futures 啟用
- API Key + Secret（從 Binance 後台取得）

CRT SNIPER Symbol: BTCUSDT.P → Binance: BTCUSDT
"""

from __future__ import annotations

import logging
from typing import Optional

import ccxt.async_support as ccxt_async

from src.models import SignalSide
from src.trader.base import BaseBroker, OrderResult, Position, AccountInfo

logger = logging.getLogger(__name__)


def to_binance_symbol(symbol: str) -> str:
    """CRT SNIPER symbol → Binance 格式"""
    s = symbol.upper().strip()
    # 移除 .P 後綴（永續標記）
    if s.endswith(".P"):
        s = s[:-2]
    return s


class BinanceFuturesBroker(BaseBroker):
    """Binance USDⓈ-M Futures 實作（使用 ccxt）"""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        is_testnet: bool = True,
    ):
        options = {
            "defaultType": "future",
            "enableRateLimit": True,
        }
        if is_testnet:
            options["sandboxMode"] = True

        self.exchange = ccxt_async.binance({
            "apiKey": api_key,
            "secret": api_secret,
            "options": options,
        })
        self.is_testnet = is_testnet

    async def connect(self) -> bool:
        try:
            await self.exchange.load_markets()
            balance = await self.exchange.fetch_balance()
            total = balance.get("total", {}).get("USDT", 0)
            mode = "Testnet" if self.is_testnet else "Live"
            logger.info(f"Binance Futures 已連線 ({mode}), USDT: {total}")
            return True
        except Exception as e:
            logger.error(f"Binance 連線失敗: {e}")
            return False

    async def get_account(self) -> AccountInfo:
        balance = await self.exchange.fetch_balance()
        usdt = balance.get("USDT", {})
        return AccountInfo(
            balance=float(usdt.get("total", 0)),
            equity=float(usdt.get("total", 0)),
            margin_used=float(usdt.get("used", 0)),
            margin_available=float(usdt.get("free", 0)),
            open_positions=len(await self.get_open_positions()),
            currency="USDT",
        )

    async def get_price(self, symbol: str) -> tuple[float, float]:
        binance_sym = to_binance_symbol(symbol)
        ticker = await self.exchange.fetch_ticker(binance_sym)
        bid = ticker.get("bid", 0) or ticker.get("last", 0)
        ask = ticker.get("ask", 0) or ticker.get("last", 0)
        return float(bid), float(ask)

    async def market_order(
        self,
        symbol: str,
        side: SignalSide,
        units: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> OrderResult:
        binance_sym = to_binance_symbol(symbol)
        ccxt_side = "buy" if side == SignalSide.LONG else "sell"

        try:
            # 主單：市價單
            order = await self.exchange.create_order(
                symbol=binance_sym,
                type="market",
                side=ccxt_side,
                amount=units,
            )

            trade_id = str(order.get("id", ""))
            entry_price = float(order.get("average", 0) or order.get("price", 0) or 0)

            # 設定止損單
            if sl is not None:
                sl_side = "sell" if side == SignalSide.LONG else "buy"
                await self.exchange.create_order(
                    symbol=binance_sym,
                    type="stop_market",
                    side=sl_side,
                    amount=units,
                    params={
                        "stopPrice": sl,
                        "closePosition": False,
                        "reduceOnly": True,
                    },
                )

            # 設定止盈單
            if tp is not None:
                tp_side = "sell" if side == SignalSide.LONG else "buy"
                await self.exchange.create_order(
                    symbol=binance_sym,
                    type="take_profit_market",
                    side=tp_side,
                    amount=units,
                    params={
                        "stopPrice": tp,
                        "closePosition": False,
                        "reduceOnly": True,
                    },
                )

            logger.info(f"Binance 下單成功: {binance_sym} {ccxt_side} {units} @ {entry_price}")

            return OrderResult(
                success=True,
                order_id=trade_id,
                trade_id=trade_id,
                symbol=symbol,
                side=side.value,
                units=units,
                entry_price=entry_price,
                sl_price=sl or 0,
                tp_price=tp or 0,
            )

        except Exception as e:
            logger.error(f"Binance 下單失敗 {binance_sym}: {e}")
            return OrderResult(success=False, symbol=symbol, error=str(e))

    async def modify_trade(
        self,
        trade_id: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        **kwargs,
    ) -> bool:
        """
        修改止損/止盈。
        Binance Futures 沒有直接改 SL 的 API，需要：
        1. 取消原有的 stop 單
        2. 下新的 stop 單
        """
        try:
            # 找到對應的持倉
            positions = await self.exchange.fetch_positions()
            target_pos = None
            for pos in positions:
                if pos.get("contracts", 0) > 0:
                    target_pos = pos
                    break

            if not target_pos:
                return False

            symbol = target_pos["symbol"]
            amount = abs(float(target_pos.get("contracts", 0)))
            side_str = target_pos.get("side", "long")
            close_side = "sell" if side_str == "long" else "buy"

            # 取消所有現有 stop 單
            open_orders = await self.exchange.fetch_open_orders(symbol)
            for order in open_orders:
                order_type = order.get("type", "")
                if "stop" in order_type.lower() or "take_profit" in order_type.lower():
                    await self.exchange.cancel_order(order["id"], symbol)

            # 下新的 SL
            if sl is not None:
                await self.exchange.create_order(
                    symbol=symbol,
                    type="stop_market",
                    side=close_side,
                    amount=amount,
                    params={
                        "stopPrice": sl,
                        "reduceOnly": True,
                    },
                )

            # 下新的 TP
            if tp is not None:
                await self.exchange.create_order(
                    symbol=symbol,
                    type="take_profit_market",
                    side=close_side,
                    amount=amount,
                    params={
                        "stopPrice": tp,
                        "reduceOnly": True,
                    },
                )

            logger.info(f"Binance SL/TP 已修改: SL={sl}, TP={tp}")
            return True

        except Exception as e:
            logger.error(f"Binance 修改失敗: {e}")
            return False

    async def close_trade(self, trade_id: str) -> bool:
        try:
            positions = await self.exchange.fetch_positions()
            for pos in positions:
                if abs(float(pos.get("contracts", 0))) > 0:
                    symbol = pos["symbol"]
                    amount = abs(float(pos["contracts"]))
                    side = "sell" if pos.get("side") == "long" else "buy"

                    await self.exchange.create_order(
                        symbol=symbol,
                        type="market",
                        side=side,
                        amount=amount,
                        params={"reduceOnly": True},
                    )
                    logger.info(f"Binance 已平倉: {symbol}")
                    return True
            return False
        except Exception as e:
            logger.error(f"Binance 平倉失敗: {e}")
            return False

    async def get_open_positions(self) -> list[Position]:
        positions = await self.exchange.fetch_positions()
        result = []
        for pos in positions:
            contracts = abs(float(pos.get("contracts", 0)))
            if contracts > 0:
                result.append(Position(
                    trade_id=pos.get("id", ""),
                    symbol=pos["symbol"],
                    side=pos.get("side", "long"),
                    units=contracts,
                    entry_price=float(pos.get("entryPrice", 0)),
                    current_price=float(pos.get("markPrice", 0)),
                    unrealized_pnl=float(pos.get("unrealizedPnl", 0)),
                ))
        return result

    async def get_position_by_symbol(self, symbol: str) -> Optional[Position]:
        binance_sym = to_binance_symbol(symbol)
        positions = await self.get_open_positions()
        for p in positions:
            # ccxt 格式可能是 BTCUSDT 或 BTC/USDT:USDT
            if binance_sym in p.symbol.replace("/", "").replace(":USDT", ""):
                return p
        return None

    async def close(self):
        """關閉連線"""
        await self.exchange.close()

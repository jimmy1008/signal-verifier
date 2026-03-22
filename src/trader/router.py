"""
交易路由器

職責：
- 根據 symbol 自動判斷走 OANDA 還是 Binance
- 統一管理雙 broker
- 合併持倉查詢

規則：
  xxxUSDT.P → Binance Futures（加密貨幣）
  NAS100USD / XAUUSD / EURJPY 等 → OANDA（外匯 / CFD）
"""

from __future__ import annotations

import logging
from typing import Optional

from src.models import SignalSide
from src.trader.base import BaseBroker, OrderResult, Position, AccountInfo

logger = logging.getLogger(__name__)

# 加密貨幣判定
CRYPTO_SUFFIXES = (".P", "USDT", "BUSD", "USDC")
CRYPTO_BASES = {
    "BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "DOGE", "BNB",
    "LINK", "HYPE", "TAO", "SUI", "ORDI", "ZEC", "LTC", "UNI",
    "WLD", "PEPE", "ZEN", "ARB", "ATOM", "DOT", "AAVE", "1000PEPE",
}


def is_crypto(symbol: str) -> bool:
    """判斷是否為加密貨幣"""
    s = symbol.upper().strip()

    # 有 .P 後綴 = 永續合約
    if s.endswith(".P"):
        return True

    # 以 USDT 結尾
    if s.endswith("USDT"):
        return True

    # 在已知列表中
    base = s.replace("USDT", "").replace(".P", "")
    if base in CRYPTO_BASES:
        return True

    return False


class TradingRouter(BaseBroker):
    """
    雙 Broker 路由器。

    根據 symbol 自動選擇：
    - crypto → Binance Futures
    - forex/cfd → OANDA
    """

    def __init__(
        self,
        crypto_broker: Optional[BaseBroker] = None,
        forex_broker: Optional[BaseBroker] = None,
    ):
        self.crypto = crypto_broker
        self.forex = forex_broker

    def _route(self, symbol: str) -> BaseBroker:
        """根據 symbol 選擇 broker"""
        if is_crypto(symbol):
            if self.crypto is None:
                raise ValueError(f"收到加密貨幣信號 {symbol}，但未設定 Binance broker")
            return self.crypto
        else:
            if self.forex is None:
                raise ValueError(f"收到外匯/CFD 信號 {symbol}，但未設定 OANDA broker")
            return self.forex

    def _route_label(self, symbol: str) -> str:
        return "Binance" if is_crypto(symbol) else "OANDA"

    async def connect(self) -> bool:
        results = []
        if self.crypto:
            ok = await self.crypto.connect()
            results.append(("Binance", ok))
        if self.forex:
            ok = await self.forex.connect()
            results.append(("OANDA", ok))

        for name, ok in results:
            status = "OK" if ok else "FAILED"
            logger.info(f"{name}: {status}")

        return all(ok for _, ok in results)

    async def get_account(self) -> AccountInfo:
        # 回傳 forex broker 帳戶（或 crypto）
        if self.forex:
            return await self.forex.get_account()
        if self.crypto:
            return await self.crypto.get_account()
        raise ValueError("無可用 broker")

    async def get_price(self, symbol: str) -> tuple[float, float]:
        broker = self._route(symbol)
        return await broker.get_price(symbol)

    async def market_order(
        self,
        symbol: str,
        side: SignalSide,
        units: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        tp3: Optional[float] = None,
    ) -> OrderResult:
        broker = self._route(symbol)
        label = self._route_label(symbol)
        logger.info(f"[{label}] 下單 {symbol} {side.value} units={units}")
        return await broker.market_order(symbol, side, units, sl, tp, tp3)

    async def modify_trade(
        self,
        trade_id: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        **kwargs,
    ) -> bool:
        # 嘗試兩邊都改（trade_id 應該只存在一邊）
        if self.crypto:
            try:
                result = await self.crypto.modify_trade(trade_id, sl, tp, **kwargs)
                if result:
                    return True
            except Exception:
                pass
        if self.forex:
            try:
                result = await self.forex.modify_trade(trade_id, sl, tp, **kwargs)
                if result:
                    return True
            except Exception:
                pass
        return False

    async def close_trade(self, trade_id: str) -> bool:
        if self.crypto:
            try:
                if await self.crypto.close_trade(trade_id):
                    return True
            except Exception:
                pass
        if self.forex:
            try:
                if await self.forex.close_trade(trade_id):
                    return True
            except Exception:
                pass
        return False

    async def get_open_positions(self) -> list[Position]:
        positions = []
        if self.crypto:
            positions.extend(await self.crypto.get_open_positions())
        if self.forex:
            positions.extend(await self.forex.get_open_positions())
        return positions

    async def get_position_by_symbol(self, symbol: str) -> Optional[Position]:
        broker = self._route(symbol)
        return await broker.get_position_by_symbol(symbol)

    async def get_combined_account(self) -> dict:
        """取得兩邊帳戶的合併摘要"""
        result = {}
        if self.crypto:
            acc = await self.crypto.get_account()
            result["binance"] = {
                "balance": acc.balance,
                "equity": acc.equity,
                "positions": acc.open_positions,
                "currency": acc.currency,
            }
        if self.forex:
            acc = await self.forex.get_account()
            result["oanda"] = {
                "balance": acc.balance,
                "equity": acc.equity,
                "positions": acc.open_positions,
                "currency": acc.currency,
            }
        return result

"""
Paper Broker — 模擬交易，不實際下單

特點：
- 模擬帳戶餘額（初始 $500）
- 使用 BingX 即時報價
- 模擬手續費（開倉 + 平倉各 0.05% = 合計 0.1%）
- 所有訂單即時成交（市價單）
- SL/TP 由 executor 價格監控管理（不模擬觸發）
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

import ccxt.async_support as ccxt_async

from src.models import SignalSide
from src.trader.base import (
    AccountInfo, BaseBroker, MarketInfo, OrderResult, Position,
)
from src.trader.bingx import to_bingx_symbol

logger = logging.getLogger(__name__)

FEE_RATE = 0.0005  # 單邊手續費 0.05%


class PaperBroker(BaseBroker):
    """模擬交易 Broker"""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        initial_balance: float = 500.0,
        leverage: int = 50,
    ):
        # 用真實 exchange 取報價
        self.exchange = ccxt_async.bingx({
            "apiKey": api_key,
            "secret": api_secret,
            "options": {"defaultType": "swap", "enableRateLimit": True},
        })
        self.leverage = leverage
        self._balance = initial_balance
        self._initial_balance = initial_balance
        self._positions: dict[str, _PaperPosition] = {}  # symbol -> position
        self._market_cache: dict[str, MarketInfo] = {}
        self._last_protection_failure: dict | None = None
        self._total_fee = 0.0
        self._realized_pnl = 0.0

    async def connect(self) -> bool:
        try:
            await self.exchange.load_markets()
            logger.info(f"PaperBroker 已連線 (模擬), 初始餘額: ${self._balance:.2f}")
            return True
        except Exception as e:
            logger.error(f"PaperBroker 連線失敗: {e}")
            return False

    async def get_account(self) -> AccountInfo:
        # 計算未實現盈虧
        unrealized = 0.0
        for pos in self._positions.values():
            if pos.units != 0:
                try:
                    bid, ask = await self.get_price(pos.symbol)
                    price = bid if pos.side == "sell" else ask
                    unrealized += pos.unrealized_pnl(price)
                except Exception:
                    pass

        equity = self._balance + unrealized
        return AccountInfo(
            balance=self._balance,
            equity=equity,
            margin_used=0,
            margin_available=self._balance,
            open_positions=sum(1 for p in self._positions.values() if p.units != 0),
            currency="USDT",
        )

    async def get_price(self, symbol: str) -> tuple[float, float]:
        bingx_sym = to_bingx_symbol(symbol)
        ticker = await self.exchange.fetch_ticker(bingx_sym)
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
        tp3: Optional[float] = None,
    ) -> OrderResult:
        try:
            bid, ask = await self.get_price(symbol)
            # 模擬成交價：買用 ask，賣用 bid
            fill_price = ask if side == SignalSide.LONG else bid

            # 計算手續費
            notional = units * fill_price
            fee = notional * FEE_RATE
            self._balance -= fee
            self._total_fee += fee

            # 記錄持倉
            key = f"{symbol}_{side.value}"
            if key not in self._positions:
                self._positions[key] = _PaperPosition(symbol, side.value)

            pos = self._positions[key]
            pos.add(units, fill_price)

            trade_id = f"paper_{uuid.uuid4().hex[:8]}"
            logger.info(
                f"PaperBroker 模擬成交: {symbol} {side.value} {units} @ {fill_price} "
                f"fee=${fee:.4f} [SL/TP]"
            )

            return OrderResult(
                success=True,
                trade_id=trade_id,
                symbol=symbol,
                side=side.value,
                units=units,
                entry_price=fill_price,
                error="",
            )
        except Exception as e:
            logger.error(f"PaperBroker 下單失敗: {e}")
            return OrderResult(
                success=False, trade_id="", symbol=symbol,
                side=side.value, units=0, price=0, error=str(e),
            )

    async def close_trade(self, trade_id: str, symbol: str = "", units: float = 0, side: str = "") -> bool:
        """平倉"""
        for key, pos in self._positions.items():
            if pos.units > 0 and (not symbol or symbol in key):
                try:
                    bid, ask = await self.get_price(pos.symbol)
                    close_price = bid if pos.side == "long" else ask
                    close_units = units if units > 0 else pos.units

                    # 平倉手續費
                    fee = close_units * close_price * FEE_RATE
                    self._balance -= fee
                    self._total_fee += fee

                    # 計算盈虧
                    pnl = pos.close(close_units, close_price)
                    self._balance += pnl
                    self._realized_pnl += pnl

                    logger.info(
                        f"PaperBroker 平倉: {pos.symbol} {close_units} @ {close_price} "
                        f"pnl=${pnl:.4f} fee=${fee:.4f}"
                    )
                    return True
                except Exception as e:
                    logger.error(f"PaperBroker 平倉失敗: {e}")
                    return False
        return False

    async def partial_close(self, symbol: str, side: str, units: float) -> OrderResult:
        """部分平倉"""
        key = f"{symbol}_{side}"
        pos = self._positions.get(key)
        if not pos or pos.units <= 0:
            return OrderResult(
                success=False, trade_id="", symbol=symbol,
                side=side, units=0, price=0, error="no position",
            )

        try:
            bid, ask = await self.get_price(symbol)
            close_price = bid if side == "long" else ask

            fee = units * close_price * FEE_RATE
            self._balance -= fee
            self._total_fee += fee

            pnl = pos.close(units, close_price)
            self._balance += pnl
            self._realized_pnl += pnl

            logger.info(
                f"PaperBroker 部分平倉: {symbol} {units} @ {close_price} "
                f"pnl=${pnl:.4f} fee=${fee:.4f}"
            )
            return OrderResult(
                success=True, trade_id=f"paper_close_{uuid.uuid4().hex[:8]}",
                symbol=symbol, side=side, units=units, entry_price=close_price,
            )
        except Exception as e:
            return OrderResult(
                success=False, trade_id="", symbol=symbol,
                side=side, units=0, price=0, error=str(e),
            )

    async def modify_trade(
        self,
        trade_id: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        symbol: str = "",
        side: str = "",
    ) -> bool:
        # Paper mode: SL/TP 由 executor 管理，這裡只是紀錄
        if sl:
            bingx_sym = to_bingx_symbol(symbol) if symbol else trade_id
            side_str = side.upper() if side else ""
            logger.info(f"PaperBroker SL 已移動: {bingx_sym} {side_str} SL={sl}")
        return True

    async def get_open_positions(self) -> list[Position]:
        result = []
        for key, pos in self._positions.items():
            if pos.units > 0:
                try:
                    bid, ask = await self.get_price(pos.symbol)
                    price = (bid + ask) / 2
                    upnl = pos.unrealized_pnl(price)
                    result.append(Position(
                        symbol=pos.symbol,
                        side=pos.side,
                        units=pos.units,
                        entry_price=pos.entry_price,
                        current_price=price,
                        unrealized_pnl=upnl,
                    ))
                except Exception:
                    pass
        return result

    async def get_position_by_symbol(self, symbol: str) -> Optional[Position]:
        positions = await self.get_open_positions()
        for p in positions:
            if p.symbol == symbol or symbol in p.symbol:
                return p
        return None

    def get_market(self, symbol: str) -> Optional[MarketInfo]:
        bingx_sym = to_bingx_symbol(symbol)
        if bingx_sym in self._market_cache:
            return self._market_cache[bingx_sym]
        try:
            m = self.exchange.market(bingx_sym)
            precision = m.get("precision", {})
            limits = m.get("limits", {})
            mi = MarketInfo(
                symbol=bingx_sym,
                base_currency=m.get("base", ""),
                min_quantity=limits.get("amount", {}).get("min", 0.001),
                quantity_step=precision.get("amount", 0.001),
                price_step=precision.get("price", 0.01),
                min_notional=limits.get("cost", {}).get("min", 1.0),
            )
            self._market_cache[bingx_sym] = mi
            return mi
        except Exception:
            return None

    def summary(self) -> dict:
        return {
            "initial_balance": self._initial_balance,
            "current_balance": self._balance,
            "realized_pnl": self._realized_pnl,
            "total_fee": self._total_fee,
            "open_positions": sum(1 for p in self._positions.values() if p.units > 0),
        }


class _PaperPosition:
    """內部持倉追蹤"""

    def __init__(self, symbol: str, side: str):
        self.symbol = symbol
        self.side = side
        self.units: float = 0.0
        self.entry_price: float = 0.0
        self._total_cost: float = 0.0

    def add(self, units: float, price: float) -> None:
        if self.units == 0:
            self.entry_price = price
            self.units = units
            self._total_cost = units * price
        else:
            self._total_cost += units * price
            self.units += units
            self.entry_price = self._total_cost / self.units

    def close(self, units: float, price: float) -> float:
        close_units = min(units, self.units)
        if self.side == "long":
            pnl = (price - self.entry_price) * close_units
        else:
            pnl = (self.entry_price - price) * close_units

        self.units -= close_units
        if self.units < 1e-10:
            self.units = 0.0
            self._total_cost = 0.0
        else:
            self._total_cost = self.units * self.entry_price
        return pnl

    def unrealized_pnl(self, current_price: float) -> float:
        if self.units == 0:
            return 0.0
        if self.side == "long":
            return (current_price - self.entry_price) * self.units
        else:
            return (self.entry_price - current_price) * self.units

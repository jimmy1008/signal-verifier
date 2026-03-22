"""
Broker 介面定義

職責：定義所有 broker 的共同介面
所有 broker 實作都要繼承 BaseBroker
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional
from pydantic import BaseModel, Field
from datetime import datetime

from src.models import SignalSide

logger = logging.getLogger(__name__)


class OrderResult(BaseModel):
    """下單結果"""
    success: bool
    order_id: str = ""
    trade_id: str = ""
    symbol: str = ""
    side: str = ""
    units: float = 0.0
    entry_price: float = 0.0
    sl_price: float = 0.0
    tp_price: float = 0.0
    error: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class Position(BaseModel):
    """持倉資訊"""
    trade_id: str
    symbol: str
    side: str              # "long" / "short"
    units: float
    entry_price: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None


class AccountInfo(BaseModel):
    """帳戶資訊"""
    balance: float
    equity: float
    margin_used: float = 0.0
    margin_available: float = 0.0
    open_positions: int = 0
    currency: str = "USD"


# ── Market 精度規格（參考 bbgo market.go）──────────────

class MarketInfo(BaseModel):
    """交易對規格：精度、最小量、tick size"""
    symbol: str
    base_currency: str = ""
    quote_currency: str = "USDT"
    price_precision: int = 8
    amount_precision: int = 8
    min_notional: float = 5.0       # 最小名義價值
    min_quantity: float = 0.001
    tick_size: float = 0.01         # 價格最小步長
    step_size: float = 0.001        # 數量最小步長

    def round_price(self, price: float) -> float:
        if self.tick_size > 0:
            return round(round(price / self.tick_size) * self.tick_size, self.price_precision)
        return round(price, self.price_precision)

    def round_quantity(self, qty: float) -> float:
        import math
        if self.step_size >= 1.0:
            # 步長 ≥ 1（如 SOL）：四捨五入到整數，至少 1
            rounded = max(round(qty), 1) * self.step_size
        elif self.step_size > 0:
            rounded = math.floor(qty / self.step_size) * self.step_size
            rounded = round(rounded, self.amount_precision)
        else:
            rounded = round(qty, self.amount_precision)
        if rounded < self.min_quantity:
            rounded = self.min_quantity
        return rounded

    def validate_order(self, price: float, quantity: float) -> tuple[bool, str]:
        if quantity < self.min_quantity:
            return False, f"數量 {quantity} < 最小量 {self.min_quantity}"
        notional = price * quantity
        if notional < self.min_notional:
            return False, f"名義價值 ${notional:.2f} < 最小 ${self.min_notional}"
        return True, ""


# ── Position Tracker（參考 bbgo position.go）──────────

class PositionTracker:
    """
    即時持倉追蹤器 — 逐筆成交累加，自動算均價和已實現盈虧。
    正 base = 多倉, 負 base = 空倉, 零 = 已平。
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.base: float = 0.0              # 持倉量（正=多, 負=空）
        self.quote: float = 0.0             # 累計成本（quote currency）
        self.average_cost: float = 0.0      # 持倉均價
        self.realized_pnl: float = 0.0      # 已實現盈虧
        self.total_fee: float = 0.0         # 累計手續費
        self.trade_count: int = 0           # 成交筆數
        self.closed_trades: list[dict] = [] # 已平倉紀錄

    @property
    def is_long(self) -> bool:
        return self.base > 0

    @property
    def is_short(self) -> bool:
        return self.base < 0

    @property
    def is_closed(self) -> bool:
        return abs(self.base) < 1e-10

    def unrealized_pnl(self, current_price: float) -> float:
        if self.is_closed:
            return 0.0
        return self.base * (current_price - self.average_cost)

    def add_trade(self, side: str, quantity: float, price: float,
                  fee: float = 0.0, timestamp: datetime | None = None) -> float:
        """
        記錄一筆成交，回傳此筆的已實現盈虧（平倉時）。
        side: "buy" / "sell"
        """
        self.trade_count += 1
        self.total_fee += fee

        # buy = +quantity, sell = -quantity
        delta = quantity if side == "buy" else -quantity
        realized = 0.0

        if self.is_closed:
            # 新開倉
            self.base = delta
            self.quote = abs(delta) * price
            self.average_cost = price

        elif (self.base > 0 and delta > 0) or (self.base < 0 and delta < 0):
            # 同方向加倉 → 加權平均
            old_cost = abs(self.base) * self.average_cost
            new_cost = abs(delta) * price
            self.base += delta
            total_qty = abs(self.base)
            self.average_cost = (old_cost + new_cost) / total_qty if total_qty > 0 else price
            self.quote = total_qty * self.average_cost

        else:
            # 反方向 → 部分/全部平倉
            close_qty = min(abs(delta), abs(self.base))

            if self.base > 0:
                # 多倉被賣平：盈虧 = (賣價 - 均價) * 平倉量
                realized = (price - self.average_cost) * close_qty
            else:
                # 空倉被買平：盈虧 = (均價 - 買價) * 平倉量
                realized = (self.average_cost - price) * close_qty

            realized -= fee  # 扣手續費
            self.realized_pnl += realized

            remaining = abs(delta) - close_qty
            self.base += delta

            if abs(self.base) < 1e-10:
                # 完全平倉
                self._record_close(realized, price, timestamp)
                self.base = 0.0
                self.quote = 0.0
                self.average_cost = 0.0
            elif remaining > 0:
                # 翻轉方向（平完後還有餘量）
                self._record_close(realized, price, timestamp)
                self.average_cost = price
                self.quote = abs(self.base) * price
            else:
                # 部分平倉，均價不變
                self.quote = abs(self.base) * self.average_cost

        return realized

    def _record_close(self, pnl: float, exit_price: float,
                      timestamp: datetime | None = None) -> None:
        self.closed_trades.append({
            "symbol": self.symbol,
            "pnl": pnl,
            "exit_price": exit_price,
            "time": timestamp or datetime.utcnow(),
        })

    def summary(self) -> dict:
        return {
            "symbol": self.symbol,
            "base": self.base,
            "average_cost": self.average_cost,
            "realized_pnl": self.realized_pnl,
            "total_fee": self.total_fee,
            "trade_count": self.trade_count,
            "closed_count": len(self.closed_trades),
        }


class BaseBroker(ABC):
    """Broker 抽象介面"""

    @abstractmethod
    async def connect(self) -> bool:
        """連線到 broker"""
        ...

    @abstractmethod
    async def get_account(self) -> AccountInfo:
        """取得帳戶資訊"""
        ...

    @abstractmethod
    async def get_price(self, symbol: str) -> tuple[float, float]:
        """取得即時報價 (bid, ask)"""
        ...

    @abstractmethod
    async def market_order(
        self,
        symbol: str,
        side: SignalSide,
        units: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        tp3: Optional[float] = None,
    ) -> OrderResult:
        """市價單"""
        ...

    @abstractmethod
    async def modify_trade(
        self,
        trade_id: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> bool:
        """修改持倉的 SL / TP"""
        ...

    @abstractmethod
    async def close_trade(self, trade_id: str) -> bool:
        """平倉"""
        ...

    @abstractmethod
    async def get_open_positions(self) -> list[Position]:
        """取得所有持倉"""
        ...

    @abstractmethod
    async def get_position_by_symbol(self, symbol: str) -> Optional[Position]:
        """取得指定商品的持倉"""
        ...

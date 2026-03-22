"""
核心資料模型 — 所有模組共用的 Pydantic Models & SQLAlchemy ORM
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Boolean,
    JSON,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


# ============================================================
# Enums
# ============================================================

class SignalSide(str, enum.Enum):
    LONG = "long"
    SHORT = "short"


class SignalStatus(str, enum.Enum):
    PENDING = "pending"           # 剛解析出來
    TRIGGERED = "triggered"       # 已觸發進場
    NOT_TRIGGERED = "not_triggered"  # 過期未觸發
    CLOSED = "closed"             # 已結束
    CANCELLED = "cancelled"       # 被取消


class UpdateType(str, enum.Enum):
    TP_HIT = "tp_hit"            # 已達 TPx
    SL_MOVED = "sl_moved"        # 移動止損
    CLOSE_NOW = "close_now"      # 立即平倉
    CANCEL = "cancel"            # 取消信號
    ENTRY_UPDATE = "entry_update" # 更新進場價
    OTHER = "other"


class ExitReason(str, enum.Enum):
    TP_HIT = "tp_hit"
    SL_HIT = "sl_hit"
    BREAKEVEN = "breakeven"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    MANUAL_CLOSE = "manual_close"


class AmbiguousMode(str, enum.Enum):
    CONSERVATIVE = "conservative"  # 同 K 線優先算 SL
    OPTIMISTIC = "optimistic"      # 同 K 線優先算 TP


# ============================================================
# SQLAlchemy ORM
# ============================================================

class Base(DeclarativeBase):
    pass


class RawMessageORM(Base):
    """原始 Telegram 訊息"""
    __tablename__ = "raw_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(100), nullable=False)       # 頻道名稱
    chat_id = Column(String(50), nullable=False)
    message_id = Column(String(50), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    raw_text = Column(Text, nullable=False)
    reply_to_message_id = Column(String(50), nullable=True)
    is_edited = Column(Boolean, default=False)
    is_forwarded = Column(Boolean, default=False)
    parsed_status = Column(String(20), default="pending")  # pending / parsed / skipped
    created_at = Column(DateTime, server_default=func.now())


class SignalORM(Base):
    """解析後的交易訊號"""
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(100), nullable=False)
    signal_key = Column(String(200), nullable=True, unique=True)  # 用於去重
    symbol = Column(String(50), nullable=False)
    side = Column(Enum(SignalSide), nullable=False)
    entry = Column(Float, nullable=False)
    sl = Column(Float, nullable=False)
    tp1 = Column(Float, nullable=True)
    tp2 = Column(Float, nullable=True)
    tp3 = Column(Float, nullable=True)
    tp4 = Column(Float, nullable=True)
    timeframe = Column(String(10), nullable=True)
    signal_time = Column(DateTime, nullable=False)
    raw_message_id = Column(Integer, ForeignKey("raw_messages.id"), nullable=True)
    status = Column(Enum(SignalStatus), default=SignalStatus.PENDING)
    created_at = Column(DateTime, server_default=func.now())

    updates = relationship("SignalUpdateORM", back_populates="signal")


class SignalUpdateORM(Base):
    """信號後續更新"""
    __tablename__ = "signal_updates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=False)
    update_type = Column(Enum(UpdateType), nullable=False)
    update_value = Column(String(200), nullable=True)
    raw_message_id = Column(Integer, ForeignKey("raw_messages.id"), nullable=True)
    timestamp = Column(DateTime, nullable=False)

    signal = relationship("SignalORM", back_populates="updates")


class CandleORM(Base):
    """市場 K 線快取"""
    __tablename__ = "candles"
    __table_args__ = (UniqueConstraint('symbol', 'timeframe', 'open_time', name='uq_candle'),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), nullable=False)
    timeframe = Column(String(10), nullable=False)
    open_time = Column(DateTime, nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=True)
    source = Column(String(50), default="binance")


class BingxTradeORM(Base):
    """BingX 成交紀錄本地存檔（防 API 歷史丟失）"""
    __tablename__ = "bingx_trades"
    __table_args__ = (UniqueConstraint('account', 'trade_id', name='uq_bingx_trade'),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    account = Column(String(10), nullable=False)           # "h1" / "h4"
    trade_id = Column(String(100), nullable=False)         # BingX order/trade ID
    symbol = Column(String(50), nullable=False)
    side = Column(String(10), nullable=False)              # "buy" / "sell"
    position_side = Column(String(10), nullable=False)     # "LONG" / "SHORT"
    price = Column(Float, nullable=False)
    amount = Column(Float, nullable=False)                 # 合約數量
    notional = Column(Float, nullable=False)               # USDT 名義價值
    commission = Column(Float, default=0)                  # 手續費
    order_type = Column(String(50), nullable=True)         # MARKET / STOP_MARKET / TAKE_PROFIT 等
    timestamp = Column(DateTime, nullable=False)
    raw_json = Column(JSON, nullable=True)                 # 完整原始數據
    created_at = Column(DateTime, server_default=func.now())


class BacktestRunORM(Base):
    """回測執行紀錄"""
    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_name = Column(String(100), nullable=False)
    config_json = Column(JSON, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    results = relationship("TradeResultORM", back_populates="run")


class TradeResultORM(Base):
    """每筆信號在某個回測規則下的結果"""
    __tablename__ = "trade_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("backtest_runs.id"), nullable=False)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=False)
    triggered = Column(Boolean, nullable=False)
    entry_time = Column(DateTime, nullable=True)
    exit_time = Column(DateTime, nullable=True)
    exit_reason = Column(Enum(ExitReason), nullable=True)
    exit_price = Column(Float, nullable=True)
    max_tp_hit = Column(Integer, default=0)    # 最高觸及的 TP 層級 (0-4)
    pnl_r = Column(Float, nullable=True)       # 以 R 為單位的損益
    pnl_pct = Column(Float, nullable=True)     # 百分比損益
    drawdown_r = Column(Float, nullable=True)  # 此筆交易最大回撤 (R)
    notes = Column(Text, nullable=True)

    run = relationship("BacktestRunORM", back_populates="results")


# ============================================================
# Pydantic Models (模組間傳遞用)
# ============================================================

class ParsedSignal(BaseModel):
    """Parser 輸出的結構化信號"""
    symbol: str
    side: SignalSide
    entry: float
    sl: float
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    tp4: Optional[float] = None
    timeframe: Optional[str] = None
    signal_time: datetime
    signal_type: str = "entry"  # entry / update / close / cancel
    raw_message_id: Optional[int] = None
    # 用於 update 類型
    related_signal_key: Optional[str] = None
    update_type: Optional[UpdateType] = None
    update_value: Optional[str] = None


class Candle(BaseModel):
    """單根 K 線"""
    symbol: str
    timeframe: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class TradeResult(BaseModel):
    """單筆交易結果"""
    signal_id: int
    triggered: bool
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[ExitReason] = None
    exit_price: Optional[float] = None
    max_tp_hit: int = 0
    pnl_r: float = 0.0
    pnl_pct: float = 0.0
    drawdown_r: float = 0.0
    notes: str = ""


class BacktestConfig(BaseModel):
    """回測配置"""
    name: str = "default"
    mode: str = "partial_be"          # single_tp | partial_tp | breakeven | partial_be
    target_tp: str = "tp4"            # single_tp / breakeven 模式用
    partial_weights: dict = Field(default_factory=lambda: {
        "tp1": 0, "tp2": 0, "tp3": 0.50, "tp4": 0.50
    })
    move_sl_after: str = "tp2"        # breakeven 模式用
    ambiguous_mode: AmbiguousMode = AmbiguousMode.CONSERVATIVE
    signal_expiry_bars: int = 48
    signal_expiry_hours: int = 72


class PerformanceMetrics(BaseModel):
    """績效統計結果"""
    total_signals: int = 0
    triggered_count: int = 0
    not_triggered_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    breakeven_count: int = 0
    win_rate: float = 0.0
    loss_rate: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    avg_rr: float = 0.0
    expectancy: float = 0.0
    total_r: float = 0.0
    max_drawdown_r: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    tp1_hit_rate: float = 0.0
    tp2_hit_rate: float = 0.0
    tp3_hit_rate: float = 0.0
    tp4_hit_rate: float = 0.0
    tp1_hit_then_sl_rate: float = 0.0  # 碰 TP1 但最後打 SL 的比例

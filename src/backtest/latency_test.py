"""
延遲敏感度測試

職責：
- 模擬不同進場延遲對績效的影響
- 測試 0s / 5s / 10s / 1根K 延遲
- 如果延遲一點就崩 → 策略不可用

原理：
  延遲 = 進場價格變差
  對 LONG：進場價往上偏移（追高買入）
  對 SHORT：進場價往下偏移（追低賣出）
  用 K 線的 open/high/low 來模擬延遲後的實際進場價

輸入：list[SignalORM] + candles + BacktestConfig
輸出：LatencyTestResult
"""

from __future__ import annotations

import copy
import logging
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.models import (
    SignalORM,
    SignalSide,
    BacktestConfig,
    TradeResult,
    Candle,
)
from src.backtest.engine import simulate_trade
from src.market_data.provider import load_candles, MarketDataProvider
from src.stats.metrics import compute_metrics, PerformanceMetrics

logger = logging.getLogger(__name__)


class LatencyLevel(BaseModel):
    """單一延遲等級的結果"""
    label: str                       # "0s", "5s", "10s", "1_bar"
    delay_bars: int = 0              # 延遲幾根 K 線
    slippage_pct: float = 0.0        # 進場滑點百分比
    metrics: Optional[PerformanceMetrics] = None
    total_r: float = 0.0
    expectancy: float = 0.0


class LatencyTestResult(BaseModel):
    """延遲敏感度測試結果"""
    levels: list[LatencyLevel] = Field(default_factory=list)
    latency_sensitive: bool = False
    max_viable_delay: str = ""       # 最大可承受延遲
    degradation_pct: float = 0.0     # 從 0s 到最大延遲的績效衰退%
    reasons: list[str] = Field(default_factory=list)


# 預設延遲設定
# 對 15m K 線：5s ≈ 0.03% slippage, 10s ≈ 0.06%, 1 bar = 跳過一根
DEFAULT_LATENCY_LEVELS = [
    {"label": "0s (ideal)", "delay_bars": 0, "slippage_pct": 0.0},
    {"label": "5s delay", "delay_bars": 0, "slippage_pct": 0.03},
    {"label": "10s delay", "delay_bars": 0, "slippage_pct": 0.06},
    {"label": "30s delay", "delay_bars": 0, "slippage_pct": 0.15},
    {"label": "1 bar delay", "delay_bars": 1, "slippage_pct": 0.0},
]


def run_latency_test(
    session: Session,
    config: BacktestConfig,
    source: Optional[str] = None,
    symbol_map: Optional[dict] = None,
    provider: Optional[MarketDataProvider] = None,
    latency_levels: Optional[list[dict]] = None,
) -> LatencyTestResult:
    """
    測試不同延遲下的績效變化。

    對每個延遲等級：
    1. 調整進場價（加滑點 or 延遲 K 線）
    2. 重新計算 risk（entry 變了，但 SL 不變）
    3. 跑回測
    4. 比較績效
    """
    from datetime import timedelta

    if latency_levels is None:
        latency_levels = DEFAULT_LATENCY_LEVELS

    # 讀取信號
    signals = (
        session.query(SignalORM)
        .filter(SignalORM.source == source if source else True)
        .order_by(SignalORM.signal_time.asc())
        .all()
    )

    if not signals:
        return LatencyTestResult(reasons=["no_signals"])

    levels: list[LatencyLevel] = []

    for level_cfg in latency_levels:
        label = level_cfg["label"]
        delay_bars = level_cfg["delay_bars"]
        slippage_pct = level_cfg["slippage_pct"]

        all_results: list[TradeResult] = []

        for signal in signals:
            # 載入 K 線
            since = signal.signal_time
            until = since + timedelta(hours=168)
            timeframe = signal.timeframe or "15m"

            try:
                candles = load_candles(
                    session, signal.symbol, timeframe, since, until,
                    provider=provider, symbol_map=symbol_map,
                )
            except Exception:
                continue

            if not candles:
                continue

            # 調整進場價模擬延遲
            adjusted_signal = _apply_latency(signal, delay_bars, slippage_pct, candles)
            if adjusted_signal is None:
                continue

            # 延遲 K 線偏移
            adjusted_candles = candles[delay_bars:] if delay_bars > 0 else candles

            result = simulate_trade(adjusted_signal, adjusted_candles, config)
            all_results.append(result)

        metrics = compute_metrics(all_results) if all_results else None

        levels.append(LatencyLevel(
            label=label,
            delay_bars=delay_bars,
            slippage_pct=slippage_pct,
            metrics=metrics,
            total_r=metrics.total_r if metrics else 0.0,
            expectancy=metrics.expectancy if metrics else 0.0,
        ))

    # 分析結果
    return _analyze_latency_results(levels)


def _apply_latency(
    signal: SignalORM,
    delay_bars: int,
    slippage_pct: float,
    candles: list[Candle],
) -> Optional[_FakeSignal]:
    """
    建立延遲調整後的信號副本。

    LONG: entry 往上調（追高買）
    SHORT: entry 往下調（追低賣）
    """
    entry = signal.entry
    sl = signal.sl

    # 滑點調整
    if slippage_pct > 0:
        if signal.side == SignalSide.LONG:
            entry = entry * (1 + slippage_pct / 100)
        else:
            entry = entry * (1 - slippage_pct / 100)

    # 延遲 K 線：用延遲後那根 K 線的 open 當進場價
    if delay_bars > 0 and delay_bars < len(candles):
        delayed_candle = candles[delay_bars]
        entry = delayed_candle.open

    # 檢查調整後是否合理
    if signal.side == SignalSide.LONG and entry <= sl:
        return None  # 延遲太大，進場價已低於 SL
    if signal.side == SignalSide.SHORT and entry >= sl:
        return None

    fake = _FakeSignal()
    fake.id = signal.id
    fake.side = signal.side
    fake.entry = entry
    fake.sl = sl
    fake.tp1 = signal.tp1
    fake.tp2 = signal.tp2
    fake.tp3 = signal.tp3
    fake.tp4 = signal.tp4
    fake.timeframe = signal.timeframe
    fake.signal_time = signal.signal_time
    return fake


class _FakeSignal:
    """輕量信號副本，避免修改 ORM 物件"""
    pass


def _analyze_latency_results(levels: list[LatencyLevel]) -> LatencyTestResult:
    """分析延遲測試結果，判定是否延遲敏感"""
    if not levels:
        return LatencyTestResult(reasons=["no_results"])

    reasons = []
    baseline = levels[0]  # 0s
    baseline_r = baseline.total_r

    # 找最大可承受延遲（expectancy 仍 > 0 的最後一級）
    max_viable = levels[0].label
    for lv in levels:
        if lv.expectancy > 0:
            max_viable = lv.label

    # 計算衰退
    worst = levels[-1]
    if baseline_r != 0:
        degradation = ((baseline_r - worst.total_r) / abs(baseline_r)) * 100
    else:
        degradation = 0

    # 判定是否延遲敏感
    sensitive = False

    # 規則 1：5s 延遲就讓 expectancy 從正變負
    if len(levels) >= 2 and baseline.expectancy > 0 and levels[1].expectancy <= 0:
        sensitive = True
        reasons.append(
            "extremely_sensitive: 5s delay kills the edge — "
            "this strategy requires sub-second execution, not viable for manual or normal bot trading"
        )

    # 規則 2：10s 延遲讓績效掉超過 50%
    if len(levels) >= 3 and baseline_r > 0:
        r_at_10s = levels[2].total_r
        drop = (baseline_r - r_at_10s) / baseline_r
        if drop > 0.5:
            sensitive = True
            reasons.append(
                f"highly_sensitive: 10s delay degrades performance by {drop:.0%} — "
                f"edge is fragile and execution-dependent"
            )

    # 規則 3：1 bar 延遲完全翻負
    bar_delay = [lv for lv in levels if lv.delay_bars >= 1]
    if bar_delay and baseline.expectancy > 0 and bar_delay[0].expectancy <= 0:
        if not sensitive:
            reasons.append(
                "bar_delay_sensitive: waiting 1 bar eliminates the edge — "
                "signals are time-critical"
            )
        # 不一定標記為 sensitive（1 bar 延遲對很多策略都有影響）

    if not sensitive and baseline.expectancy > 0:
        reasons.append(
            f"execution_robust: edge survives up to '{max_viable}' delay "
            f"(degradation: {degradation:.0f}%)"
        )

    # 結果摘要
    for lv in levels:
        reasons.append(
            f"  {lv.label}: total_r={lv.total_r:+.2f}, expectancy={lv.expectancy:+.4f}"
        )

    return LatencyTestResult(
        levels=levels,
        latency_sensitive=sensitive,
        max_viable_delay=max_viable,
        degradation_pct=round(degradation, 1),
        reasons=reasons,
    )

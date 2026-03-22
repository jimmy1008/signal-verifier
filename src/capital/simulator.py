"""
資金模擬層

職責：
- 將「單筆 R 值序列」轉換為「真實資金曲線」
- 模擬複利下的資金動態（不是靜態加總）
- 追蹤最大回撤、連敗、谷底、回血時間
- 判定策略在真實資金環境下是否可交易

核心觀點：
  期望值正 ≠ 能交易
  市場不是看你會不會賺錢，是看你能不能活到賺錢那天

輸入：list[TradeResult] (按時間排序)
輸出：CapitalResult
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field

from src.models import TradeResult

logger = logging.getLogger(__name__)


# ============================================================
# 資料模型
# ============================================================

class CapitalSnapshot(BaseModel):
    """單筆交易後的資金快照"""
    trade_index: int
    signal_id: int
    capital: float
    pnl_r: float
    pnl_amount: float
    drawdown_pct: float       # 當前從峰值回撤%
    peak: float
    losing_streak: int


class CapitalResult(BaseModel):
    """資金模擬完整結果"""
    initial_capital: float
    final_capital: float
    risk_per_trade: float

    # 報酬
    total_return_pct: float        # (final - initial) / initial
    total_return_amount: float

    # 回撤
    max_drawdown_pct: float        # 最大回撤百分比
    max_drawdown_amount: float     # 最大回撤金額
    max_drawdown_trade_index: int  # 最大回撤發生在第幾筆

    # 連敗
    max_losing_streak: int
    max_losing_streak_end_index: int  # 最長連敗結束位置

    # 谷底
    min_capital: float             # 最低資金
    min_capital_ratio: float       # min / initial（< 0.5 = 心理破產）
    min_capital_trade_index: int   # 谷底在第幾筆

    # 回血
    recovery_trades: Optional[int] = None  # 從最大回撤回到峰值需要幾筆
    recovery_possible: bool = True         # 在資料範圍內是否有回到峰值

    # 資金曲線
    equity_curve: list[float] = Field(default_factory=list)
    snapshots: list[CapitalSnapshot] = Field(default_factory=list)

    # 最終判定
    verdict: str = ""              # viable / untradeable / psychologically_untradeable / no_edge
    verdict_reasons: list[str] = Field(default_factory=list)


# ============================================================
# 資金模擬器
# ============================================================

class CapitalSimulator:
    """
    資金動態模擬器。

    每筆交易：
    - 風險金額 = 當前資金 × risk_per_trade
    - 損益 = pnl_r × 風險金額
    - 新資金 = 舊資金 + 損益

    這是複利模型：賺了多下，虧了少下。
    """

    def __init__(self, initial_capital: float = 1000.0, risk_per_trade: float = 0.01):
        self.initial_capital = initial_capital
        self.risk_per_trade = risk_per_trade

        self.capital = initial_capital
        self.peak = initial_capital
        self.min_capital = initial_capital

        self.equity_curve: list[float] = [initial_capital]
        self.snapshots: list[CapitalSnapshot] = []

        # 回撤追蹤
        self.max_drawdown_pct = 0.0
        self.max_drawdown_amount = 0.0
        self.max_drawdown_trade_index = 0

        # 連敗追蹤
        self.losing_streak = 0
        self.max_losing_streak = 0
        self.max_losing_streak_end_index = 0

        # 谷底追蹤
        self.min_capital_trade_index = 0

        # 回血追蹤
        self._dd_peak_index: Optional[int] = None  # 最大回撤開始的位置
        self._recovery_index: Optional[int] = None
        self._in_drawdown = False

        self._trade_count = 0

    def apply_trade(self, pnl_r: float, signal_id: int = 0) -> None:
        """套用一筆交易結果到資金"""
        # 計算損益
        risk_amount = self.capital * self.risk_per_trade
        pnl_amount = pnl_r * risk_amount
        self.capital += pnl_amount

        self._trade_count += 1

        # 更新峰值
        if self.capital > self.peak:
            self.peak = self.capital
            # 如果在追蹤回血，記錄回血點
            if self._in_drawdown and self._recovery_index is None:
                self._recovery_index = self._trade_count
                self._in_drawdown = False

        # 計算回撤
        if self.peak > 0:
            drawdown_pct = (self.peak - self.capital) / self.peak
        else:
            drawdown_pct = 0.0

        drawdown_amount = self.peak - self.capital

        if drawdown_pct > self.max_drawdown_pct:
            self.max_drawdown_pct = drawdown_pct
            self.max_drawdown_amount = drawdown_amount
            self.max_drawdown_trade_index = self._trade_count
            self._dd_peak_index = self._trade_count
            self._in_drawdown = True
            self._recovery_index = None  # 重置回血追蹤

        # 更新谷底
        if self.capital < self.min_capital:
            self.min_capital = self.capital
            self.min_capital_trade_index = self._trade_count

        # 連敗追蹤
        if pnl_r < 0:
            self.losing_streak += 1
            if self.losing_streak > self.max_losing_streak:
                self.max_losing_streak = self.losing_streak
                self.max_losing_streak_end_index = self._trade_count
        else:
            self.losing_streak = 0

        # 記錄
        self.equity_curve.append(self.capital)
        self.snapshots.append(CapitalSnapshot(
            trade_index=self._trade_count,
            signal_id=signal_id,
            capital=round(self.capital, 2),
            pnl_r=pnl_r,
            pnl_amount=round(pnl_amount, 2),
            drawdown_pct=round(drawdown_pct, 4),
            peak=round(self.peak, 2),
            losing_streak=self.losing_streak,
        ))

    def get_result(self) -> CapitalResult:
        """取得完整模擬結果"""
        total_return_amount = self.capital - self.initial_capital
        total_return_pct = total_return_amount / self.initial_capital if self.initial_capital > 0 else 0

        min_ratio = self.min_capital / self.initial_capital if self.initial_capital > 0 else 0

        # 回血計算
        recovery_trades = None
        recovery_possible = True
        if self._dd_peak_index is not None:
            if self._recovery_index is not None:
                recovery_trades = self._recovery_index - self._dd_peak_index
            else:
                recovery_possible = False

        # 最終判定
        verdict, reasons = capital_verdict(
            total_return_pct=total_return_pct,
            max_drawdown_pct=self.max_drawdown_pct,
            max_losing_streak=self.max_losing_streak,
            min_capital_ratio=min_ratio,
            recovery_trades=recovery_trades,
            recovery_possible=recovery_possible,
        )

        return CapitalResult(
            initial_capital=self.initial_capital,
            final_capital=round(self.capital, 2),
            risk_per_trade=self.risk_per_trade,
            total_return_pct=round(total_return_pct, 4),
            total_return_amount=round(total_return_amount, 2),
            max_drawdown_pct=round(self.max_drawdown_pct, 4),
            max_drawdown_amount=round(self.max_drawdown_amount, 2),
            max_drawdown_trade_index=self.max_drawdown_trade_index,
            max_losing_streak=self.max_losing_streak,
            max_losing_streak_end_index=self.max_losing_streak_end_index,
            min_capital=round(self.min_capital, 2),
            min_capital_ratio=round(min_ratio, 4),
            min_capital_trade_index=self.min_capital_trade_index,
            recovery_trades=recovery_trades,
            recovery_possible=recovery_possible,
            equity_curve=[round(x, 2) for x in self.equity_curve],
            snapshots=self.snapshots,
            verdict=verdict,
            verdict_reasons=reasons,
        )


# ============================================================
# 便利函式
# ============================================================

def run_simulation(
    trade_results: list[TradeResult],
    initial_capital: float = 1000.0,
    risk_per_trade: float = 0.01,
) -> CapitalResult:
    """
    跑完整資金模擬。

    Args:
        trade_results: 按時間排序的交易結果（只取 triggered 的）
        initial_capital: 初始資金
        risk_per_trade: 每筆風險比例（1% = 0.01）

    Returns:
        CapitalResult
    """
    sim = CapitalSimulator(initial_capital, risk_per_trade)

    triggered = [r for r in trade_results if r.triggered]
    for trade in triggered:
        sim.apply_trade(trade.pnl_r, signal_id=trade.signal_id)

    result = sim.get_result()
    logger.info(
        f"資金模擬完成: ${initial_capital} → ${result.final_capital} "
        f"({result.total_return_pct:+.1%}), "
        f"max DD {result.max_drawdown_pct:.1%}, "
        f"max losing streak {result.max_losing_streak}"
    )
    return result


def run_multi_risk_simulation(
    trade_results: list[TradeResult],
    initial_capital: float = 1000.0,
    risk_levels: Optional[list[float]] = None,
) -> dict[str, CapitalResult]:
    """
    用不同風險比例跑模擬，找出最適風險。

    Returns:
        {risk_label: CapitalResult}
    """
    if risk_levels is None:
        risk_levels = [0.005, 0.01, 0.015, 0.02, 0.03, 0.05]

    results = {}
    for risk in risk_levels:
        label = f"{risk:.1%}"
        results[label] = run_simulation(trade_results, initial_capital, risk)

    return results


# ============================================================
# 最終判定邏輯
# ============================================================

def capital_verdict(
    total_return_pct: float,
    max_drawdown_pct: float,
    max_losing_streak: int,
    min_capital_ratio: float,
    recovery_trades: Optional[int],
    recovery_possible: bool,
) -> tuple[str, list[str]]:
    """
    資金層最終判定。

    Returns:
        (verdict, reasons)
        verdict: "viable" / "untradeable" / "psychologically_untradeable" / "no_edge"
    """
    reasons = []

    # ── 完全沒賺 ──
    if total_return_pct <= 0:
        reasons.append(
            f"no_capital_growth: total return {total_return_pct:+.1%} — "
            f"this strategy does not grow capital"
        )
        return "no_edge", reasons

    # ── 回撤太深 ──
    if max_drawdown_pct > 0.5:
        reasons.append(
            f"catastrophic_drawdown: {max_drawdown_pct:.0%} max drawdown — "
            f"account would lose more than half its value, "
            f"virtually impossible to continue trading through this"
        )
        return "untradeable", reasons

    if max_drawdown_pct > 0.3:
        reasons.append(
            f"severe_drawdown: {max_drawdown_pct:.0%} max drawdown — "
            f"most traders cannot psychologically survive a 30%+ drawdown"
        )
        # 不直接 return，繼續檢查其他條件

    # ── 連敗太長 ──
    if max_losing_streak >= 15:
        reasons.append(
            f"extreme_losing_streak: {max_losing_streak} consecutive losses — "
            f"even with perfect risk management, this will break most traders"
        )
        return "psychologically_untradeable", reasons

    if max_losing_streak >= 10:
        reasons.append(
            f"long_losing_streak: {max_losing_streak} consecutive losses — "
            f"high psychological pressure, requires extreme discipline"
        )

    # ── 谷底太深 ──
    if min_capital_ratio < 0.5:
        reasons.append(
            f"capital_decay: account dropped to {min_capital_ratio:.0%} of initial capital — "
            f"psychological bankruptcy threshold"
        )
        if max_drawdown_pct > 0.3:
            return "untradeable", reasons

    # ── 回血太慢 ──
    if not recovery_possible:
        reasons.append(
            "no_recovery: account never recovered from max drawdown within the data period"
        )
        if max_drawdown_pct > 0.2:
            return "untradeable", reasons

    if recovery_trades is not None and recovery_trades > 50:
        reasons.append(
            f"slow_recovery: took {recovery_trades} trades to recover from max drawdown — "
            f"most traders will abandon the strategy long before recovery"
        )

    # ── 最終判定 ──
    if max_drawdown_pct > 0.3:
        return "untradeable", reasons

    if max_losing_streak >= 10 and max_drawdown_pct > 0.2:
        return "psychologically_untradeable", reasons

    if not reasons:
        reasons.append(
            f"strategy is viable: {total_return_pct:+.1%} return, "
            f"{max_drawdown_pct:.0%} max DD, "
            f"max {max_losing_streak} consecutive losses"
        )

    return "viable", reasons

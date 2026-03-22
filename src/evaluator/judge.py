"""
Evaluation Layer — 最終判定模組

職責：
- 綜合所有統計指標，判定頻道是否有 edge
- 偵測常見陷阱（高勝率低RR、TP1假象、不穩定策略）
- 執行策略穩定性測試（多規則交叉驗證）
- 輸出結構化判定結果 + 信心度

輸入：PerformanceMetrics, list[TradeResult], SignalORM list, BacktestConfig
輸出：EdgeVerdict
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.models import (
    PerformanceMetrics,
    TradeResult,
    SignalORM,
    BacktestConfig,
    AmbiguousMode,
)
from src.backtest.runner import run_backtest
from src.stats.metrics import compute_metrics

logger = logging.getLogger(__name__)


# ============================================================
# 判定結果模型
# ============================================================

class EdgeVerdict(BaseModel):
    """最終 Edge 判定"""
    has_edge: bool = False
    tradeable: bool = False                  # 數學上有 edge + 資金上可執行
    confidence: float = 0.0                  # 0.0 ~ 1.0
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    stability: Optional[StabilityResult] = None
    capital: Optional[CapitalVerdict] = None
    details: dict = Field(default_factory=dict)


class CapitalVerdict(BaseModel):
    """資金層判定結果"""
    verdict: str = ""                        # viable / untradeable / psychologically_untradeable / no_edge
    final_capital: float = 0.0
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    max_losing_streak: int = 0
    min_capital_ratio: float = 0.0           # min / initial
    recovery_trades: Optional[int] = None
    reasons: list[str] = Field(default_factory=list)


class StabilityResult(BaseModel):
    """策略穩定性測試結果"""
    unstable_strategy: bool = False
    mode_results: dict[str, float] = Field(default_factory=dict)  # mode → total_r
    profitable_modes: int = 0
    total_modes: int = 0
    reasons: list[str] = Field(default_factory=list)


# 因為 EdgeVerdict 引用 StabilityResult / CapitalVerdict，需要 rebuild
EdgeVerdict.model_rebuild()


# ============================================================
# 核心判定邏輯
# ============================================================

def evaluate_edge(metrics: PerformanceMetrics) -> EdgeVerdict:
    """
    根據績效指標判定是否有 edge。

    判定規則：
    1. 期望值 <= 0 → 無 edge
    2. 高勝率 + 低 RR → 高勝率陷阱
    3. TP1 觸達高但整體虧損 → TP1 假象
    4. 樣本太少 → 降低信心
    5. 回撤過大 → 警告
    6. 碰 TP1 後打 SL 比例過高 → 警告
    """
    reasons = []
    warnings = []
    confidence = 1.0
    has_edge = True

    # ── 樣本量檢查 ──
    if metrics.triggered_count < 10:
        warnings.append(f"sample_too_small: only {metrics.triggered_count} triggered trades")
        confidence *= 0.4
    elif metrics.triggered_count < 30:
        warnings.append(f"sample_limited: {metrics.triggered_count} trades (need 30+ for reliability)")
        confidence *= 0.7

    # ── Rule 1: 期望值 ──
    if metrics.expectancy <= 0:
        has_edge = False
        reasons.append(f"negative_expectancy: {metrics.expectancy:+.4f}R per trade")

    # ── Rule 2: 高勝率陷阱 ──
    if metrics.win_rate > 0.7 and metrics.avg_rr < 0.5:
        has_edge = False
        reasons.append(
            f"high_winrate_trap: {metrics.win_rate:.0%} winrate but avg RR only {metrics.avg_rr:.2f} "
            f"(avg win {metrics.avg_win_r:+.2f}R vs avg loss {metrics.avg_loss_r:+.2f}R)"
        )

    # ── Rule 3: TP1 假象 ──
    if metrics.tp1_hit_rate > 0.6 and metrics.total_r < 0:
        has_edge = False
        reasons.append(
            f"tp1_illusion: TP1 hit rate {metrics.tp1_hit_rate:.0%} looks great, "
            f"but total R is {metrics.total_r:+.2f} — the channel looks good on paper but loses money"
        )

    # ── Rule 4: 碰 TP1 後打 SL 比例 ──
    if metrics.tp1_hit_then_sl_rate > 0.4:
        warnings.append(
            f"tp1_reversal_risk: {metrics.tp1_hit_then_sl_rate:.0%} of trades hit TP1 then reversed to SL"
        )
        confidence *= 0.85

    # ── Rule 5: 回撤警告 ──
    if metrics.total_r > 0 and metrics.max_drawdown_r > metrics.total_r * 0.8:
        warnings.append(
            f"high_drawdown: max drawdown {metrics.max_drawdown_r:.2f}R is {metrics.max_drawdown_r/metrics.total_r:.0%} of total profit"
        )
        confidence *= 0.8

    # ── Rule 6: 勝率太低 ──
    if metrics.win_rate < 0.3 and metrics.avg_rr < 2.0:
        has_edge = False
        reasons.append(
            f"low_winrate_low_rr: {metrics.win_rate:.0%} winrate with only {metrics.avg_rr:.2f} RR is not viable"
        )

    # ── Rule 7: 期望值正但很微弱 ──
    if 0 < metrics.expectancy < 0.05:
        warnings.append(
            f"marginal_edge: expectancy {metrics.expectancy:+.4f}R is barely positive — "
            f"likely consumed by slippage and fees in live trading"
        )
        confidence *= 0.7

    # ── 信心度調整 ──
    if has_edge and metrics.expectancy > 0.2:
        confidence = min(confidence * 1.1, 1.0)
    if has_edge and metrics.triggered_count >= 100:
        confidence = min(confidence * 1.1, 1.0)

    confidence = round(max(0.0, min(1.0, confidence)), 2)

    return EdgeVerdict(
        has_edge=has_edge,
        confidence=confidence,
        reasons=reasons,
        warnings=warnings,
        details={
            "expectancy": metrics.expectancy,
            "win_rate": metrics.win_rate,
            "avg_rr": metrics.avg_rr,
            "total_r": metrics.total_r,
            "max_drawdown_r": metrics.max_drawdown_r,
            "tp1_hit_rate": metrics.tp1_hit_rate,
            "tp1_then_sl_rate": metrics.tp1_hit_then_sl_rate,
            "sample_size": metrics.triggered_count,
        },
    )


# ============================================================
# 策略穩定性測試
# ============================================================

STABILITY_CONFIGS = [
    BacktestConfig(name="tp1_exit", mode="single_tp", target_tp="tp1"),
    BacktestConfig(name="tp2_exit", mode="single_tp", target_tp="tp2"),
    BacktestConfig(name="tp3_exit", mode="single_tp", target_tp="tp3"),
    BacktestConfig(name="partial_tp", mode="partial_tp", target_tp="tp4"),
    BacktestConfig(name="breakeven", mode="breakeven", target_tp="tp2", move_sl_after="tp1"),
]


def run_stability_test(
    session: Session,
    source: Optional[str] = None,
    symbol_map: Optional[dict] = None,
    configs: Optional[list[BacktestConfig]] = None,
) -> StabilityResult:
    """
    用多種出場規則跑同一組信號，判定策略是否穩定。

    規則：
    - 5 種模式中至少 3 種盈利 → stable
    - 只有 1 種賺其它都賠 → unstable
    - 全部都賠 → 無 edge
    """
    if configs is None:
        configs = STABILITY_CONFIGS

    mode_results: dict[str, float] = {}
    reasons = []

    for cfg in configs:
        try:
            results = run_backtest(session, cfg, source=source, symbol_map=symbol_map)
            m = compute_metrics(results)
            mode_results[cfg.name] = m.total_r
        except Exception as e:
            logger.error(f"Stability test failed for {cfg.name}: {e}")
            mode_results[cfg.name] = 0.0

    profitable = sum(1 for v in mode_results.values() if v > 0)
    total = len(mode_results)

    unstable = False
    if profitable <= 1 and total >= 3:
        unstable = True
        if profitable == 0:
            reasons.append("all_modes_negative: no exit rule produces profit")
        else:
            winner = [k for k, v in mode_results.items() if v > 0][0]
            reasons.append(
                f"single_mode_dependent: only '{winner}' is profitable — "
                f"edge is likely an artifact of that specific exit rule, not a real signal quality"
            )

    if profitable >= 3:
        reasons.append(f"robust: {profitable}/{total} exit modes are profitable")

    return StabilityResult(
        unstable_strategy=unstable,
        mode_results=mode_results,
        profitable_modes=profitable,
        total_modes=total,
        reasons=reasons,
    )


# ============================================================
# 完整判定流程
# ============================================================

def full_evaluation(
    session: Session,
    source: Optional[str] = None,
    symbol_map: Optional[dict] = None,
    run_stability: bool = True,
    initial_capital: float = 1000.0,
    risk_per_trade: float = 0.01,
) -> EdgeVerdict:
    """
    完整 Edge 判定：基礎指標 + 資金模擬 + 穩定性測試。

    三層判定：
    1. 數學層：期望值、勝率、RR → has_edge
    2. 資金層：回撤、連敗、谷底、回血 → tradeable
    3. 穩定層：多規則交叉驗證 → stability

    Args:
        session: DB session
        source: 頻道來源
        symbol_map: symbol 映射
        run_stability: 是否執行穩定性測試
        initial_capital: 初始資金
        risk_per_trade: 每筆風險比例

    Returns:
        EdgeVerdict 完整判定結果
    """
    from src.capital.simulator import run_simulation, CapitalResult

    # 先用預設設定跑一次回測
    base_config = BacktestConfig(name="evaluation_base", mode="single_tp", target_tp="tp2")
    results = run_backtest(session, base_config, source=source, symbol_map=symbol_map)
    metrics = compute_metrics(results)

    # ── Layer 1: 數學層判定 ──
    verdict = evaluate_edge(metrics)

    # ── Layer 2: 資金層判定 ──
    cap_result = run_simulation(results, initial_capital, risk_per_trade)

    capital_v = CapitalVerdict(
        verdict=cap_result.verdict,
        final_capital=cap_result.final_capital,
        total_return_pct=cap_result.total_return_pct,
        max_drawdown_pct=cap_result.max_drawdown_pct,
        max_losing_streak=cap_result.max_losing_streak,
        min_capital_ratio=cap_result.min_capital_ratio,
        recovery_trades=cap_result.recovery_trades,
        reasons=cap_result.verdict_reasons,
    )
    verdict.capital = capital_v

    # 資金層否決
    if cap_result.verdict == "no_edge":
        verdict.has_edge = False
        verdict.tradeable = False
        verdict.reasons.append("capital_no_growth: " + "; ".join(cap_result.verdict_reasons))

    elif cap_result.verdict == "untradeable":
        verdict.tradeable = False
        verdict.warnings.append("untradeable_drawdown: " + "; ".join(cap_result.verdict_reasons))

    elif cap_result.verdict == "psychologically_untradeable":
        verdict.tradeable = False
        verdict.warnings.append("psychological_break_risk: " + "; ".join(cap_result.verdict_reasons))

    elif cap_result.verdict == "viable" and verdict.has_edge:
        verdict.tradeable = True

    # 補充資金層細節到 details
    verdict.details["capital"] = {
        "initial": initial_capital,
        "final": cap_result.final_capital,
        "return_pct": cap_result.total_return_pct,
        "max_drawdown_pct": cap_result.max_drawdown_pct,
        "max_losing_streak": cap_result.max_losing_streak,
        "min_capital_ratio": cap_result.min_capital_ratio,
        "recovery_trades": cap_result.recovery_trades,
        "verdict": cap_result.verdict,
    }

    # ── Layer 3: 穩定性測試 ──
    if run_stability and metrics.triggered_count >= 10:
        stability = run_stability_test(session, source=source, symbol_map=symbol_map)
        verdict.stability = stability

        if stability.unstable_strategy:
            verdict.has_edge = False
            verdict.tradeable = False
            verdict.reasons.append("unstable_strategy: " + "; ".join(stability.reasons))
            verdict.confidence *= 0.5
            verdict.confidence = round(verdict.confidence, 2)

    return verdict

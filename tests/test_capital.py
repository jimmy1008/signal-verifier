"""資金模擬器單元測試"""

from src.capital.simulator import (
    CapitalSimulator,
    run_simulation,
    capital_verdict,
)
from src.models import TradeResult, ExitReason
from datetime import datetime


def test_basic_simulation():
    """基本複利計算"""
    sim = CapitalSimulator(initial_capital=1000, risk_per_trade=0.01)

    # 贏 2R：1000 * 0.01 * 2 = +20 → 1020
    sim.apply_trade(2.0)
    assert abs(sim.capital - 1020.0) < 0.01

    # 輸 1R：1020 * 0.01 * -1 = -10.2 → 1009.8
    sim.apply_trade(-1.0)
    assert abs(sim.capital - 1009.8) < 0.01


def test_drawdown_tracking():
    """回撤追蹤"""
    sim = CapitalSimulator(initial_capital=1000, risk_per_trade=0.05)

    sim.apply_trade(2.0)   # 1000 → 1100 (peak)
    sim.apply_trade(-1.0)  # 1100 → 1045 (dd = 55/1100 = 5%)
    sim.apply_trade(-1.0)  # 1045 → 992.75

    result = sim.get_result()
    assert result.max_drawdown_pct > 0.05  # 至少 5% 回撤


def test_losing_streak():
    """連敗追蹤"""
    sim = CapitalSimulator(initial_capital=1000, risk_per_trade=0.01)

    for _ in range(5):
        sim.apply_trade(-1.0)
    sim.apply_trade(1.0)
    for _ in range(3):
        sim.apply_trade(-1.0)

    result = sim.get_result()
    assert result.max_losing_streak == 5


def test_min_capital():
    """谷底追蹤"""
    sim = CapitalSimulator(initial_capital=1000, risk_per_trade=0.02)

    # 連虧 10 筆
    for _ in range(10):
        sim.apply_trade(-1.0)

    # 然後回來
    for _ in range(20):
        sim.apply_trade(2.0)

    result = sim.get_result()
    assert result.min_capital < 1000
    assert result.min_capital_ratio < 1.0
    assert result.min_capital_trade_index == 10


def test_recovery_tracking():
    """回血追蹤"""
    sim = CapitalSimulator(initial_capital=1000, risk_per_trade=0.01)

    sim.apply_trade(2.0)   # peak
    sim.apply_trade(-1.0)  # drawdown
    sim.apply_trade(-1.0)  # deeper
    sim.apply_trade(3.0)   # recovery
    sim.apply_trade(2.0)   # new peak

    result = sim.get_result()
    assert result.recovery_possible is True
    assert result.recovery_trades is not None


def test_run_simulation():
    """整合測試 run_simulation"""
    results = [
        TradeResult(signal_id=1, triggered=True, pnl_r=2.0,
                    entry_time=datetime(2026, 1, 1), exit_time=datetime(2026, 1, 2)),
        TradeResult(signal_id=2, triggered=True, pnl_r=-1.0,
                    entry_time=datetime(2026, 1, 3), exit_time=datetime(2026, 1, 4)),
        TradeResult(signal_id=3, triggered=True, pnl_r=1.5,
                    entry_time=datetime(2026, 1, 5), exit_time=datetime(2026, 1, 6)),
        TradeResult(signal_id=4, triggered=False),  # 應被跳過
    ]

    cap = run_simulation(results, initial_capital=1000, risk_per_trade=0.01)

    assert cap.final_capital > 1000  # 整體正收益
    assert len(cap.equity_curve) == 4  # initial + 3 triggered trades
    assert cap.verdict == "viable"


def test_verdict_no_edge():
    v, reasons = capital_verdict(
        total_return_pct=-0.05,
        max_drawdown_pct=0.1,
        max_losing_streak=3,
        min_capital_ratio=0.95,
        recovery_trades=None,
        recovery_possible=False,
    )
    assert v == "no_edge"


def test_verdict_untradeable():
    v, reasons = capital_verdict(
        total_return_pct=0.1,
        max_drawdown_pct=0.55,
        max_losing_streak=5,
        min_capital_ratio=0.45,
        recovery_trades=30,
        recovery_possible=True,
    )
    assert v == "untradeable"


def test_verdict_psychological():
    v, reasons = capital_verdict(
        total_return_pct=0.05,
        max_drawdown_pct=0.15,
        max_losing_streak=16,
        min_capital_ratio=0.85,
        recovery_trades=20,
        recovery_possible=True,
    )
    assert v == "psychologically_untradeable"


def test_verdict_viable():
    v, reasons = capital_verdict(
        total_return_pct=0.2,
        max_drawdown_pct=0.12,
        max_losing_streak=5,
        min_capital_ratio=0.88,
        recovery_trades=8,
        recovery_possible=True,
    )
    assert v == "viable"


def test_compound_effect():
    """驗證複利效果：連續虧損比單筆計算更痛"""
    sim = CapitalSimulator(initial_capital=1000, risk_per_trade=0.02)

    # 連虧 20 筆
    for _ in range(20):
        sim.apply_trade(-1.0)

    result = sim.get_result()

    # 靜態計算：20 * 1000 * 0.02 = 400 → 剩 600
    # 複利計算：1000 * (1 - 0.02)^20 = 1000 * 0.6676 = 667.6
    # 複利虧損比靜態少（因為越虧越少下）
    assert result.final_capital > 600  # 複利保護
    assert result.final_capital < 700
    assert result.min_capital_ratio < 0.7

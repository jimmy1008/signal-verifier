"""Evaluator 單元測試"""

from src.evaluator.judge import evaluate_edge
from src.models import PerformanceMetrics


def test_positive_edge():
    m = PerformanceMetrics(
        total_signals=50, triggered_count=40, not_triggered_count=10,
        win_count=24, loss_count=16,
        win_rate=0.6, loss_rate=0.4,
        avg_win_r=1.5, avg_loss_r=-1.0, avg_rr=1.5,
        expectancy=0.5, total_r=20.0, max_drawdown_r=5.0,
        tp1_hit_rate=0.7, tp2_hit_rate=0.5, tp3_hit_rate=0.3, tp4_hit_rate=0.1,
        tp1_hit_then_sl_rate=0.2,
    )
    v = evaluate_edge(m)
    assert v.has_edge is True
    assert v.confidence > 0.5


def test_negative_expectancy():
    m = PerformanceMetrics(
        total_signals=50, triggered_count=40,
        win_rate=0.4, loss_rate=0.6,
        avg_win_r=1.0, avg_loss_r=-1.2,
        expectancy=-0.32, total_r=-12.8,
    )
    v = evaluate_edge(m)
    assert v.has_edge is False
    assert any("negative_expectancy" in r for r in v.reasons)


def test_high_winrate_trap():
    m = PerformanceMetrics(
        total_signals=100, triggered_count=80,
        win_rate=0.8, loss_rate=0.2,
        avg_win_r=0.3, avg_loss_r=-2.0, avg_rr=0.15,
        expectancy=-0.16, total_r=-12.8,
    )
    v = evaluate_edge(m)
    assert v.has_edge is False
    assert any("high_winrate_trap" in r for r in v.reasons)


def test_tp1_illusion():
    m = PerformanceMetrics(
        total_signals=60, triggered_count=50,
        win_rate=0.5, loss_rate=0.5,
        avg_win_r=0.8, avg_loss_r=-1.0, avg_rr=0.8,
        expectancy=-0.1, total_r=-5.0,
        tp1_hit_rate=0.75,
    )
    v = evaluate_edge(m)
    assert v.has_edge is False
    assert any("tp1_illusion" in r for r in v.reasons)


def test_small_sample_warning():
    m = PerformanceMetrics(
        total_signals=8, triggered_count=5,
        win_rate=0.8, loss_rate=0.2,
        avg_win_r=2.0, avg_loss_r=-1.0, avg_rr=2.0,
        expectancy=1.4, total_r=7.0,
    )
    v = evaluate_edge(m)
    assert v.has_edge is True
    assert v.confidence < 0.6  # 樣本太少，信心低
    assert any("sample_too_small" in w for w in v.warnings)


def test_marginal_edge_warning():
    m = PerformanceMetrics(
        total_signals=100, triggered_count=80,
        win_rate=0.52, loss_rate=0.48,
        avg_win_r=1.05, avg_loss_r=-1.0, avg_rr=1.05,
        expectancy=0.03, total_r=2.4,
    )
    v = evaluate_edge(m)
    assert any("marginal_edge" in w for w in v.warnings)

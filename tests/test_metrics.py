"""績效統計單元測試"""

from src.stats.metrics import compute_metrics, _compute_max_drawdown, _compute_streaks
from src.models import TradeResult, ExitReason
from datetime import datetime


def test_basic_metrics():
    results = [
        TradeResult(signal_id=1, triggered=True, pnl_r=2.0, exit_reason=ExitReason.TP_HIT, max_tp_hit=2,
                    entry_time=datetime(2026, 1, 1), exit_time=datetime(2026, 1, 2)),
        TradeResult(signal_id=2, triggered=True, pnl_r=-1.0, exit_reason=ExitReason.SL_HIT, max_tp_hit=0,
                    entry_time=datetime(2026, 1, 3), exit_time=datetime(2026, 1, 4)),
        TradeResult(signal_id=3, triggered=True, pnl_r=1.5, exit_reason=ExitReason.TP_HIT, max_tp_hit=1,
                    entry_time=datetime(2026, 1, 5), exit_time=datetime(2026, 1, 6)),
        TradeResult(signal_id=4, triggered=False),
    ]
    m = compute_metrics(results)
    assert m.total_signals == 4
    assert m.triggered_count == 3
    assert m.not_triggered_count == 1
    assert m.win_count == 2
    assert m.loss_count == 1
    assert abs(m.win_rate - 2/3) < 0.01
    assert m.total_r == 2.5  # 2 + (-1) + 1.5


def test_max_drawdown():
    # equity: 0 → 2 → 1 → 2.5
    # peak 2, dd 1 at step 2
    dd = _compute_max_drawdown([2.0, -1.0, 1.5])
    assert dd == 1.0


def test_streaks():
    results = [
        TradeResult(signal_id=1, triggered=True, pnl_r=1.0),
        TradeResult(signal_id=2, triggered=True, pnl_r=1.0),
        TradeResult(signal_id=3, triggered=True, pnl_r=1.0),
        TradeResult(signal_id=4, triggered=True, pnl_r=-1.0),
        TradeResult(signal_id=5, triggered=True, pnl_r=-1.0),
    ]
    wins, losses = _compute_streaks(results)
    assert wins == 3
    assert losses == 2

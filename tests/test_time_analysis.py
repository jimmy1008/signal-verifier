"""時間切片分析單元測試"""

from datetime import datetime
from src.stats.time_analysis import get_session_name, analyze_by_session, SessionName
from src.models import TradeResult, ExitReason


def test_session_classification():
    assert get_session_name(0) == SessionName.ASIA
    assert get_session_name(3) == SessionName.ASIA
    assert get_session_name(7) == SessionName.ASIA
    assert get_session_name(8) == SessionName.EUROPE
    assert get_session_name(13) == SessionName.EUROPE
    assert get_session_name(14) == SessionName.US
    assert get_session_name(20) == SessionName.US
    assert get_session_name(21) == SessionName.CROSS
    assert get_session_name(23) == SessionName.CROSS


def test_analyze_by_session():
    # 建立假信號（用 SimpleNamespace）
    class FakeSig:
        def __init__(self, id, hour):
            self.id = id
            self.signal_time = datetime(2026, 1, 1, hour, 0, 0)

    signals = {
        1: FakeSig(1, 2),   # Asia
        2: FakeSig(2, 3),   # Asia
        3: FakeSig(3, 5),   # Asia
        4: FakeSig(4, 10),  # Europe
        5: FakeSig(5, 11),  # Europe
        6: FakeSig(6, 12),  # Europe
        7: FakeSig(7, 15),  # US
        8: FakeSig(8, 16),  # US
        9: FakeSig(9, 17),  # US
    }

    results = [
        TradeResult(signal_id=1, triggered=True, pnl_r=1.0, exit_reason=ExitReason.TP_HIT,
                    entry_time=datetime(2026, 1, 1, 2), exit_time=datetime(2026, 1, 1, 4)),
        TradeResult(signal_id=2, triggered=True, pnl_r=1.5, exit_reason=ExitReason.TP_HIT,
                    entry_time=datetime(2026, 1, 1, 3), exit_time=datetime(2026, 1, 1, 5)),
        TradeResult(signal_id=3, triggered=True, pnl_r=-1.0, exit_reason=ExitReason.SL_HIT,
                    entry_time=datetime(2026, 1, 1, 5), exit_time=datetime(2026, 1, 1, 7)),
        TradeResult(signal_id=4, triggered=True, pnl_r=-1.0, exit_reason=ExitReason.SL_HIT,
                    entry_time=datetime(2026, 1, 1, 10), exit_time=datetime(2026, 1, 1, 12)),
        TradeResult(signal_id=5, triggered=True, pnl_r=-1.0, exit_reason=ExitReason.SL_HIT,
                    entry_time=datetime(2026, 1, 1, 11), exit_time=datetime(2026, 1, 1, 13)),
        TradeResult(signal_id=6, triggered=True, pnl_r=-1.0, exit_reason=ExitReason.SL_HIT,
                    entry_time=datetime(2026, 1, 1, 12), exit_time=datetime(2026, 1, 1, 14)),
        TradeResult(signal_id=7, triggered=True, pnl_r=2.0, exit_reason=ExitReason.TP_HIT,
                    entry_time=datetime(2026, 1, 1, 15), exit_time=datetime(2026, 1, 1, 17)),
        TradeResult(signal_id=8, triggered=True, pnl_r=1.0, exit_reason=ExitReason.TP_HIT,
                    entry_time=datetime(2026, 1, 1, 16), exit_time=datetime(2026, 1, 1, 18)),
        TradeResult(signal_id=9, triggered=True, pnl_r=1.5, exit_reason=ExitReason.TP_HIT,
                    entry_time=datetime(2026, 1, 1, 17), exit_time=datetime(2026, 1, 1, 19)),
    ]

    result = analyze_by_session(results, signals)

    # Asia: 3 trades, 2 win 1 loss
    asia = result.sessions[SessionName.ASIA]
    assert asia.trade_count == 3
    assert asia.metrics.win_rate > 0.5

    # Europe: 3 trades, all loss
    europe = result.sessions[SessionName.EUROPE]
    assert europe.trade_count == 3
    assert europe.metrics.win_rate == 0.0

    # US: 3 trades, all win
    us = result.sessions[SessionName.US]
    assert us.trade_count == 3
    assert us.metrics.win_rate == 1.0

    assert result.best_session == SessionName.US
    assert result.worst_session == SessionName.EUROPE

"""回測引擎單元測試"""

from datetime import datetime, timedelta
from src.backtest.engine import simulate_trade, _price_touched
from src.models import Candle, BacktestConfig, AmbiguousMode, SignalSide, ExitReason


def make_signal(entry, sl, tp1=None, tp2=None, tp3=None, tp4=None, side=SignalSide.LONG):
    """建立模擬 SignalORM（用 SimpleNamespace 替代）"""
    class FakeSignal:
        pass
    s = FakeSignal()
    s.id = 1
    s.side = side
    s.entry = entry
    s.sl = sl
    s.tp1 = tp1
    s.tp2 = tp2
    s.tp3 = tp3
    s.tp4 = tp4
    s.timeframe = "15m"
    s.signal_time = datetime(2026, 1, 1)
    return s


def make_candles(ohlc_list, start=None):
    """從 (open, high, low, close) 列表建立 K 線"""
    if start is None:
        start = datetime(2026, 1, 1)
    candles = []
    for i, (o, h, l, c) in enumerate(ohlc_list):
        candles.append(Candle(
            symbol="BTC/USDT", timeframe="15m",
            open_time=start + timedelta(minutes=15 * i),
            open=o, high=h, low=l, close=c,
        ))
    return candles


def test_long_tp_hit():
    signal = make_signal(entry=100, sl=95, tp1=105, tp2=110)
    candles = make_candles([
        (99, 101, 98, 100),   # 觸及 entry
        (100, 103, 99, 102),  # 沒到 TP2
        (102, 111, 101, 110), # 觸及 TP2
    ])
    config = BacktestConfig(mode="single_tp", target_tp="tp2")
    result = simulate_trade(signal, candles, config)
    assert result.triggered
    assert result.exit_reason == ExitReason.TP_HIT
    assert result.pnl_r == 2.0  # (110-100)/(100-95) = 2R


def test_long_sl_hit():
    signal = make_signal(entry=100, sl=95, tp1=105, tp2=110)
    candles = make_candles([
        (99, 101, 98, 100),   # 觸及 entry
        (100, 101, 94, 96),   # 觸及 SL
    ])
    config = BacktestConfig(mode="single_tp", target_tp="tp2")
    result = simulate_trade(signal, candles, config)
    assert result.triggered
    assert result.exit_reason == ExitReason.SL_HIT
    assert result.pnl_r == -1.0


def test_not_triggered():
    signal = make_signal(entry=100, sl=95, tp1=105, tp2=110)
    candles = make_candles([
        (90, 92, 88, 91),  # 價格遠低於 entry
        (91, 93, 89, 92),
    ])
    config = BacktestConfig(signal_expiry_bars=2)
    result = simulate_trade(signal, candles, config)
    assert not result.triggered


def test_ambiguous_conservative():
    """同 K 線碰到 TP 和 SL → 保守算 SL"""
    signal = make_signal(entry=100, sl=95, tp2=110)
    candles = make_candles([
        (99, 101, 98, 100),    # 觸及 entry
        (100, 115, 90, 105),   # 同時碰 SL 和 TP2
    ])
    config = BacktestConfig(mode="single_tp", target_tp="tp2", ambiguous_mode=AmbiguousMode.CONSERVATIVE)
    result = simulate_trade(signal, candles, config)
    assert result.exit_reason == ExitReason.SL_HIT


def test_ambiguous_optimistic():
    """同 K 線碰到 TP 和 SL → 樂觀算 TP"""
    signal = make_signal(entry=100, sl=95, tp2=110)
    candles = make_candles([
        (99, 101, 98, 100),
        (100, 115, 90, 105),
    ])
    config = BacktestConfig(mode="single_tp", target_tp="tp2", ambiguous_mode=AmbiguousMode.OPTIMISTIC)
    result = simulate_trade(signal, candles, config)
    assert result.exit_reason == ExitReason.TP_HIT


def test_breakeven_mode():
    """到 TP1 後 SL 移到 entry，之後回來打 BE"""
    signal = make_signal(entry=100, sl=95, tp1=105, tp2=110)
    candles = make_candles([
        (99, 101, 98, 100),    # 觸及 entry
        (100, 106, 99, 105),   # 觸及 TP1 → SL 移到 100
        (105, 106, 99, 100),   # 回來打到 BE (100)
    ])
    config = BacktestConfig(mode="breakeven", target_tp="tp2", move_sl_after="tp1")
    result = simulate_trade(signal, candles, config)
    assert result.exit_reason == ExitReason.BREAKEVEN
    assert result.pnl_r == 0.0

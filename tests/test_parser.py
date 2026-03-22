"""Parser 單元測試"""

from datetime import datetime
from src.parsers.default_parser import DefaultParser
from src.models import SignalSide, UpdateType


parser = DefaultParser()
now = datetime(2026, 3, 19, 12, 0, 0)


def test_basic_signal():
    text = """BTCUSDT LONG
Entry: 65000
SL: 64000
TP1: 66000
TP2: 67000
TP3: 68000
TP4: 70000"""
    result = parser.parse(text, now)
    assert result is not None
    assert result.symbol == "BTCUSDT"
    assert result.side == SignalSide.LONG
    assert result.entry == 65000
    assert result.sl == 64000
    assert result.tp1 == 66000
    assert result.tp2 == 67000
    assert result.tp3 == 68000
    assert result.tp4 == 70000


def test_short_signal():
    text = """ETH/USDT SHORT
Entry: 3500
Stop Loss: 3600
TP1: 3400
TP2: 3300"""
    result = parser.parse(text, now)
    assert result is not None
    assert result.side == SignalSide.SHORT
    assert result.entry == 3500
    assert result.sl == 3600
    assert result.tp1 == 3400


def test_tp_hit_update():
    text = "TP1 reached ✅"
    result = parser.parse(text, now)
    assert result is not None
    assert result.signal_type == "update"
    assert result.update_type == UpdateType.TP_HIT
    assert result.update_value == "tp1"


def test_cancel_signal():
    text = "Signal cancelled ❌"
    result = parser.parse(text, now)
    assert result is not None
    assert result.signal_type == "cancel"
    assert result.update_type == UpdateType.CANCEL


def test_non_signal_message():
    text = "Good morning everyone!"
    result = parser.parse(text, now)
    assert result is None


def test_sl_move():
    text = "SL moved to entry"
    result = parser.parse(text, now)
    assert result is not None
    assert result.update_type == UpdateType.SL_MOVED
    assert result.update_value == "entry"

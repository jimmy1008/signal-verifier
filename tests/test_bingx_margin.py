"""BingX 保證金釋放重試機制測試"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.trader.bingx import BingXBroker, to_bingx_symbol

pytestmark = pytest.mark.anyio


# ── Fixtures ────────────────────────────────────────

@pytest.fixture
def broker():
    """建立 mock BingXBroker（不連真實交易所）"""
    with patch.object(BingXBroker, "__init__", lambda self, *a, **kw: None):
        b = BingXBroker.__new__(BingXBroker)
        b.exchange = AsyncMock()
        b.leverage = 20
        b.margin_mode = "cross"
        b.is_demo = True
        b._configured_symbols = set()
        b._market_cache = {}
        b._last_protection_failure = None
        return b


def _make_positions(symbols_leverages: list[tuple[str, int]]):
    """產生模擬持倉列表"""
    return [
        {"symbol": sym, "leverage": lev, "contracts": 10.0, "side": "short",
         "entryPrice": 100, "markPrice": 99, "unrealizedPnl": 1.0, "id": f"pos_{i}"}
        for i, (sym, lev) in enumerate(symbols_leverages)
    ]


# ── to_bingx_symbol ────────────────────────────────

def test_symbol_crypto():
    assert to_bingx_symbol("BTCUSDT.P") == "BTC-USDT"
    assert to_bingx_symbol("ETHUSDT.P") == "ETH-USDT"
    assert to_bingx_symbol("XRPUSDT") == "XRP-USDT"


def test_symbol_forex():
    assert to_bingx_symbol("XAUUSD") == "XAU-USD"
    assert to_bingx_symbol("EURUSD") == "EUR-USD"


def test_symbol_passthrough():
    assert to_bingx_symbol("BTC-USDT") == "BTC-USDT"


# ── _free_margin_and_retry ─────────────────────────

async def test_retry_succeeds(broker):
    """調到 150x 後重試成功"""
    broker.exchange.fetch_positions = AsyncMock(return_value=_make_positions([
        ("TAO/USDT:USDT", 50),
        ("XRP/USDT:USDT", 50),
    ]))
    broker.exchange.set_leverage = AsyncMock()
    broker.exchange.create_order = AsyncMock(return_value={
        "id": "order_123", "average": 2080.0, "price": 2080.0,
    })
    broker.exchange.market = MagicMock(return_value={
        "limits": {"amount": {"min": 0.001}},
        "precision": {"amount": 3},
    })

    result = await broker._free_margin_and_retry(
        "ETH-USDT", "sell", 0.787, "SHORT", 2083.5, 2061.9, 2070.4)

    assert result is not None
    assert result.success
    assert result.entry_price == 2080.0
    # 應該有調槓桿（2 個倉位 × 2 方向 = 4 次）
    assert broker.exchange.set_leverage.call_count >= 2
    # 全部直接拉到 150x
    for call in broker.exchange.set_leverage.call_args_list:
        assert call.args[0] == 150


async def test_retry_still_fails(broker):
    """調到 150x 後仍失敗 → 外層 except 捕獲回傳 None"""
    broker.exchange.fetch_positions = AsyncMock(return_value=_make_positions([
        ("TAO/USDT:USDT", 50),
    ]))
    broker.exchange.set_leverage = AsyncMock()
    broker.exchange.create_order = AsyncMock(
        side_effect=Exception('bingx {"code":101204,"msg":"Insufficient margin","data":{}}'))

    result = await broker._free_margin_and_retry(
        "ETH-USDT", "sell", 0.787, "SHORT", 2083.5, 2061.9, 2070.4)

    assert result is None
    # 只嘗試 1 次開倉
    assert broker.exchange.create_order.call_count == 1


async def test_retry_no_open_positions(broker):
    """無持倉可調槓桿 → 直接回傳 None"""
    broker.exchange.fetch_positions = AsyncMock(return_value=[])

    result = await broker._free_margin_and_retry(
        "ETH-USDT", "sell", 0.787, "SHORT", 2083.5, 2061.9, 2070.4)

    assert result is None
    broker.exchange.create_order.assert_not_called()


async def test_retry_non_margin_error_aborts(broker):
    """非保證金錯誤不會重試，直接拋出"""
    broker.exchange.fetch_positions = AsyncMock(return_value=_make_positions([
        ("TAO/USDT:USDT", 50),
    ]))
    broker.exchange.set_leverage = AsyncMock()
    broker.exchange.create_order = AsyncMock(
        side_effect=Exception("Network timeout"))

    result = await broker._free_margin_and_retry(
        "ETH-USDT", "sell", 0.787, "SHORT", 2083.5, 2061.9, 2070.4)

    # 非 margin 錯誤 → 外層 except 捕獲，回傳 None
    assert result is None
    assert broker.exchange.create_order.call_count == 1


async def test_retry_respects_max_leverage(broker):
    """已達 150x 上限的倉位不再調 → 直接回傳 None"""
    broker.exchange.fetch_positions = AsyncMock(return_value=_make_positions([
        ("TAO/USDT:USDT", 150),
    ]))
    broker.exchange.set_leverage = AsyncMock()

    result = await broker._free_margin_and_retry(
        "ETH-USDT", "sell", 0.787, "SHORT", 2083.5, 2061.9, 2070.4)

    assert result is None
    # 已達上限 → adjusted=False → 不嘗試開倉
    broker.exchange.set_leverage.assert_not_called()
    broker.exchange.create_order.assert_not_called()


async def test_retry_hangs_sl_tp_orders(broker):
    """重試成功後應掛上 SL/TP 保護單"""
    broker.exchange.fetch_positions = AsyncMock(side_effect=[
        # fetch_positions for leverage adjustment
        _make_positions([("TAO/USDT:USDT", 50)]),
        # fetch_positions for merged position check
        [{"symbol": "ETH/USDT:USDT", "contracts": 0.787, "side": "short",
          "leverage": 50, "entryPrice": 2080, "markPrice": 2079,
          "unrealizedPnl": 0.5, "id": "pos_eth"}],
    ])
    broker.exchange.set_leverage = AsyncMock()
    broker.exchange.create_order = AsyncMock(return_value={
        "id": "order_789", "average": 2080.0, "price": 2080.0,
    })
    broker.exchange.market = MagicMock(return_value={
        "limits": {"amount": {"min": 0.001}},
        "precision": {"amount": 3},
    })

    result = await broker._free_margin_and_retry(
        "ETH-USDT", "sell", 0.787, "SHORT", 2083.5, 2061.9, 2070.4)

    assert result is not None
    assert result.success

    # 開倉 + SL + TP3 + TP4 = 至少 3 次 create_order
    calls = broker.exchange.create_order.call_args_list
    assert len(calls) >= 3


# ── Executor retry queue（asyncio only，unittest.mock.AsyncMock 不支援 trio）──

from datetime import datetime
from src.trader.executor import TradeExecutor, ExecutorConfig, TradeState
from src.trader.base import OrderResult, AccountInfo
from src.models import ParsedSignal, SignalSide

_SIG_KEY = "ETHUSDTP1H032300"


def _make_signal(symbol="ETHUSDT.P", side=SignalSide.SHORT):
    return ParsedSignal(
        symbol=symbol, side=side, entry=2081.0, sl=2083.5,
        tp1=2078.9, tp2=2074.6, tp3=2070.4, tp4=2061.9,
        timeframe="1h", signal_time=datetime(2026, 3, 23),
        signal_type="entry",
        related_signal_key=_SIG_KEY,
    )


@pytest.fixture
def executor(tmp_path):
    """建立 mock TradeExecutor（狀態存到臨時目錄，不污染 db/）"""
    broker = AsyncMock()
    broker.get_account = AsyncMock(return_value=AccountInfo(
        balance=1000.0, equity=1000.0, margin_used=500.0, margin_available=500.0,
    ))
    broker.get_market = MagicMock(return_value=None)
    cfg = ExecutorConfig(risk_per_trade=0.01)
    ex = TradeExecutor(broker=broker, config=cfg, label="test")
    # 把狀態目錄指向臨時目錄
    ex.STATE_DIR = tmp_path
    return ex


@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_margin_fail_queues_retry(executor, anyio_backend):
    """保證金不足時信號被加入重試佇列"""
    executor.broker.market_order = AsyncMock(return_value=OrderResult(
        success=False, symbol="ETHUSDT.P",
        error='bingx {"code":101204,"msg":"Insufficient margin","data":{}}',
    ))

    sig = _make_signal()
    result = await executor.execute_signal(sig)

    assert not result.success
    assert len(executor._retry_queue) == 1
    assert executor._retry_queue[0][0] is sig
    assert executor._retry_queue[0][1] == 1


@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_non_margin_fail_not_queued(executor, anyio_backend):
    """非保證金錯誤不加入重試佇列"""
    executor.broker.market_order = AsyncMock(return_value=OrderResult(
        success=False, symbol="ETHUSDT.P", error="Network timeout",
    ))

    result = await executor.execute_signal(_make_signal())

    assert not result.success
    assert len(executor._retry_queue) == 0


@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_process_retry_queue_success(executor, anyio_backend):
    """重試佇列成功開倉"""
    sig = _make_signal()
    executor._retry_queue.append((sig, 1))

    executor.broker.market_order = AsyncMock(return_value=OrderResult(
        success=True, order_id="123", trade_id="123",
        symbol="ETHUSDT.P", side="short", units=0.787,
        entry_price=2080.0, sl_price=2083.5, tp_price=2061.9,
    ))

    await executor._process_retry_queue()

    assert len(executor._retry_queue) == 0
    assert _SIG_KEY in executor.active_trades


@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_process_retry_queue_exceeds_max(executor, anyio_backend):
    """重試超過 RETRY_MAX 次後放棄"""
    sig = _make_signal()
    executor._retry_queue.append((sig, executor.RETRY_MAX))

    executor.broker.market_order = AsyncMock(return_value=OrderResult(
        success=False, symbol="ETHUSDT.P",
        error='bingx {"code":101204,"msg":"Insufficient margin","data":{}}',
    ))

    await executor._process_retry_queue()

    # 超過上限 → 佇列清空，不再重試
    assert len(executor._retry_queue) == 0


@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_process_retry_queue_skip_if_already_opened(executor, anyio_backend):
    """已經有持倉的信號不再重試"""
    sig = _make_signal()
    executor._retry_queue.append((sig, 1))

    # 模擬已經有這筆持倉
    executor.active_trades[_SIG_KEY] = TradeState(
        signal_key=_SIG_KEY, trade_id="existing",
        symbol="ETHUSDT.P", side=SignalSide.SHORT,
        entry_price=2081.0, total_units=0.787, remaining_units=0.787,
        sl_original=2083.5, sl_current=2083.5, tp1=2078.9,
    )

    await executor._process_retry_queue()

    assert len(executor._retry_queue) == 0
    executor.broker.market_order.assert_not_called()

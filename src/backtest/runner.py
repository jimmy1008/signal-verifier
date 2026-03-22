"""
回測執行管理器

職責：
- 從 DB 讀取所有信號
- 為每筆信號載入對應 K 線
- 呼叫 engine 模擬
- 將結果寫入 DB
- 支援不同設定的批次回測

輸入：BacktestConfig
輸出：BacktestRunORM + TradeResultORM 寫入 DB
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from src.models import (
    SignalORM,
    SignalStatus,
    BacktestConfig,
    BacktestRunORM,
    TradeResultORM,
    TradeResult,
)
from src.backtest.engine import simulate_trade
from src.market_data.provider import load_candles, MarketDataProvider

logger = logging.getLogger(__name__)


def run_backtest(
    session: Session,
    config: BacktestConfig,
    provider: Optional[MarketDataProvider] = None,
    symbol_map: Optional[dict] = None,
    source: Optional[str] = None,
    lookforward_hours: int = 168,  # 進場後往前看多久的 K 線（預設 7 天）
) -> list[TradeResult]:
    """
    執行一次完整回測。

    Args:
        session: DB session
        config: 回測配置
        provider: 市場資料提供者
        symbol_map: symbol 映射
        source: 篩選特定來源
        lookforward_hours: 每筆信號抓多長的 K 線

    Returns:
        所有交易結果
    """
    # 建立回測紀錄
    run = BacktestRunORM(
        config_name=config.name,
        config_json=config.model_dump(),
    )
    session.add(run)
    session.flush()

    # 讀取信號
    query = session.query(SignalORM)
    if source:
        query = query.filter_by(source=source)
    signals = query.order_by(SignalORM.signal_time.asc()).all()

    results = []
    for i, signal in enumerate(signals):
        logger.info(f"[{i+1}/{len(signals)}] {signal.symbol} {signal.side.value} @ {signal.entry}")

        # 載入 K 線
        since = signal.signal_time
        until = since + timedelta(hours=lookforward_hours)
        timeframe = signal.timeframe or "15m"

        try:
            candles = load_candles(
                session, signal.symbol, timeframe, since, until,
                provider=provider, symbol_map=symbol_map,
            )
        except Exception as e:
            logger.error(f"K 線載入失敗 {signal.symbol}: {e}")
            result = TradeResult(signal_id=signal.id, triggered=False, notes=f"candle fetch error: {e}")
            results.append(result)
            continue

        # 模擬交易
        result = simulate_trade(signal, candles, config)
        results.append(result)

        # 更新信號狀態
        if result.triggered:
            signal.status = SignalStatus.TRIGGERED
        else:
            signal.status = SignalStatus.NOT_TRIGGERED

        # 儲存結果
        trade = TradeResultORM(
            run_id=run.id,
            signal_id=signal.id,
            triggered=result.triggered,
            entry_time=result.entry_time,
            exit_time=result.exit_time,
            exit_reason=result.exit_reason,
            exit_price=result.exit_price,
            max_tp_hit=result.max_tp_hit,
            pnl_r=result.pnl_r,
            pnl_pct=result.pnl_pct,
            drawdown_r=result.drawdown_r,
            notes=result.notes,
        )
        session.add(trade)

    session.commit()
    logger.info(f"回測完成: {len(results)} 筆信號, run_id={run.id}")
    return results

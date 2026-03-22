"""
交易執行引擎

策略：
- 進場後下 4 張 TP 單（各 25% 倉位）+ 1 張 SL 單
- 碰 TP1 → 出 25% + SL 移到 Entry（保本）
- 碰 TP2 → 出 25%
- 碰 TP3 → 出 25%
- 碰 TP4 → 出最後 25%
- 碰 SL → 剩餘全出

雙層保護：
- Layer 1: Broker 端 TP/SL 掛單（自動觸發）
- Layer 2: 監聽頻道 TP/SL 回報（防重複，以 Broker 為主）
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from src.models import ParsedSignal, SignalSide, UpdateType
from src.trader.base import BaseBroker, OrderResult, Position, PositionTracker

logger = logging.getLogger(__name__)


class TradeState(BaseModel):
    """單筆交易的追蹤狀態"""
    signal_key: str              # 唯一識別：如 #BTC1H031917
    trade_id: str
    symbol: str
    side: SignalSide
    timeframe: Optional[str] = None  # 1h / 4h
    entry_price: float
    total_units: float
    remaining_units: float
    sl_original: float
    sl_current: float
    tp1: float
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    tp4: Optional[float] = None
    # 各 TP 層是否已出場（防重複）
    tp1_closed: bool = False
    tp2_closed: bool = False
    tp3_closed: bool = False
    tp4_closed: bool = False
    sl_moved_to_be: bool = False
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    closed: bool = False
    close_reason: str = ""


class ExecutorConfig(BaseModel):
    """執行器設定"""
    risk_per_trade: float = 0.01
    max_positions: int = 0            # 0 = 無上限
    monitor_interval: int = 10
    # 分批止盈：TP1/TP2 不出場，TP3=50% TP4=50%
    # TP2 觸發後 SL 移到 Entry（保本）
    tp1_pct: float = 0.0
    tp2_pct: float = 0.0
    tp3_pct: float = 0.50
    tp4_pct: float = 0.50


class TradeExecutor:
    # 狀態存檔路徑（每個 executor 實例用 label 區分）
    STATE_DIR = Path(__file__).resolve().parent.parent.parent / "db"

    def __init__(self, broker: BaseBroker, config: ExecutorConfig, label: str = "default"):
        self.broker = broker
        self.config = config
        self.label = label
        self.active_trades: dict[str, TradeState] = {}
        self.positions: dict[str, PositionTracker] = {}  # symbol → tracker
        self._monitor_task: Optional[asyncio.Task] = None

        # 啟動時恢復狀態
        self._restore_state()

    def _get_tracker(self, symbol: str) -> PositionTracker:
        """取得或建立 symbol 的持倉追蹤器"""
        if symbol not in self.positions:
            self.positions[symbol] = PositionTracker(symbol)
        return self.positions[symbol]

    # ─── 狀態持久化（參考 bbgo persistence）─────────

    # BE 標記檔（跨帳戶共用）
    _BE_MARKS_PATH = Path(__file__).resolve().parent.parent.parent / "db" / "be_marks.json"

    @property
    def _state_path(self) -> Path:
        return self.STATE_DIR / f"executor_state_{self.label}.json"

    def _mark_be(self, symbol: str, side: str, signal_key: str = "") -> None:
        """標記某倉位已移到保本，供 dashboard 判斷 BE"""
        try:
            marks = {}
            if self._BE_MARKS_PATH.exists():
                marks = json.loads(self._BE_MARKS_PATH.read_text(encoding="utf-8"))
            key = f"{symbol}|{side}"
            marks[key] = signal_key or True
            self._BE_MARKS_PATH.write_text(
                json.dumps(marks, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _unmark_be(self, symbol: str, side: str) -> None:
        """平倉後清除 BE 標記"""
        try:
            if not self._BE_MARKS_PATH.exists():
                return
            marks = json.loads(self._BE_MARKS_PATH.read_text(encoding="utf-8"))
            key = f"{symbol}|{side}"
            if key in marks:
                del marks[key]
                self._BE_MARKS_PATH.write_text(
                    json.dumps(marks, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _save_state(self) -> None:
        """將 active_trades 序列化存檔，並清理已平倉的 BE 標記"""
        try:
            data = {}
            for key, state in self.active_trades.items():
                data[key] = state.model_dump(mode="json")
                # 已平倉 → 清 BE 標記
                if state.closed and state.sl_moved_to_be:
                    self._unmark_be(state.symbol, state.side.value)
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(data, default=str, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug(f"狀態存檔失敗: {e}")

    def _restore_state(self) -> None:
        """啟動時從 JSON 恢復 active_trades"""
        if not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            restored = 0
            for key, val in raw.items():
                try:
                    state = TradeState(**val)
                    if not state.closed:
                        self.active_trades[key] = state
                        restored += 1
                except Exception:
                    continue
            if restored:
                logger.info(f"[{self.label}] 從存檔恢復 {restored} 筆活躍持倉")
        except Exception as e:
            logger.warning(f"狀態恢復失敗: {e}")

    # ─── 信號進場 ─────────────────────────────────

    async def execute_signal(self, signal: ParsedSignal) -> Optional[OrderResult]:
        """收到進場信號 → 下單 + 掛 4 張 TP 分批單"""
        if signal.signal_type != "entry":
            return None

        # TP4 必須存在
        if signal.tp4 is None:
            logger.info(f"跳過：{signal.symbol} 沒有 TP4")
            return None

        # 同 signal_key 不重複（同幣種可開多空/1H4H 共 4 倉）
        key = signal.related_signal_key or f"{signal.symbol}_{signal.side.value}_{signal.timeframe}_{signal.entry}"
        if key in self.active_trades and not self.active_trades[key].closed:
            logger.info(f"跳過：{key} 已有持倉")
            return None

        # 最大持倉（0 = 無上限）
        if self.config.max_positions > 0:
            active = sum(1 for s in self.active_trades.values() if not s.closed)
            if active >= self.config.max_positions:
                logger.info(f"跳過：已達最大持倉數")
                return None

        # 計算倉位
        account = await self.broker.get_account()
        units = self._calc_units(account.balance, signal.entry, signal.sl, signal.symbol)
        if units <= 0:
            logger.warning(f"倉位為 0: {signal.symbol}")
            return None

        # 下主單（全倉市價單 + SL）
        logger.info(
            f"下單: {signal.symbol} {signal.side.value} "
            f"units={units} sl={signal.sl} tp1~tp4={signal.tp1}/{signal.tp2}/{signal.tp3}/{signal.tp4}"
        )

        result = await self.broker.market_order(
            symbol=signal.symbol,
            side=signal.side,
            units=units,
            sl=signal.sl,
            tp=signal.tp4,
            tp3=signal.tp3,
        )

        if not result.success:
            logger.error(f"下單失敗: {result.error}")
            return result

        # 追蹤（key 已在上面算過）
        state = TradeState(
            signal_key=key,
            trade_id=result.trade_id,
            symbol=signal.symbol,
            side=signal.side,
            timeframe=signal.timeframe,
            entry_price=result.entry_price or signal.entry,
            total_units=units,
            remaining_units=units,
            sl_original=signal.sl,
            sl_current=signal.sl,
            tp1=signal.tp1 or 0,
            tp2=signal.tp2,
            tp3=signal.tp3,
            tp4=signal.tp4,
        )
        self.active_trades[key] = state

        # 記錄到 PositionTracker
        tracker = self._get_tracker(signal.symbol)
        buy_side = "buy" if signal.side == SignalSide.LONG else "sell"
        tracker.add_trade(buy_side, units, result.entry_price or signal.entry)

        logger.info(f"已開倉: {signal.symbol} {signal.side.value} {signal.timeframe or ''} key={key} units={units}")
        self._save_state()
        return result

    # ─── 頻道 TP/SL 回報處理（Layer 2）─────────────

    async def handle_update(self, signal: ParsedSignal) -> None:
        """
        收到頻道的 TP hit / SL hit 回報。
        作為第二層保護，確保分批止盈執行。
        如果 Broker 端已經觸發了，這裡就跳過（防重複）。
        """
        if not signal.related_signal_key:
            logger.debug(f"更新訊息無 signal_key，跳過")
            return

        state = self.active_trades.get(signal.related_signal_key)
        if not state or state.closed:
            # 無對應持倉（中途跟單，這筆沒進場）→ 跳過
            logger.debug(f"無對應持倉: {signal.related_signal_key}，跳過")
            return

        if signal.update_type == UpdateType.TP_HIT:
            tp_level = signal.update_value  # "tp1", "tp2", etc.
            await self._partial_close_by_level(state, tp_level, source="頻道回報")

        elif signal.update_type == UpdateType.CLOSE_NOW:
            # SL hit 回報
            logger.info(f"[{state.symbol}] 頻道回報 SL hit，檢查 Broker 狀態")
            state.closed = True
            state.close_reason = "sl_hit (頻道確認)"
            await self._cancel_remaining_orders(state)
            logger.info(f"[{state.symbol}] 已確認平倉，殘留掛單已清除")
            self._save_state()

    async def _partial_close_by_level(self, state: TradeState, tp_level: str, source: str = "監控") -> None:
        """分批平倉指定 TP 層，防重複。TP1=0% TP2=0%(保本) TP3=50% TP4=50%"""
        pct_map = {"tp1": self.config.tp1_pct, "tp2": self.config.tp2_pct,
                   "tp3": self.config.tp3_pct, "tp4": self.config.tp4_pct}
        pct = pct_map.get(tp_level, 0)
        if pct <= 0:
            # 這層不出場（如 TP1/TP2 = 0%），只標記觸及
            logger.info(f"[{state.symbol}] {tp_level} 觸及，不出場（{source}）")
            if tp_level == "tp1":
                state.tp1_closed = True
                logger.info(f"[{state.symbol}] TP1 觸及，繼續持倉")
            elif tp_level == "tp2":
                state.tp2_closed = True
                if not state.sl_moved_to_be:
                    try:
                        # 保本價 = Entry ± 手續費回本（開倉 0.05% + 平倉 0.05% = 0.1%）
                        fee_pct = 0.001  # 0.1% 雙向手續費
                        if state.side == SignalSide.LONG:
                            be_price = round(state.entry_price * (1 + fee_pct), 6)
                        else:
                            be_price = round(state.entry_price * (1 - fee_pct), 6)

                        ok = await self.broker.modify_trade(state.trade_id, sl=be_price, symbol=state.symbol, side=state.side.value)
                        if ok:
                            state.sl_current = be_price
                            state.sl_moved_to_be = True
                            self._mark_be(state.symbol, state.side.value, state.signal_key)
                            logger.info(f"[{state.symbol}] TP2 觸及，SL 移到保本 {be_price}（含手續費，Entry={state.entry_price}）")
                        else:
                            # 失敗了，下次重試
                            state.tp2_closed = False
                            logger.warning(f"[{state.symbol}] SL 移動失敗，下次重試")
                    except Exception as e:
                        state.tp2_closed = False
                        logger.error(f"[{state.symbol}] SL 移動異常: {e}，下次重試")
            self._save_state()
            return

        portion = round(state.total_units * pct, 8)

        # 防重複：檢查該層是否已平（TP1/TP2 pct=0 時已在上面 return，這裡只處理 TP3/TP4）
        if tp_level == "tp3" and state.tp3_closed:
            logger.debug(f"[{state.symbol}] TP3 已平過，跳過 ({source})")
            return
        if tp_level == "tp4" and state.tp4_closed:
            logger.debug(f"[{state.symbol}] TP4 已平過，跳過 ({source})")
            return

        close_units = min(portion, state.remaining_units)
        if close_units <= 0:
            return

        logger.info(f"[{state.symbol}] {tp_level} 觸發 ({source}) → 平 {close_units} units (剩 {state.remaining_units - close_units})")

        # 平倉部分倉位
        success = await self._close_partial(state, close_units)

        if success:
            state.remaining_units -= close_units

            if tp_level == "tp3":
                state.tp3_closed = True
            elif tp_level == "tp4":
                state.tp4_closed = True
                state.closed = True
                state.close_reason = "tp4_all_closed"
                await self._cancel_remaining_orders(state)

            if state.remaining_units <= 0:
                state.closed = True
                state.close_reason = f"all_tp_closed"
                await self._cancel_remaining_orders(state)

            self._save_state()

    async def _close_partial(self, state: TradeState, units: float) -> bool:
        """平倉部分倉位（Hedge Mode: positionSide 代替 reduceOnly）"""
        try:
            close_side = "sell" if state.side == SignalSide.LONG else "buy"
            pos_side = "LONG" if state.side == SignalSide.LONG else "SHORT"

            from src.trader.bingx import to_bingx_symbol
            bingx_sym = to_bingx_symbol(state.symbol)

            # 取得 exchange 物件
            if hasattr(self.broker, 'exchange'):
                ex = self.broker.exchange
            elif hasattr(self.broker, 'crypto') and self.broker.crypto and hasattr(self.broker.crypto, 'exchange'):
                ex = self.broker.crypto.exchange
            else:
                logger.error(f"找不到 exchange 物件")
                return False

            order = await ex.create_order(
                symbol=bingx_sym,
                type="market",
                side=close_side,
                amount=units,
                params={"positionSide": pos_side},
            )
            # 記錄到 PositionTracker
            exit_price = float(order.get("average", 0) or order.get("price", 0) or 0)
            tracker = self._get_tracker(state.symbol)
            rpnl = tracker.add_trade(close_side, units, exit_price)
            logger.info(f"[{state.symbol}] 部分平倉 {units} units 成功 (realized: ${rpnl:+.4f})")
            return True
        except Exception as e:
            err = str(e)
            if "No position" in err or "101205" in err:
                # 持倉已被 Broker 端 TP/SL 平掉
                logger.info(f"[{state.symbol}] 持倉已不存在（Broker 端已平倉）")
                state.closed = True
                state.close_reason = "broker_closed"
                self._save_state()
                return True
            logger.error(f"部分平倉失敗 {state.symbol}: {e}")
            return False

    # ─── 掛單清理 ─────────────────────────────────

    async def _cancel_remaining_orders(self, state: TradeState) -> None:
        """平倉後取消該持倉殘留的 TP/SL 掛單"""
        try:
            from src.trader.bingx import to_bingx_symbol
            bingx_sym = to_bingx_symbol(state.symbol)

            if hasattr(self.broker, 'exchange'):
                ex = self.broker.exchange
            elif hasattr(self.broker, 'crypto') and self.broker.crypto and hasattr(self.broker.crypto, 'exchange'):
                ex = self.broker.crypto.exchange
            else:
                return

            orders = await ex.fetch_open_orders(bingx_sym)
            pos_side = "LONG" if state.side == SignalSide.LONG else "SHORT"
            close_side = "sell" if state.side == SignalSide.LONG else "buy"
            cancelled = 0

            for order in orders:
                otype = str(order.get("type", "")).lower()
                oside = str(order.get("side", "")).lower()
                order_pos_side = order.get("info", {}).get("positionSide", "")

                # 只取消屬於這個方向的 TP/SL 單
                if ("stop" in otype or "take_profit" in otype) and oside == close_side:
                    if order_pos_side and order_pos_side != pos_side:
                        continue  # 不是這個方向的掛單
                    await ex.cancel_order(order["id"], bingx_sym)
                    cancelled += 1

            if cancelled:
                logger.info(f"[{state.symbol}] 已取消 {cancelled} 筆殘留掛單 ({pos_side})")
        except Exception as e:
            logger.warning(f"[{state.symbol}] 清理掛單失敗: {e}")

    async def _check_position_exists(self, state: TradeState) -> bool:
        """檢查指定方向的持倉是否還存在（雙向持倉感知）"""
        try:
            from src.trader.bingx import to_bingx_symbol
            bingx_sym = to_bingx_symbol(state.symbol)
            target_side = "long" if state.side == SignalSide.LONG else "short"

            if hasattr(self.broker, 'exchange'):
                ex = self.broker.exchange
            elif hasattr(self.broker, 'crypto') and self.broker.crypto and hasattr(self.broker.crypto, 'exchange'):
                ex = self.broker.crypto.exchange
            else:
                return True  # 無法確認，假設存在

            positions = await ex.fetch_positions([bingx_sym])
            for pos in positions:
                contracts = abs(float(pos.get("contracts", 0)))
                if contracts > 0 and pos.get("side") == target_side:
                    return True
            return False
        except Exception:
            return True  # 查詢失敗，保守假設存在

    # ─── 持倉監控（Layer 1）─────────────────────────

    async def start_monitor(self) -> None:
        logger.info(f"開始持倉監控（每 {self.config.monitor_interval} 秒）")
        # 啟動時檢查已有持倉，防止重啟後失去管理
        try:
            existing = await self.broker.get_open_positions()
            if existing:
                logger.warning(f"偵測到 {len(existing)} 筆已有持倉（bot 重啟），將由 Broker 端 SL/TP 管理")
        except Exception:
            pass
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop_monitor(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
            logger.info("持倉監控已停止")

    async def _monitor_loop(self) -> None:
        _margin_check_counter = 0
        while True:
            try:
                await self._check_positions()
                # 每 6 輪（約 60 秒）檢查保證金 + 掛單完整性
                _margin_check_counter += 1
                if _margin_check_counter >= 6:
                    _margin_check_counter = 0
                    if hasattr(self.broker, "check_margin_and_adjust"):
                        await self.broker.check_margin_and_adjust(threshold=0.4)
                    await self._check_protection_orders()
            except Exception as e:
                logger.error(f"監控異常: {e}")
            await asyncio.sleep(self.config.monitor_interval)

    async def _check_positions(self) -> None:
        """逐筆檢查持倉，觸發分批止盈"""
        for key, state in list(self.active_trades.items()):
            if state.closed:
                continue

            try:
                # 定期檢查 Broker 端持倉是否還在（每 60 秒一次）
                import time as _time
                age = _time.time() - state.opened_at.timestamp()
                if int(age) % 60 < self.config.monitor_interval:
                    has_position = await self._check_position_exists(state)
                    if not has_position and state.tp1_closed:
                        # 該方向持倉消失（被 SL/TP 或手動平掉）
                        state.closed = True
                        state.close_reason = "broker_closed"
                        await self._cancel_remaining_orders(state)
                        logger.info(f"[{state.symbol}] Broker 端已無持倉，殘留掛單已清除")
                        self._save_state()
                        continue

                bid, ask = await self.broker.get_price(state.symbol)
                current = bid if state.side == SignalSide.LONG else ask
                if current <= 0:
                    continue

                is_long = state.side == SignalSide.LONG

                # 逐層檢查 TP（由低到高）
                if state.tp1 and not state.tp1_closed:
                    hit = (is_long and current >= state.tp1) or (not is_long and current <= state.tp1)
                    if hit:
                        await self._partial_close_by_level(state, "tp1", source="價格監控")

                if state.tp2 and not state.tp2_closed:
                    hit = (is_long and current >= state.tp2) or (not is_long and current <= state.tp2)
                    if hit:
                        await self._partial_close_by_level(state, "tp2", source="價格監控")

                if state.tp3 and not state.tp3_closed:
                    hit = (is_long and current >= state.tp3) or (not is_long and current <= state.tp3)
                    if hit:
                        await self._partial_close_by_level(state, "tp3", source="價格監控")

                if state.tp4 and not state.tp4_closed:
                    hit = (is_long and current >= state.tp4) or (not is_long and current <= state.tp4)
                    if hit:
                        await self._partial_close_by_level(state, "tp4", source="價格監控")

            except Exception as e:
                logger.error(f"監控 {state.symbol} 異常: {e}")

    # ─── 倉位計算 ─────────────────────────────────

    def _calc_units(self, balance: float, entry: float, sl: float, symbol: str) -> float:
        risk_amount = balance * self.config.risk_per_trade
        risk_per_unit = abs(entry - sl)
        if risk_per_unit == 0:
            return 0

        units = risk_amount / risk_per_unit

        # 用 MarketInfo 精度（如果 broker 有提供）
        if hasattr(self.broker, "get_market"):
            mkt = self.broker.get_market(symbol)
            if mkt:
                return mkt.round_quantity(units)

        # fallback: 硬編碼精度
        sym = symbol.upper().replace(".P", "")
        if sym.endswith("USDT"):
            if entry > 1000:
                units = round(units, 3)
            elif entry > 10:
                units = round(units, 2)
            else:
                units = round(units, 1)
            return max(units, 0.001)
        else:
            return max(int(units), 1)

    async def _check_protection_orders(self) -> None:
        """定期檢查每個持倉是否都有 SL/TP 保護單"""
        if not hasattr(self.broker, 'exchange'):
            return
        try:
            from src.trader.bingx import to_bingx_symbol
            positions = await self.broker.exchange.fetch_positions()

            for pos in positions:
                contracts = abs(float(pos.get("contracts", 0)))
                if contracts <= 0:
                    continue

                sym = pos.get("symbol", "")
                side = pos.get("side", "")
                pos_side = "LONG" if side == "long" else "SHORT"
                close_side = "sell" if side == "long" else "buy"

                try:
                    orders = await self.broker.exchange.fetch_open_orders(sym)
                except Exception:
                    continue

                relevant = [o for o in orders
                            if o.get("info", {}).get("positionSide") == pos_side
                            and str(o.get("side", "")).lower() == close_side]

                has_sl = any("stop" in str(o.get("info", {}).get("type", "")).lower()
                            and "profit" not in str(o.get("info", {}).get("type", "")).lower()
                            for o in relevant)
                has_tp = any("profit" in str(o.get("info", {}).get("type", "")).lower()
                            for o in relevant)

                if not has_sl or not has_tp:
                    sym_short = sym.replace("/USDT:USDT", "")
                    missing = []
                    if not has_sl:
                        missing.append("SL")
                    if not has_tp:
                        missing.append("TP")
                    logger.warning(f"[{sym_short} {pos_side}] 缺少保護單: {', '.join(missing)}")
        except Exception as e:
            logger.debug(f"掛單檢查異常: {e}")

    def get_active_trades(self) -> list[TradeState]:
        return [s for s in self.active_trades.values() if not s.closed]

    def get_trade_count(self) -> int:
        return sum(1 for s in self.active_trades.values() if not s.closed)

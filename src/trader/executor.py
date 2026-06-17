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
    # 分批進場
    staged_entry: bool = False
    staged_remaining_units: float = 0.0
    staged_added: bool = False


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
    # 分批進場：50% 進場，TP1 後加倉 50%
    staged_entry: bool = False
    staged_entry_pct: float = 0.50
    # 幣種過濾（黑名單）
    blocked_symbols: list[str] = []


class TradeExecutor:
    # 狀態存檔路徑（每個 executor 實例用 label 區分）
    STATE_DIR = Path(__file__).resolve().parent.parent.parent / "db"

    RETRY_MAX = 3          # 每筆信號最多重試次數
    RETRY_INTERVAL = 30    # 重試間隔（秒）

    def __init__(self, broker: BaseBroker, config: ExecutorConfig, label: str = "default"):
        self.broker = broker
        self.config = config
        self.label = label
        self.active_trades: dict[str, TradeState] = {}
        self.positions: dict[str, PositionTracker] = {}  # symbol → tracker
        self._monitor_task: Optional[asyncio.Task] = None
        self._retry_queue: list[tuple[ParsedSignal, int]] = []  # (signal, attempt)

        # 啟動時恢復狀態
        self._restore_state()

    def _get_paper_broker(self):
        """如果 broker 是 PaperBroker，回傳它"""
        from src.trader.paper import PaperBroker
        if isinstance(self.broker, PaperBroker):
            return self.broker
        if hasattr(self.broker, 'crypto') and isinstance(self.broker.crypto, PaperBroker):
            return self.broker.crypto
        return None

    def _get_tracker(self, symbol: str) -> PositionTracker:
        """取得或建立 symbol 的持倉追蹤器"""
        if symbol not in self.positions:
            self.positions[symbol] = PositionTracker(symbol)
        return self.positions[symbol]

    def _daily_loss_limit(self) -> float:
        """每日虧損上限（帳戶餘額的 5%，最低 $5）"""
        return max(5.0, 150 * 0.05)  # $7.5

    def _get_today_closed(self, today_str: str) -> list:
        """從 trade_history 讀今日已平倉"""
        history_path = self._state_path.parent / f"trade_history_{self.label}.json"
        if not history_path.exists():
            return []
        try:
            with open(history_path, "r", encoding="utf-8") as f:
                history = json.load(f)
            return [h for h in history if today_str in str(h.get("closed_at", "") or h.get("opened_at", ""))]
        except Exception:
            return []

    async def _check_crt_sweep(self, signal) -> bool:
        """檢查信號是否符合 CRT sweep 邏輯：
        LONG = 信號K線 sweep 了前幾根的低點
        SHORT = 信號K線 sweep 了前幾根的高點
        """
        try:
            # 取前 3 根 K 線的報價（用 BingX ticker 無法取歷史，改用信號資訊推斷）
            # 簡化方法：用 entry/SL 推算
            # LONG: SL 在下方 → 如果 SL 距離 entry 很遠（> 0.3%），代表有 sweep
            # SHORT: SL 在上方 → 同理
            #
            # 更精確：從 DB 的 K 線快取取
            from src.trader.bingx import to_bingx_symbol

            # 用 BingX API 即時抓 K 線
            if hasattr(self.broker, 'exchange'):
                ex = self.broker.exchange
            elif hasattr(self.broker, 'crypto') and self.broker.crypto and hasattr(self.broker.crypto, 'exchange'):
                ex = self.broker.crypto.exchange
            else:
                logger.warning(f"[CRT] 無 exchange，放行")
                return True

            bingx_sym = to_bingx_symbol(signal.symbol)
            tf = signal.timeframe or "1h"

            ohlcv = await ex.fetch_ohlcv(bingx_sym, tf, limit=5)
            if not ohlcv or len(ohlcv) < 3:
                logger.warning(f"[CRT] {signal.symbol} K 線不足，放行")
                return True

            # 倒數第二根 = 信號 K 線（剛收盤），前面的是歷史
            sig = ohlcv[-2]
            p1 = ohlcv[-3]
            p2 = ohlcv[-4] if len(ohlcv) >= 4 else p1

            sig_high, sig_low, sig_close = sig[2], sig[3], sig[4]
            p1_high, p1_low = p1[2], p1[3]
            p2_high, p2_low = p2[2], p2[3]

            # CRT candle = 前 2 根中 range 最大的
            p1_range = p1_high - p1_low
            p2_range = p2_high - p2_low
            crt_high = p1_high if p1_range >= p2_range else p2_high
            crt_low = p1_low if p1_range >= p2_range else p2_low

            if signal.side.value == "long":
                is_sweep = sig_low < crt_low
                logger.info(f"[CRT] {signal.symbol} LONG sweep={is_sweep} (sig_low={sig_low} vs crt_low={crt_low})")
                return is_sweep
            else:
                is_sweep = sig_high > crt_high
                logger.info(f"[CRT] {signal.symbol} SHORT sweep={is_sweep} (sig_high={sig_high} vs crt_high={crt_high})")
                return is_sweep

        except Exception as e:
            logger.warning(f"[CRT] sweep 檢查失敗: {e}，放行")
            return True

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
        """將 active_trades 序列化存檔，已平倉移入歷史"""
        try:
            data = {}
            newly_closed = []
            for key, state in self.active_trades.items():
                data[key] = state.model_dump(mode="json")
                if state.closed and state.sl_moved_to_be:
                    self._unmark_be(state.symbol, state.side.value)
                if state.closed:
                    newly_closed.append(key)

            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(data, default=str, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._state_path)  # 原子替換

            # 已平倉追加到歷史檔案
            if newly_closed:
                history_path = self._state_path.parent / f"trade_history_{self.label}.json"
                history = []
                if history_path.exists():
                    try:
                        history = json.loads(history_path.read_text(encoding="utf-8"))
                    except Exception:
                        history = []

                existing_keys = {h.get("signal_key") for h in history}
                for key in newly_closed:
                    state = self.active_trades[key]
                    if key not in existing_keys:
                        record = state.model_dump(mode="json")
                        record["signal_key"] = key
                        record["closed_at"] = str(datetime.now(timezone.utc))

                        # 計算 PnL
                        entry = state.entry_price
                        sl_orig = state.sl_original
                        risk = abs(entry - sl_orig) if sl_orig else 0
                        units = state.total_units
                        reason = state.close_reason
                        max_tp = 0
                        if state.tp4_closed: max_tp = 4
                        elif state.tp3_closed: max_tp = 3
                        elif state.tp2_closed: max_tp = 2
                        elif state.tp1_closed: max_tp = 1

                        if "sl_hit" in reason and max_tp == 0:
                            pnl = -risk * units
                        elif "all_tp" in reason or max_tp >= 4:
                            tp3 = state.tp3 or entry
                            tp4 = state.tp4 or entry
                            pnl = abs(tp3 - entry) * units * 0.5 + abs(tp4 - entry) * units * 0.5
                        elif max_tp >= 3:
                            pnl = abs(state.tp3 - entry) * units * 0.5 - risk * units * 0.5
                        elif "sl_hit" in reason or "broker_closed" in reason:
                            pnl = -risk * units
                        else:
                            pnl = 0

                        fee = entry * units * 0.001  # 0.1% round trip
                        record["realized_pnl"] = round(pnl - fee, 4)
                        record["fee"] = round(fee, 4)
                        record["max_tp_hit"] = max_tp
                        record["close_reason"] = reason

                        # 分類 exit type
                        if max_tp >= 3 and ("all_tp" in reason or max_tp >= 4):
                            record["exit_type"] = "TP"
                        elif max_tp >= 1 and "sl" in reason:
                            record["exit_type"] = f"TP{max_tp}+SL"
                        elif "sl" in reason:
                            record["exit_type"] = "SL"
                        else:
                            record["exit_type"] = reason

                        history.append(record)

                history_path.write_text(
                    json.dumps(history, default=str, ensure_ascii=False, indent=2),
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
                except Exception as e:
                    logger.warning(f"[{self.label}] 恢復倉位 {key} 失敗: {e}")
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

        # 信號過期檢查（超過 120 秒跳過）
        if signal.signal_time:
            from datetime import timezone
            age = (datetime.now(timezone.utc) - signal.signal_time.replace(tzinfo=timezone.utc)).total_seconds()
            if age > 120:
                logger.info(f"跳過：{signal.symbol} 信號已過期 ({int(age)}秒)")
                return None

        # 每日虧損上限檢查（超過 5% 停止接單）
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_loss = sum(
            s.get("realized_pnl", 0) for s in self._get_today_closed(today_str)
        )
        if today_loss < -self._daily_loss_limit():
            logger.warning(f"[{self.label}] 今日已虧 ${today_loss:.2f}，超過上限，停止接單")
            return None

        # TP4 必須存在
        if signal.tp4 is None:
            logger.info(f"跳過：{signal.symbol} 沒有 TP4")
            return None

        # CRT Sweep 過濾：只跟符合 sweep 邏輯的信號
        sweep_ok = await self._check_crt_sweep(signal)
        if not sweep_ok:
            logger.info(f"[{self.label}] 跳過：{signal.symbol} 不符合 CRT sweep")
            return None

        # 幣種黑名單過濾
        if self.config.blocked_symbols:
            base = signal.symbol.upper().replace("USDT", "").replace(".P", "")
            if base in [s.upper() for s in self.config.blocked_symbols]:
                logger.info(f"[{self.label}] 跳過：{signal.symbol} 在排除清單中")
                return None

        # SL 距離太近（< 0.1%）不下單
        if signal.entry > 0 and abs(signal.entry - signal.sl) / signal.entry < 0.001:
            logger.info(f"跳過：{signal.symbol} SL 距離 {abs(signal.entry - signal.sl) / signal.entry:.4%} < 0.1%")
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
        full_units = self._calc_units(account.balance, signal.entry, signal.sl, signal.symbol)
        if full_units <= 0:
            logger.warning(f"倉位為 0: {signal.symbol}")
            return None

        # 分批進場：先進 50%
        staged_remaining = 0.0
        if self.config.staged_entry:
            units = self._round_units(full_units * self.config.staged_entry_pct, signal.symbol)
            staged_remaining = self._round_units(full_units - units, signal.symbol)
            if units <= 0:
                units = full_units
                staged_remaining = 0.0
        else:
            units = full_units

        # 下主單（市價單 + SL）
        staged_tag = f" [首批 {self.config.staged_entry_pct:.0%}]" if staged_remaining > 0 else ""
        logger.info(
            f"下單: {signal.symbol} {signal.side.value} "
            f"units={units}{staged_tag} sl={signal.sl} tp1~tp4={signal.tp1}/{signal.tp2}/{signal.tp3}/{signal.tp4}"
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
            # 保證金不足 → 加入重試佇列
            if "Insufficient margin" in (result.error or "") or "101204" in (result.error or ""):
                self._retry_queue.append((signal, 1))
                logger.info(f"[{signal.symbol}] 已加入重試佇列（第 1 次，{self.RETRY_INTERVAL}s 後重試）")
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
            staged_entry=staged_remaining > 0,
            staged_remaining_units=staged_remaining,
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
        """分批平倉。TP1 RR ≥ 1 用 45/30/15/10，否則 0/0/50/50"""
        # 動態判斷：TP1 RR ≥ 1R → 高 RR 模式（45/30/15/10）
        risk = abs(state.entry_price - state.sl_original) if state.sl_original else 0
        tp1_rr = abs(state.tp1 - state.entry_price) / risk if risk > 0 and state.tp1 else 0
        if tp1_rr >= 1.0:
            pct_map = {"tp1": 0.45, "tp2": 0.30, "tp3": 0.15, "tp4": 0.10}
        else:
            pct_map = {"tp1": self.config.tp1_pct, "tp2": self.config.tp2_pct,
                       "tp3": self.config.tp3_pct, "tp4": self.config.tp4_pct}
        pct = pct_map.get(tp_level, 0)
        if pct <= 0:
            # 這層不出場（如 TP1/TP2 = 0%），只標記觸及
            logger.info(f"[{state.symbol}] {tp_level} 觸及，不出場（{source}）")
            if tp_level == "tp1":
                state.tp1_closed = True
                if state.staged_entry and not state.staged_added and state.staged_remaining_units > 0:
                    added = await self._staged_add_position(state)
                    if not added:
                        logger.warning(f"[{state.symbol}] TP1 加倉失敗，只持有首批倉位")
                else:
                    logger.info(f"[{state.symbol}] TP1 觸及，繼續持倉")
            elif tp_level == "tp2":
                state.tp2_closed = True
                logger.info(f"[{state.symbol}] TP2 觸及，繼續持倉（保本已由 0.5R 觸發）")
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

            # PaperBroker: 用 partial_close 方法
            paper_broker = self._get_paper_broker()
            if paper_broker:
                result = await paper_broker.partial_close(state.symbol, state.side.value, units)
                if result.success:
                    tracker = self._get_tracker(state.symbol)
                    rpnl = tracker.add_trade(close_side, units, result.entry_price)
                    logger.info(f"[{state.symbol}] 部分平倉 {units} units 成功 (realized: ${rpnl:+.4f})")
                    return True
                else:
                    logger.error(f"部分平倉失敗 {state.symbol}: {result.error}")
                    return False

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
            # PaperBroker 不需要清理掛單
            if self._get_paper_broker():
                return

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
                # 用 info.type（原始值）判斷，ccxt 會把 STOP_MARKET normalize 成 "market"
                raw_type = str(order.get("info", {}).get("type", "")).upper()
                oside = str(order.get("side", "")).lower()
                order_pos_side = order.get("info", {}).get("positionSide", "")

                # 只取消屬於這個方向的 TP/SL 單
                is_protection = "STOP" in raw_type or "TAKE_PROFIT" in raw_type
                if is_protection and oside == close_side:
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
            # PaperBroker: 查內部持倉
            paper = self._get_paper_broker()
            if paper:
                key = f"{state.symbol}_{state.side.value}"
                pos = paper._positions.get(key)
                return pos is not None and pos.units > 0

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
        # PaperBroker: 從 executor state 恢復持倉
        paper = self._get_paper_broker()
        if paper:
            for key, state in self.active_trades.items():
                if not state.closed and state.remaining_units > 0:
                    pos_key = f"{state.symbol}_{state.side.value}"
                    if pos_key not in paper._positions:
                        from src.trader.paper import _PaperPosition
                        pos = _PaperPosition(state.symbol, state.side.value)
                        pos.add(state.remaining_units, state.entry_price)
                        paper._positions[pos_key] = pos
                        logger.info(f"[Paper] 恢復持倉: {state.symbol} {state.side.value} {state.remaining_units} @ {state.entry_price}")
        else:
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
        _retry_counter = 0
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
                # 每 3 輪（約 30 秒）處理重試佇列
                _retry_counter += 1
                if _retry_counter >= 3 and self._retry_queue:
                    _retry_counter = 0
                    await self._process_retry_queue()
            except Exception as e:
                logger.error(f"監控異常: {e}")
            await asyncio.sleep(self.config.monitor_interval)

    async def _process_retry_queue(self) -> None:
        """處理保證金不足的重試佇列"""
        if not self._retry_queue:
            return

        pending = list(self._retry_queue)
        self._retry_queue.clear()

        for signal, attempt in pending:
            key = signal.related_signal_key or f"{signal.symbol}_{signal.side.value}_{signal.timeframe}_{signal.entry}"
            # 已經開倉了（其他途徑成功）→ 跳過
            if key in self.active_trades and not self.active_trades[key].closed:
                logger.info(f"[{signal.symbol}] 重試跳過：已有持倉")
                continue

            logger.info(f"[{signal.symbol}] 重試下單（第 {attempt} 次）")
            result = await self.execute_signal(signal)

            if result and result.success:
                logger.info(f"[{signal.symbol}] 重試成功 trade_id={result.trade_id}")
            elif result and not result.success:
                # execute_signal 內部已經會把 margin 失敗加回佇列，
                # 但 attempt 會是 1，這裡要修正為累加
                if self._retry_queue and self._retry_queue[-1][0] is signal:
                    self._retry_queue[-1] = (signal, attempt + 1)
                    if attempt + 1 > self.RETRY_MAX:
                        self._retry_queue.pop()
                        logger.warning(f"[{signal.symbol}] 已重試 {self.RETRY_MAX} 次仍失敗，放棄")
                    else:
                        logger.info(f"[{signal.symbol}] 第 {attempt} 次重試仍失敗，等待下次（{attempt + 1}/{self.RETRY_MAX}）")

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
                    if not has_position:
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

                # 0/0/50/50 + TP2 保本（SL 移到 Entry）
                # TP2 保本：碰 TP2 後 SL 移到 entry（含手續費）
                if not state.sl_moved_to_be and state.tp2_closed:
                    fee_pct = 0.001
                    if is_long:
                        be_price = round(state.entry_price * (1 + fee_pct), 6)
                    else:
                        be_price = round(state.entry_price * (1 - fee_pct), 6)
                    try:
                        ok = await self.broker.modify_trade(
                            state.trade_id, sl=be_price,
                            symbol=state.symbol, side=state.side.value)
                        if ok:
                            state.sl_current = be_price
                            state.sl_moved_to_be = True
                            logger.info(f"[{state.symbol}] TP2 保本 → SL 移到 {be_price}")
                            self._save_state()
                    except Exception as e:
                        logger.debug(f"[{state.symbol}] 保本失敗: {e}")

                # 逐層檢查 TP
                tp_levels = [
                    ("tp1", state.tp1, state.tp1_closed),
                    ("tp2", state.tp2, state.tp2_closed),
                    ("tp3", state.tp3, state.tp3_closed),
                    ("tp4", state.tp4, state.tp4_closed),
                ]
                for tp_name, tp_price, tp_done in tp_levels:
                    if not tp_price or tp_done:
                        continue
                    hit = (is_long and current >= tp_price) or (not is_long and current <= tp_price)
                    if hit:
                        await self._partial_close_by_level(state, tp_name, source="價格監控")

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

    def _round_units(self, units: float, symbol: str) -> float:
        """用 broker 精度或 fallback 精度取整"""
        if hasattr(self.broker, "get_market"):
            mkt = self.broker.get_market(symbol)
            if mkt:
                return mkt.round_quantity(units)
        sym = symbol.upper().replace(".P", "")
        if sym.endswith("USDT"):
            entry_approx = 1  # fallback
            if units > 1000: return round(units, 0)
            if units > 10: return round(units, 1)
            return round(units, 2)
        return max(int(units), 1)

    async def _staged_add_position(self, state: TradeState) -> bool:
        """TP1 觸發後加倉剩餘 50%"""
        try:
            add_units = state.staged_remaining_units
            if add_units <= 0:
                return False

            result = await self.broker.market_order(
                symbol=state.symbol,
                side=state.side,
                units=add_units,
                sl=state.sl_current,
                tp=state.tp4,
                tp3=state.tp3,
            )
            if result.success:
                state.total_units += add_units
                state.remaining_units += add_units
                state.staged_remaining_units = 0.0
                state.staged_added = True
                self._save_state()
                logger.info(f"[{state.symbol}] TP1 加倉成功: +{add_units} units (總 {state.total_units})")
                return True
            else:
                logger.warning(f"[{state.symbol}] TP1 加倉失敗: {result.error}")
                return False
        except Exception as e:
            logger.error(f"[{state.symbol}] TP1 加倉異常: {e}")
            return False

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

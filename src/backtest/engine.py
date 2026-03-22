"""
回測 / 模擬執行引擎（核心模組）

職責：
- 根據信號 + K 線資料，模擬交易結果
- 支援多種出場模式：single_tp / partial_tp / breakeven / custom
- 處理同 K 線觸發衝突（保守/樂觀模式）
- 處理信號過期

輸入：SignalORM + list[Candle] + BacktestConfig
輸出：TradeResult
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from src.models import (
    Candle,
    SignalORM,
    SignalSide,
    BacktestConfig,
    TradeResult,
    ExitReason,
    AmbiguousMode,
)

logger = logging.getLogger(__name__)


def simulate_trade(
    signal: SignalORM,
    candles: list[Candle],
    config: BacktestConfig,
) -> TradeResult:
    """
    根據信號與 K 線模擬單筆交易。

    流程：
    1. 檢查是否在 expiry 內觸發 entry
    2. 進場後逐根 K 線檢查是否碰到 TP / SL
    3. 根據 config.mode 計算最終 pnl_r

    Args:
        signal: 交易信號
        candles: 排序好的 K 線（從信號時間開始）
        config: 回測配置

    Returns:
        TradeResult
    """
    if not candles:
        return _not_triggered(signal.id, "no candle data")

    is_long = signal.side == SignalSide.LONG
    entry = signal.entry
    sl = signal.sl
    risk = abs(entry - sl)

    if risk == 0:
        return _not_triggered(signal.id, "zero risk (entry == sl)")

    tps = _get_tps(signal)

    # ── Phase 1: 找進場點 ──
    entry_time = None
    entry_candle_idx = None

    for i, c in enumerate(candles):
        # 檢查過期
        bars_elapsed = i + 1
        hours_elapsed = (c.open_time - candles[0].open_time).total_seconds() / 3600

        if bars_elapsed > config.signal_expiry_bars or hours_elapsed > config.signal_expiry_hours:
            return _not_triggered(signal.id, f"expired after {bars_elapsed} bars / {hours_elapsed:.0f}h")

        # 檢查是否觸及 entry
        if _price_touched(c, entry):
            entry_time = c.open_time
            entry_candle_idx = i
            break

    if entry_time is None:
        return _not_triggered(signal.id, "price never reached entry")

    # ── Phase 2: 模擬持倉 ──
    if config.mode == "single_tp":
        return _simulate_single_tp(signal.id, is_long, entry, sl, risk, tps, config, candles, entry_candle_idx, entry_time)
    elif config.mode == "partial_tp":
        return _simulate_partial_tp(signal.id, is_long, entry, sl, risk, tps, config, candles, entry_candle_idx, entry_time)
    elif config.mode == "breakeven":
        return _simulate_breakeven(signal.id, is_long, entry, sl, risk, tps, config, candles, entry_candle_idx, entry_time)
    elif config.mode == "partial_be":
        return _simulate_partial_be(signal.id, is_long, entry, sl, risk, tps, config, candles, entry_candle_idx, entry_time)
    else:
        return _simulate_partial_be(signal.id, is_long, entry, sl, risk, tps, config, candles, entry_candle_idx, entry_time)


# ============================================================
# Mode A: 固定單一 TP 出場
# ============================================================

def _simulate_single_tp(
    signal_id: int, is_long: bool, entry: float, sl: float, risk: float,
    tps: dict[str, float], config: BacktestConfig,
    candles: list[Candle], entry_idx: int, entry_time: datetime,
) -> TradeResult:
    target_key = config.target_tp  # e.g. "tp2"
    target_price = tps.get(target_key)

    if target_price is None:
        # 沒有這個 TP level，降級到有的最高 TP
        for fallback in ["tp4", "tp3", "tp2", "tp1"]:
            if tps.get(fallback) is not None:
                target_price = tps[fallback]
                target_key = fallback
                break
        if target_price is None:
            return _not_triggered(signal_id, "no TP target available")

    max_tp_hit = 0
    max_drawdown = 0.0

    for c in candles[entry_idx + 1:]:
        hit_sl = _price_touched(c, sl)
        hit_tp = _price_touched(c, target_price)

        # 追蹤最高觸及的 TP
        for level in [1, 2, 3, 4]:
            tp_val = tps.get(f"tp{level}")
            if tp_val and _price_touched(c, tp_val):
                max_tp_hit = max(max_tp_hit, level)

        # 追蹤回撤
        if is_long:
            dd = (entry - c.low) / risk if c.low < entry else 0
        else:
            dd = (c.high - entry) / risk if c.high > entry else 0
        max_drawdown = max(max_drawdown, dd)

        # 同 K 線衝突處理
        if hit_sl and hit_tp:
            if config.ambiguous_mode == AmbiguousMode.CONSERVATIVE:
                # 保守：算 SL
                pnl_r = -1.0
                return TradeResult(
                    signal_id=signal_id, triggered=True,
                    entry_time=entry_time, exit_time=c.open_time,
                    exit_reason=ExitReason.SL_HIT, exit_price=sl,
                    max_tp_hit=max_tp_hit, pnl_r=pnl_r,
                    pnl_pct=_r_to_pct(pnl_r, entry, risk),
                    drawdown_r=max_drawdown,
                    notes=f"ambiguous candle → conservative SL",
                )
            else:
                # 樂觀：算 TP
                pnl_r = abs(target_price - entry) / risk
                return TradeResult(
                    signal_id=signal_id, triggered=True,
                    entry_time=entry_time, exit_time=c.open_time,
                    exit_reason=ExitReason.TP_HIT, exit_price=target_price,
                    max_tp_hit=max_tp_hit, pnl_r=pnl_r,
                    pnl_pct=_r_to_pct(pnl_r, entry, risk),
                    drawdown_r=max_drawdown,
                    notes=f"ambiguous candle → optimistic TP ({target_key})",
                )

        if hit_sl:
            return TradeResult(
                signal_id=signal_id, triggered=True,
                entry_time=entry_time, exit_time=c.open_time,
                exit_reason=ExitReason.SL_HIT, exit_price=sl,
                max_tp_hit=max_tp_hit, pnl_r=-1.0,
                pnl_pct=_r_to_pct(-1.0, entry, risk),
                drawdown_r=max_drawdown,
            )

        if hit_tp:
            pnl_r = abs(target_price - entry) / risk
            return TradeResult(
                signal_id=signal_id, triggered=True,
                entry_time=entry_time, exit_time=c.open_time,
                exit_reason=ExitReason.TP_HIT, exit_price=target_price,
                max_tp_hit=max_tp_hit, pnl_r=pnl_r,
                pnl_pct=_r_to_pct(pnl_r, entry, risk),
                drawdown_r=max_drawdown,
                notes=f"hit {target_key}",
            )

    # K 線跑完都沒結束 → 視為持倉中（用最後收盤價結算）
    last = candles[-1]
    if is_long:
        unrealized_r = (last.close - entry) / risk
    else:
        unrealized_r = (entry - last.close) / risk

    return TradeResult(
        signal_id=signal_id, triggered=True,
        entry_time=entry_time, exit_time=last.open_time,
        exit_reason=ExitReason.EXPIRED,
        exit_price=last.close,
        max_tp_hit=max_tp_hit, pnl_r=unrealized_r,
        pnl_pct=_r_to_pct(unrealized_r, entry, risk),
        drawdown_r=max_drawdown,
        notes="still open at end of data",
    )


# ============================================================
# Mode B: 分批止盈（Phase 2 擴充，先留介面）
# ============================================================

def _simulate_partial_tp(
    signal_id: int, is_long: bool, entry: float, sl: float, risk: float,
    tps: dict[str, float], config: BacktestConfig,
    candles: list[Candle], entry_idx: int, entry_time: datetime,
) -> TradeResult:
    """分批止盈模擬 — Phase 2 實作"""
    weights = config.partial_weights
    remaining = 1.0  # 剩餘倉位比例
    total_pnl_r = 0.0
    max_tp_hit = 0
    max_drawdown = 0.0
    current_sl = sl

    for c in candles[entry_idx + 1:]:
        # 追蹤回撤
        if is_long:
            dd = (entry - c.low) / risk if c.low < entry else 0
        else:
            dd = (c.high - entry) / risk if c.high > entry else 0
        max_drawdown = max(max_drawdown, dd)

        # 檢查 SL
        if _price_touched(c, current_sl):
            # 剩餘倉位全部打 SL
            sl_pnl = remaining * (abs(current_sl - entry) / risk)
            if (is_long and current_sl < entry) or (not is_long and current_sl > entry):
                sl_pnl = -sl_pnl
            total_pnl_r += sl_pnl
            return TradeResult(
                signal_id=signal_id, triggered=True,
                entry_time=entry_time, exit_time=c.open_time,
                exit_reason=ExitReason.SL_HIT, exit_price=current_sl,
                max_tp_hit=max_tp_hit, pnl_r=total_pnl_r,
                pnl_pct=_r_to_pct(total_pnl_r, entry, risk),
                drawdown_r=max_drawdown,
                notes=f"partial: remaining {remaining:.0%} hit SL",
            )

        # 檢查各 TP level（由低到高）
        for level in [1, 2, 3, 4]:
            tp_key = f"tp{level}"
            tp_val = tps.get(tp_key)
            if tp_val is None or level <= max_tp_hit:
                continue
            if _price_touched(c, tp_val):
                max_tp_hit = level
                w = weights.get(tp_key, 0.25)
                portion = min(w, remaining)
                pnl = portion * (abs(tp_val - entry) / risk)
                total_pnl_r += pnl
                remaining -= portion

        if remaining <= 0:
            return TradeResult(
                signal_id=signal_id, triggered=True,
                entry_time=entry_time, exit_time=c.open_time,
                exit_reason=ExitReason.TP_HIT, exit_price=tps.get(f"tp{max_tp_hit}", entry),
                max_tp_hit=max_tp_hit, pnl_r=total_pnl_r,
                pnl_pct=_r_to_pct(total_pnl_r, entry, risk),
                drawdown_r=max_drawdown,
                notes="all partials filled",
            )

    # 資料結束
    last = candles[-1]
    if is_long:
        unrealized = remaining * ((last.close - entry) / risk)
    else:
        unrealized = remaining * ((entry - last.close) / risk)
    total_pnl_r += unrealized

    return TradeResult(
        signal_id=signal_id, triggered=True,
        entry_time=entry_time, exit_time=last.open_time,
        exit_reason=ExitReason.EXPIRED, exit_price=last.close,
        max_tp_hit=max_tp_hit, pnl_r=total_pnl_r,
        pnl_pct=_r_to_pct(total_pnl_r, entry, risk),
        drawdown_r=max_drawdown,
        notes=f"still open: {remaining:.0%} remaining",
    )


# ============================================================
# Mode D: 分批止盈 + 碰 TP1 保本（實盤策略）
# TP1=40% TP2=30% TP3=15% TP4=15% + 碰 TP1 後 SL→Entry
# ============================================================

def _simulate_partial_be(
    signal_id: int, is_long: bool, entry: float, sl: float, risk: float,
    tps: dict[str, float], config: BacktestConfig,
    candles: list[Candle], entry_idx: int, entry_time: datetime,
) -> TradeResult:
    """分批止盈 + 碰 TP1 後保本"""
    weights = config.partial_weights
    remaining = 1.0
    total_pnl_r = 0.0
    max_tp_hit = 0
    max_drawdown = 0.0
    current_sl = sl
    be_activated = False

    for c in candles[entry_idx + 1:]:
        # 追蹤回撤
        if is_long:
            dd = (entry - c.low) / risk if c.low < entry else 0
        else:
            dd = (c.high - entry) / risk if c.high > entry else 0
        max_drawdown = max(max_drawdown, dd)

        # 檢查 SL（含保本後的 SL）
        if _price_touched(c, current_sl):
            if be_activated and current_sl == entry:
                # 保本出場：剩餘倉位 0R
                return TradeResult(
                    signal_id=signal_id, triggered=True,
                    entry_time=entry_time, exit_time=c.open_time,
                    exit_reason=ExitReason.BREAKEVEN, exit_price=entry,
                    max_tp_hit=max_tp_hit, pnl_r=total_pnl_r,
                    pnl_pct=_r_to_pct(total_pnl_r, entry, risk),
                    drawdown_r=max_drawdown,
                    notes=f"BE after TP1, partial profit locked: {total_pnl_r:+.2f}R",
                )
            else:
                # 原始 SL：剩餘倉位全虧
                sl_pnl = -remaining  # 剩餘倉位 × -1R
                total_pnl_r += sl_pnl
                return TradeResult(
                    signal_id=signal_id, triggered=True,
                    entry_time=entry_time, exit_time=c.open_time,
                    exit_reason=ExitReason.SL_HIT, exit_price=current_sl,
                    max_tp_hit=max_tp_hit, pnl_r=total_pnl_r,
                    pnl_pct=_r_to_pct(total_pnl_r, entry, risk),
                    drawdown_r=max_drawdown,
                    notes=f"SL hit, remaining {remaining:.0%}",
                )

        # 檢查各 TP level（由低到高）
        for level in [1, 2, 3, 4]:
            tp_key = f"tp{level}"
            tp_val = tps.get(tp_key)
            if tp_val is None or level <= max_tp_hit:
                continue
            if _price_touched(c, tp_val):
                max_tp_hit = level
                w = weights.get(tp_key, 0.25)
                portion = min(w, remaining)
                pnl = portion * (abs(tp_val - entry) / risk)
                total_pnl_r += pnl
                remaining -= portion

                # 碰 TP1 後保本
                if level == 1 and not be_activated:
                    current_sl = entry
                    be_activated = True

        if remaining <= 0:
            return TradeResult(
                signal_id=signal_id, triggered=True,
                entry_time=entry_time, exit_time=c.open_time,
                exit_reason=ExitReason.TP_HIT, exit_price=tps.get(f"tp{max_tp_hit}", entry),
                max_tp_hit=max_tp_hit, pnl_r=total_pnl_r,
                pnl_pct=_r_to_pct(total_pnl_r, entry, risk),
                drawdown_r=max_drawdown,
                notes="all partials filled",
            )

    # 資料結束
    last = candles[-1]
    if is_long:
        unrealized = remaining * ((last.close - entry) / risk)
    else:
        unrealized = remaining * ((entry - last.close) / risk)
    total_pnl_r += unrealized

    return TradeResult(
        signal_id=signal_id, triggered=True,
        entry_time=entry_time, exit_time=last.open_time,
        exit_reason=ExitReason.EXPIRED, exit_price=last.close,
        max_tp_hit=max_tp_hit, pnl_r=total_pnl_r,
        pnl_pct=_r_to_pct(total_pnl_r, entry, risk),
        drawdown_r=max_drawdown,
        notes=f"still open: {remaining:.0%} remaining" + (" (BE active)" if be_activated else ""),
    )


# ============================================================
# Mode C: 到 TP1 後保本
# ============================================================

def _simulate_breakeven(
    signal_id: int, is_long: bool, entry: float, sl: float, risk: float,
    tps: dict[str, float], config: BacktestConfig,
    candles: list[Candle], entry_idx: int, entry_time: datetime,
) -> TradeResult:
    """到 TP1 後 SL 移到 Entry（保本）"""
    target_key = config.target_tp
    target_price = tps.get(target_key)
    be_trigger = tps.get(config.move_sl_after)

    if target_price is None:
        return _not_triggered(signal_id, "no TP target for breakeven mode")

    current_sl = sl
    be_activated = False
    max_tp_hit = 0
    max_drawdown = 0.0

    for c in candles[entry_idx + 1:]:
        # 追蹤回撤
        if is_long:
            dd = (entry - c.low) / risk if c.low < entry else 0
        else:
            dd = (c.high - entry) / risk if c.high > entry else 0
        max_drawdown = max(max_drawdown, dd)

        # 追蹤 TP 觸及
        for level in [1, 2, 3, 4]:
            tp_val = tps.get(f"tp{level}")
            if tp_val and _price_touched(c, tp_val):
                max_tp_hit = max(max_tp_hit, level)

        # 檢查是否觸發保本
        if not be_activated and be_trigger and _price_touched(c, be_trigger):
            current_sl = entry  # SL 移到 entry
            be_activated = True

        # 檢查 SL
        if _price_touched(c, current_sl):
            if be_activated and current_sl == entry:
                return TradeResult(
                    signal_id=signal_id, triggered=True,
                    entry_time=entry_time, exit_time=c.open_time,
                    exit_reason=ExitReason.BREAKEVEN, exit_price=entry,
                    max_tp_hit=max_tp_hit, pnl_r=0.0, pnl_pct=0.0,
                    drawdown_r=max_drawdown,
                    notes=f"BE after hitting {config.move_sl_after}",
                )
            else:
                return TradeResult(
                    signal_id=signal_id, triggered=True,
                    entry_time=entry_time, exit_time=c.open_time,
                    exit_reason=ExitReason.SL_HIT, exit_price=current_sl,
                    max_tp_hit=max_tp_hit, pnl_r=-1.0,
                    pnl_pct=_r_to_pct(-1.0, entry, risk),
                    drawdown_r=max_drawdown,
                )

        # 檢查 TP
        if _price_touched(c, target_price):
            pnl_r = abs(target_price - entry) / risk
            return TradeResult(
                signal_id=signal_id, triggered=True,
                entry_time=entry_time, exit_time=c.open_time,
                exit_reason=ExitReason.TP_HIT, exit_price=target_price,
                max_tp_hit=max_tp_hit, pnl_r=pnl_r,
                pnl_pct=_r_to_pct(pnl_r, entry, risk),
                drawdown_r=max_drawdown,
                notes=f"hit {target_key}" + (" (BE was active)" if be_activated else ""),
            )

    last = candles[-1]
    if is_long:
        unrealized_r = (last.close - entry) / risk
    else:
        unrealized_r = (entry - last.close) / risk

    return TradeResult(
        signal_id=signal_id, triggered=True,
        entry_time=entry_time, exit_time=last.open_time,
        exit_reason=ExitReason.EXPIRED, exit_price=last.close,
        max_tp_hit=max_tp_hit, pnl_r=unrealized_r,
        pnl_pct=_r_to_pct(unrealized_r, entry, risk),
        drawdown_r=max_drawdown,
        notes="still open" + (" (BE active)" if be_activated else ""),
    )


# ============================================================
# Helpers
# ============================================================

def _price_touched(candle: Candle, price: float) -> bool:
    """判斷這根 K 線是否觸及指定價格"""
    return candle.low <= price <= candle.high


def _get_tps(signal: SignalORM) -> dict[str, float]:
    """從 signal 取出所有 TP levels"""
    tps = {}
    for i in [1, 2, 3, 4]:
        val = getattr(signal, f"tp{i}", None)
        if val is not None:
            tps[f"tp{i}"] = val
    return tps


def _r_to_pct(r_value: float, entry: float, risk: float) -> float:
    """R 值轉換為百分比"""
    if entry == 0:
        return 0.0
    return (r_value * risk / entry) * 100


def _not_triggered(signal_id: int, reason: str) -> TradeResult:
    return TradeResult(
        signal_id=signal_id,
        triggered=False,
        notes=reason,
    )

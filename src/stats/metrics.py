"""
績效統計模組

職責：
- 從 TradeResult 列表計算各項績效指標
- 產出 PerformanceMetrics
- 產出 Equity Curve 資料
- 支援匯出 CSV / JSON

輸入：list[TradeResult]
輸出：PerformanceMetrics, equity curve, CSV/JSON
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from io import StringIO
from typing import Optional

import pandas as pd

from src.models import TradeResult, PerformanceMetrics, ExitReason

logger = logging.getLogger(__name__)


def compute_metrics(results: list[TradeResult]) -> PerformanceMetrics:
    """
    從交易結果計算完整績效指標。
    """
    total = len(results)
    triggered = [r for r in results if r.triggered]
    not_triggered = [r for r in results if not r.triggered]

    if not triggered:
        return PerformanceMetrics(
            total_signals=total,
            triggered_count=0,
            not_triggered_count=len(not_triggered),
        )

    wins = [r for r in triggered if r.pnl_r > 0]
    losses = [r for r in triggered if r.pnl_r < 0]
    breakevens = [r for r in triggered if r.pnl_r == 0]

    n_triggered = len(triggered)
    win_rate = len(wins) / n_triggered if n_triggered else 0
    loss_rate = len(losses) / n_triggered if n_triggered else 0

    avg_win_r = sum(r.pnl_r for r in wins) / len(wins) if wins else 0
    avg_loss_r = sum(r.pnl_r for r in losses) / len(losses) if losses else 0
    avg_rr = abs(avg_win_r / avg_loss_r) if avg_loss_r != 0 else 0

    # 期望值 = (勝率 × 平均盈利) + (敗率 × 平均虧損)
    expectancy = (win_rate * avg_win_r) + (loss_rate * avg_loss_r)

    total_r = sum(r.pnl_r for r in triggered)

    # 最大回撤（以 R 為單位的 equity curve drawdown）
    max_dd = _compute_max_drawdown([r.pnl_r for r in triggered])

    # 連勝 / 連敗
    max_cons_wins, max_cons_losses = _compute_streaks(triggered)

    # TP 觸達統計
    tp1_hits = sum(1 for r in triggered if r.max_tp_hit >= 1)
    tp2_hits = sum(1 for r in triggered if r.max_tp_hit >= 2)
    tp3_hits = sum(1 for r in triggered if r.max_tp_hit >= 3)
    tp4_hits = sum(1 for r in triggered if r.max_tp_hit >= 4)

    # 碰 TP1 但最後打 SL 的比例
    tp1_then_sl = sum(
        1 for r in triggered
        if r.max_tp_hit >= 1 and r.exit_reason == ExitReason.SL_HIT
    )
    tp1_then_sl_rate = tp1_then_sl / tp1_hits if tp1_hits else 0

    return PerformanceMetrics(
        total_signals=total,
        triggered_count=n_triggered,
        not_triggered_count=len(not_triggered),
        win_count=len(wins),
        loss_count=len(losses),
        breakeven_count=len(breakevens),
        win_rate=round(win_rate, 4),
        loss_rate=round(loss_rate, 4),
        avg_win_r=round(avg_win_r, 4),
        avg_loss_r=round(avg_loss_r, 4),
        avg_rr=round(avg_rr, 4),
        expectancy=round(expectancy, 4),
        total_r=round(total_r, 4),
        max_drawdown_r=round(max_dd, 4),
        max_consecutive_wins=max_cons_wins,
        max_consecutive_losses=max_cons_losses,
        tp1_hit_rate=round(tp1_hits / n_triggered, 4) if n_triggered else 0,
        tp2_hit_rate=round(tp2_hits / n_triggered, 4) if n_triggered else 0,
        tp3_hit_rate=round(tp3_hits / n_triggered, 4) if n_triggered else 0,
        tp4_hit_rate=round(tp4_hits / n_triggered, 4) if n_triggered else 0,
        tp1_hit_then_sl_rate=round(tp1_then_sl_rate, 4),
    )


def build_equity_curve(results: list[TradeResult]) -> pd.DataFrame:
    """
    建立累積 R 值的 equity curve。

    Returns:
        DataFrame with columns: [time, pnl_r, cumulative_r]
    """
    triggered = [r for r in results if r.triggered and r.exit_time]
    triggered.sort(key=lambda r: r.exit_time)

    data = []
    cum_r = 0.0
    for r in triggered:
        cum_r += r.pnl_r
        data.append({
            "time": r.exit_time,
            "signal_id": r.signal_id,
            "pnl_r": round(r.pnl_r, 4),
            "cumulative_r": round(cum_r, 4),
        })

    return pd.DataFrame(data)


def export_csv(results: list[TradeResult], filepath: str) -> None:
    """匯出交易結果為 CSV"""
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "signal_id", "triggered", "entry_time", "exit_time",
            "exit_reason", "exit_price", "max_tp_hit",
            "pnl_r", "pnl_pct", "drawdown_r", "notes",
        ])
        for r in results:
            writer.writerow([
                r.signal_id, r.triggered, r.entry_time, r.exit_time,
                r.exit_reason.value if r.exit_reason else "",
                r.exit_price, r.max_tp_hit,
                round(r.pnl_r, 4), round(r.pnl_pct, 4),
                round(r.drawdown_r, 4), r.notes,
            ])
    logger.info(f"CSV 已匯出: {filepath}")


def export_json(results: list[TradeResult], filepath: str) -> None:
    """匯出交易結果為 JSON"""
    data = []
    for r in results:
        d = r.model_dump()
        # datetime → string
        for key in ["entry_time", "exit_time"]:
            if d.get(key):
                d[key] = d[key].isoformat()
        if d.get("exit_reason"):
            d["exit_reason"] = d["exit_reason"].value
        data.append(d)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info(f"JSON 已匯出: {filepath}")


# ============================================================
# Helpers
# ============================================================

def _compute_max_drawdown(pnl_series: list[float]) -> float:
    """計算最大回撤（以 R 為單位）"""
    if not pnl_series:
        return 0.0

    peak = 0.0
    cum = 0.0
    max_dd = 0.0

    for pnl in pnl_series:
        cum += pnl
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    return max_dd


def _compute_streaks(results: list[TradeResult]) -> tuple[int, int]:
    """計算最大連勝 / 連敗"""
    max_wins = 0
    max_losses = 0
    current_wins = 0
    current_losses = 0

    for r in results:
        if r.pnl_r > 0:
            current_wins += 1
            current_losses = 0
            max_wins = max(max_wins, current_wins)
        elif r.pnl_r < 0:
            current_losses += 1
            current_wins = 0
            max_losses = max(max_losses, current_losses)
        else:
            current_wins = 0
            current_losses = 0

    return max_wins, max_losses

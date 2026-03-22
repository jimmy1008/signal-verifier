"""
時間切片分析

職責：
- 將交易按時段分類（亞洲盤 / 歐洲盤 / 美盤）
- 分析各時段的 edge 是否穩定
- 找出最強 / 最弱時段

輸入：list[TradeResult] + SignalORM（取 signal_time）
輸出：時段績效 dict

時段定義（UTC）：
  亞洲盤: 00:00 - 08:00 UTC (08:00-16:00 台北)
  歐洲盤: 08:00 - 14:00 UTC (16:00-22:00 台北)
  美盤:   14:00 - 21:00 UTC (22:00-05:00 台北)
  交叉盤: 21:00 - 00:00 UTC (05:00-08:00 台北)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from src.models import TradeResult, SignalORM, PerformanceMetrics
from src.stats.metrics import compute_metrics

logger = logging.getLogger(__name__)


class SessionName:
    ASIA = "asia"         # 亞洲盤
    EUROPE = "europe"     # 歐洲盤
    US = "us"             # 美盤
    CROSS = "cross"       # 交叉盤


class SessionStats(BaseModel):
    """單一時段的統計"""
    session_name: str
    trade_count: int = 0
    metrics: Optional[PerformanceMetrics] = None


class TimeAnalysisResult(BaseModel):
    """時間切片分析結果"""
    sessions: dict[str, SessionStats] = Field(default_factory=dict)
    best_session: Optional[str] = None
    worst_session: Optional[str] = None
    edge_stable: bool = False          # 各時段 edge 是否一致
    edge_distribution: str = ""        # 人類可讀的描述
    reasons: list[str] = Field(default_factory=list)


def get_session_name(utc_hour: int) -> str:
    """根據 UTC 小時判斷屬於哪個盤"""
    if 0 <= utc_hour < 8:
        return SessionName.ASIA
    elif 8 <= utc_hour < 14:
        return SessionName.EUROPE
    elif 14 <= utc_hour < 21:
        return SessionName.US
    else:
        return SessionName.CROSS


def analyze_by_session(
    results: list[TradeResult],
    signals: dict[int, SignalORM],
) -> TimeAnalysisResult:
    """
    按交易時段分組，分別計算績效。

    Args:
        results: 交易結果列表
        signals: signal_id → SignalORM 映射

    Returns:
        TimeAnalysisResult
    """
    # 按時段分組
    grouped: dict[str, list[TradeResult]] = defaultdict(list)

    for r in results:
        sig = signals.get(r.signal_id)
        if not sig:
            continue

        # 用信號發出時間判斷時段
        hour = sig.signal_time.hour  # 假設存的是 UTC
        session = get_session_name(hour)
        grouped[session].append(r)

    # 各時段計算統計
    session_stats: dict[str, SessionStats] = {}
    for session_name in [SessionName.ASIA, SessionName.EUROPE, SessionName.US, SessionName.CROSS]:
        trades = grouped.get(session_name, [])
        if trades:
            metrics = compute_metrics(trades)
        else:
            metrics = None

        session_stats[session_name] = SessionStats(
            session_name=session_name,
            trade_count=len(trades),
            metrics=metrics,
        )

    # 找最強 / 最弱
    active_sessions = {
        k: v for k, v in session_stats.items()
        if v.metrics and v.trade_count >= 3
    }

    best = None
    worst = None
    reasons = []

    if active_sessions:
        best = max(active_sessions, key=lambda k: active_sessions[k].metrics.expectancy)
        worst = min(active_sessions, key=lambda k: active_sessions[k].metrics.expectancy)

        # 判定 edge 穩定性
        positive_sessions = sum(
            1 for v in active_sessions.values()
            if v.metrics.expectancy > 0
        )
        negative_sessions = sum(
            1 for v in active_sessions.values()
            if v.metrics.expectancy <= 0
        )

        total_active = len(active_sessions)
        edge_stable = positive_sessions >= total_active * 0.6

        if edge_stable:
            reasons.append(f"edge_consistent: {positive_sessions}/{total_active} sessions profitable")
        else:
            reasons.append(
                f"edge_inconsistent: only {positive_sessions}/{total_active} sessions profitable"
            )

        # 極端差異警告
        if best and worst and active_sessions[best].metrics and active_sessions[worst].metrics:
            best_exp = active_sessions[best].metrics.expectancy
            worst_exp = active_sessions[worst].metrics.expectancy
            if best_exp > 0 and worst_exp < -0.1:
                reasons.append(
                    f"session_divergence: {_session_label(best)} expectancy {best_exp:+.4f}R "
                    f"vs {_session_label(worst)} expectancy {worst_exp:+.4f}R"
                )

        edge_parts = []
        for name, s in active_sessions.items():
            label = _session_label(name)
            exp = s.metrics.expectancy
            edge_parts.append(f"{label}: {exp:+.4f}R ({s.trade_count} trades)")
        distribution = " | ".join(edge_parts)
    else:
        edge_stable = False
        distribution = "insufficient data"
        reasons.append("not_enough_data: fewer than 3 trades per session")

    return TimeAnalysisResult(
        sessions=session_stats,
        best_session=best,
        worst_session=worst,
        edge_stable=edge_stable,
        edge_distribution=distribution,
        reasons=reasons,
    )


def _session_label(name: str) -> str:
    labels = {
        SessionName.ASIA: "Asia (00-08 UTC)",
        SessionName.EUROPE: "Europe (08-14 UTC)",
        SessionName.US: "US (14-21 UTC)",
        SessionName.CROSS: "Cross (21-00 UTC)",
    }
    return labels.get(name, name)

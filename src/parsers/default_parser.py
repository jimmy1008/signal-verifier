"""
預設信號解析器

職責：解析常見的 Telegram 信號格式
輸入：原始訊息文字
輸出：ParsedSignal 或 None

支援格式範例：
    BTCUSDT LONG
    Entry: 65000
    SL: 64000
    TP1: 66000
    TP2: 67000
    TP3: 68000
    TP4: 70000

也支援：
    🟢 BTC/USDT LONG
    ✅ TP1 reached
    ❌ Signal cancelled
"""

from __future__ import annotations

import re
import logging
from datetime import datetime
from typing import Optional

from src.models import ParsedSignal, SignalSide, UpdateType
from src.parsers.base import BaseParser

logger = logging.getLogger(__name__)

# ============================================================
# Regex 模板（可擴充）
# ============================================================

# 清理 symbol：移除 emoji、特殊字元
SYMBOL_CLEAN = re.compile(r"[^\w/]")

# 抓數字（含小數）
NUMBER = r"[\d]+(?:\.[\d]+)?"

# 進場信號模板
ENTRY_PATTERNS = [
    # 格式 1: SYMBOL SIDE\nEntry: xxx\nSL: xxx\nTP1: xxx...
    re.compile(
        rf"(?P<symbol>[A-Z0-9/]+)\s+(?P<side>LONG|SHORT|BUY|SELL)"
        rf".*?(?:entry|enter|price)[:\s]*(?P<entry>{NUMBER})"
        rf".*?(?:sl|stop\s*loss|stoploss)[:\s]*(?P<sl>{NUMBER})"
        rf"(?:.*?(?:tp1|take\s*profit\s*1|target\s*1)[:\s]*(?P<tp1>{NUMBER}))?"
        rf"(?:.*?(?:tp2|take\s*profit\s*2|target\s*2)[:\s]*(?P<tp2>{NUMBER}))?"
        rf"(?:.*?(?:tp3|take\s*profit\s*3|target\s*3)[:\s]*(?P<tp3>{NUMBER}))?"
        rf"(?:.*?(?:tp4|take\s*profit\s*4|target\s*4)[:\s]*(?P<tp4>{NUMBER}))?",
        re.IGNORECASE | re.DOTALL,
    ),
    # 格式 2: 🟢 SYMBOL\nDirection: LONG\nEntry: xxx
    re.compile(
        rf"(?P<symbol>[A-Z0-9/]+)"
        rf".*?(?:direction|dir|side)[:\s]*(?P<side>LONG|SHORT|BUY|SELL)"
        rf".*?(?:entry|enter|price)[:\s]*(?P<entry>{NUMBER})"
        rf".*?(?:sl|stop\s*loss)[:\s]*(?P<sl>{NUMBER})"
        rf"(?:.*?tp1[:\s]*(?P<tp1>{NUMBER}))?"
        rf"(?:.*?tp2[:\s]*(?P<tp2>{NUMBER}))?"
        rf"(?:.*?tp3[:\s]*(?P<tp3>{NUMBER}))?"
        rf"(?:.*?tp4[:\s]*(?P<tp4>{NUMBER}))?",
        re.IGNORECASE | re.DOTALL,
    ),
]

# 更新訊息模板
TP_HIT_PATTERN = re.compile(
    rf"(?:tp\s*(?P<tp_level>[1-4]))\s*(?:reached|hit|done|✅)",
    re.IGNORECASE,
)
CANCEL_PATTERN = re.compile(
    r"(?:cancel|cancelled|invalidated|❌)",
    re.IGNORECASE,
)
CLOSE_PATTERN = re.compile(
    r"(?:close\s*now|close\s*trade|exit\s*now)",
    re.IGNORECASE,
)
SL_MOVE_PATTERN = re.compile(
    rf"(?:sl|stop\s*loss)\s*(?:moved?|→|->)\s*(?:to\s*)?(?P<new_sl>{NUMBER}|entry|be|breakeven)",
    re.IGNORECASE,
)


class DefaultParser(BaseParser):
    @property
    def name(self) -> str:
        return "default"

    def parse(
        self, raw_text: str, timestamp: datetime, message_id: Optional[int] = None
    ) -> Optional[ParsedSignal]:
        # 先嘗試解析為 update 訊息
        update = self._try_parse_update(raw_text, timestamp, message_id)
        if update:
            return update

        # 嘗試解析為進場信號
        return self._try_parse_entry(raw_text, timestamp, message_id)

    def _try_parse_entry(
        self, text: str, timestamp: datetime, message_id: Optional[int]
    ) -> Optional[ParsedSignal]:
        for pattern in ENTRY_PATTERNS:
            m = pattern.search(text)
            if not m:
                continue

            symbol = SYMBOL_CLEAN.sub("", m.group("symbol")).upper()
            side_raw = m.group("side").upper()
            side = SignalSide.LONG if side_raw in ("LONG", "BUY") else SignalSide.SHORT

            try:
                entry = float(m.group("entry"))
                sl = float(m.group("sl"))
            except (TypeError, ValueError):
                continue

            # 基本合理性檢查
            if entry <= 0 or sl <= 0:
                continue
            if side == SignalSide.LONG and sl >= entry:
                continue
            if side == SignalSide.SHORT and sl <= entry:
                continue

            tp1 = _safe_float(m, "tp1")
            tp2 = _safe_float(m, "tp2")
            tp3 = _safe_float(m, "tp3")
            tp4 = _safe_float(m, "tp4")

            return ParsedSignal(
                symbol=symbol,
                side=side,
                entry=entry,
                sl=sl,
                tp1=tp1,
                tp2=tp2,
                tp3=tp3,
                tp4=tp4,
                signal_time=timestamp,
                signal_type="entry",
                raw_message_id=message_id,
            )

        return None

    def _try_parse_update(
        self, text: str, timestamp: datetime, message_id: Optional[int]
    ) -> Optional[ParsedSignal]:
        # TP hit
        m = TP_HIT_PATTERN.search(text)
        if m:
            return ParsedSignal(
                symbol="",  # 需要由上層關聯
                side=SignalSide.LONG,  # placeholder
                entry=0, sl=0,
                signal_time=timestamp,
                signal_type="update",
                raw_message_id=message_id,
                update_type=UpdateType.TP_HIT,
                update_value=f"tp{m.group('tp_level')}",
            )

        # Cancel
        if CANCEL_PATTERN.search(text):
            return ParsedSignal(
                symbol="", side=SignalSide.LONG, entry=0, sl=0,
                signal_time=timestamp,
                signal_type="cancel",
                raw_message_id=message_id,
                update_type=UpdateType.CANCEL,
            )

        # Close now
        if CLOSE_PATTERN.search(text):
            return ParsedSignal(
                symbol="", side=SignalSide.LONG, entry=0, sl=0,
                signal_time=timestamp,
                signal_type="close",
                raw_message_id=message_id,
                update_type=UpdateType.CLOSE_NOW,
            )

        # SL moved
        m = SL_MOVE_PATTERN.search(text)
        if m:
            return ParsedSignal(
                symbol="", side=SignalSide.LONG, entry=0, sl=0,
                signal_time=timestamp,
                signal_type="update",
                raw_message_id=message_id,
                update_type=UpdateType.SL_MOVED,
                update_value=m.group("new_sl"),
            )

        return None

    def can_parse(self, raw_text: str) -> bool:
        text_upper = raw_text.upper()
        keywords = ["ENTRY", "SL", "TP", "LONG", "SHORT", "BUY", "SELL", "STOP LOSS"]
        return any(kw in text_upper for kw in keywords)


def _safe_float(match: re.Match, group: str) -> Optional[float]:
    try:
        val = match.group(group)
        return float(val) if val else None
    except (IndexError, TypeError, ValueError):
        return None

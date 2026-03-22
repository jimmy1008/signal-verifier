"""
CRT SNIPER 專用解析器

格式範例：

進場信號：
    🔍#NAS100USD4H031709
    🆕 NAS100USD | 4H CRT SNIPE
    ━━━━━━━━━━━━━━━━━━
    💰 進場價：24643.4
    📈 方向：看多 (LONG)
    📊 盈虧評級： 🔴 低 (RR < 0.5)
    ⚠️ 區間評級： 🟢 高(實體50%以上)
    📍 相對位置： 溢價區 Premium
    🐢TBS : 1M、3M、5M
    ━━━━━━━━━━━━━━━━━━
    🎯 TP1: 24651.0 (RR: 0.14)
    🎯 TP2: 24689.4 (RR: 0.83)
    🎯 TP3: 24727.8 (RR: 1.53)
    🎯 TP4: 24804.6 (RR: 2.92)
    🚫 SL: 24588.1

TP 回報：
    🔍#NAS100USD4H031709
    📈 NAS100USD | 4H CRT TP
    ━━━━━━━━━━━━━━━━━━
    🎯 TP1: 24651.0 ✅

SL 回報：
    🔍#XAUUSD1H031715
    📉 XAUUSD | 1H CRT SL
    ━━━━━━━━━━━━━━━━━━
    🚫 SL：5028.200 ❌
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
# Regex
# ============================================================

# 信號 ID（用於關聯 update）
SIGNAL_KEY_RE = re.compile(r"#(\w+)")

# 進場信號
ENTRY_RE = re.compile(
    r"CRT SNIPE"
)

# 進場價（支援「進場價」和早期「收盤價」）
PRICE_RE = re.compile(r"(?:進場價|收盤價)[：:]\s*([\d.]+)")

# 方向（支援新格式 "方向：看多 (LONG)" 和早期 "🟢 看多" / "🔴 看空"）
DIRECTION_RE = re.compile(r"方向[：:]\s*看(多|空)\s*\((LONG|SHORT)\)")
DIRECTION_SIMPLE_RE = re.compile(r"(🟢\s*看多|🔴\s*看空)")

# Timeframe
TIMEFRAME_RE = re.compile(r"\|\s*(\d+H)\s*CRT", re.IGNORECASE)

# Symbol（從標題行抓，支援 BTCUSDT.P 和 NAS100USD）
SYMBOL_RE = re.compile(r"([A-Z0-9]+(?:\.[A-Z])?)\s*\|\s*\d+H?\s*CRT")
# 早期格式：【ETHUSDT.P】
SYMBOL_BRACKET_RE = re.compile(r"【([A-Z0-9.]+)】")

# TP
TP_RE = re.compile(r"TP(\d)[：:]\s*([\d.]+)")

# SL
SL_RE = re.compile(r"SL[：:]\s*([\d.]+)")

# TP 回報
TP_UPDATE_RE = re.compile(r"CRT TP")

# SL 回報
SL_UPDATE_RE = re.compile(r"CRT SL")

# 盈虧評級
RR_RATING_RE = re.compile(r"盈虧評級[：:]\s*(🟢|🟡|🔴|🟠)\s*(.+)")

# 區間評級
RANGE_RATING_RE = re.compile(r"區間評級[：:]\s*(🟢|🟡|🔴)\s*(.+)")

# 相對位置
POSITION_RE = re.compile(r"相對位置[：:]\s*(.+)")


class CrtSniperParser(BaseParser):
    @property
    def name(self) -> str:
        return "crt_sniper"

    def parse(
        self, raw_text: str, timestamp: datetime, message_id: Optional[int] = None
    ) -> Optional[ParsedSignal]:
        # 抓信號 key
        key_match = SIGNAL_KEY_RE.search(raw_text)
        signal_key = key_match.group(1) if key_match else None

        # 判斷訊息類型
        if ENTRY_RE.search(raw_text):
            return self._parse_entry(raw_text, timestamp, message_id, signal_key)
        elif TP_UPDATE_RE.search(raw_text):
            return self._parse_tp_update(raw_text, timestamp, message_id, signal_key)
        elif SL_UPDATE_RE.search(raw_text):
            return self._parse_sl_update(raw_text, timestamp, message_id, signal_key)

        return None

    def _parse_entry(
        self, text: str, timestamp: datetime, message_id: Optional[int], signal_key: Optional[str]
    ) -> Optional[ParsedSignal]:
        # Symbol（嘗試兩種格式）
        sym_match = SYMBOL_RE.search(text)
        if not sym_match:
            sym_match = SYMBOL_BRACKET_RE.search(text)
        if not sym_match:
            return None
        symbol = sym_match.group(1)

        # 進場價（支援「進場價」和「收盤價」）
        price_match = PRICE_RE.search(text)
        if not price_match:
            return None
        entry = float(price_match.group(1))

        # 方向（嘗試新格式和簡易格式）
        dir_match = DIRECTION_RE.search(text)
        if not dir_match:
            simple_match = DIRECTION_SIMPLE_RE.search(text)
            if not simple_match:
                return None
            direction_text = simple_match.group(1)
            side = SignalSide.LONG if "多" in direction_text else SignalSide.SHORT
        else:
            side = SignalSide.LONG if dir_match.group(2) == "LONG" else SignalSide.SHORT

        # SL
        sl_match = SL_RE.search(text)
        if not sl_match:
            return None
        sl = float(sl_match.group(1))

        # TP1~4
        tps = {}
        for tp_match in TP_RE.finditer(text):
            level = int(tp_match.group(1))
            tps[f"tp{level}"] = float(tp_match.group(2))

        # Timeframe
        tf_match = TIMEFRAME_RE.search(text)
        timeframe = tf_match.group(1).lower() if tf_match else None  # "4h", "1h"

        # 額外 metadata
        notes_parts = []

        rr_match = RR_RATING_RE.search(text)
        if rr_match:
            notes_parts.append(f"rr_rating={rr_match.group(2).strip()}")

        range_match = RANGE_RATING_RE.search(text)
        if range_match:
            notes_parts.append(f"range_rating={range_match.group(2).strip()}")

        pos_match = POSITION_RE.search(text)
        if pos_match:
            notes_parts.append(f"position={pos_match.group(1).strip()}")

        return ParsedSignal(
            symbol=symbol,
            side=side,
            entry=entry,
            sl=sl,
            tp1=tps.get("tp1"),
            tp2=tps.get("tp2"),
            tp3=tps.get("tp3"),
            tp4=tps.get("tp4"),
            timeframe=timeframe,
            signal_time=timestamp,
            signal_type="entry",
            raw_message_id=message_id,
            related_signal_key=signal_key,
        )

    def _parse_tp_update(
        self, text: str, timestamp: datetime, message_id: Optional[int], signal_key: Optional[str]
    ) -> Optional[ParsedSignal]:
        # 找最高的 TP level（有 ✅ 的）
        hit_levels = []
        for tp_match in TP_RE.finditer(text):
            level = int(tp_match.group(1))
            hit_levels.append(level)

        if not hit_levels:
            return None

        max_level = max(hit_levels)

        return ParsedSignal(
            symbol="",
            side=SignalSide.LONG,  # placeholder
            entry=0, sl=0,
            signal_time=timestamp,
            signal_type="update",
            raw_message_id=message_id,
            related_signal_key=signal_key,
            update_type=UpdateType.TP_HIT,
            update_value=f"tp{max_level}",
        )

    def _parse_sl_update(
        self, text: str, timestamp: datetime, message_id: Optional[int], signal_key: Optional[str]
    ) -> Optional[ParsedSignal]:
        return ParsedSignal(
            symbol="",
            side=SignalSide.LONG,  # placeholder
            entry=0, sl=0,
            signal_time=timestamp,
            signal_type="update",
            raw_message_id=message_id,
            related_signal_key=signal_key,
            update_type=UpdateType.CLOSE_NOW,
            update_value="sl_hit",
        )

    def can_parse(self, raw_text: str) -> bool:
        return "CRT" in raw_text and SIGNAL_KEY_RE.search(raw_text) is not None

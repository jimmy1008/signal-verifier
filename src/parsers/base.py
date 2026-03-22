"""
Parser 介面定義

職責：定義所有 parser 的共同介面
所有頻道 parser 都要繼承 BaseParser 並實作 parse()
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from src.models import ParsedSignal


class BaseParser(ABC):
    """訊號解析器基底類別"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Parser 名稱，用於設定檔對應"""
        ...

    @abstractmethod
    def parse(self, raw_text: str, timestamp: datetime, message_id: Optional[int] = None) -> Optional[ParsedSignal]:
        """
        嘗試解析一則訊息。

        Args:
            raw_text: 原始訊息文字
            timestamp: 訊息時間
            message_id: raw_messages 表的 id

        Returns:
            ParsedSignal 如果成功解析，None 如果不是信號訊息
        """
        ...

    def can_parse(self, raw_text: str) -> bool:
        """
        快速判斷這則訊息是否可能是信號。
        預設實作：嘗試 parse，看有沒有結果。
        子類可覆寫以提升效率。
        """
        try:
            return self.parse(raw_text, datetime.now()) is not None
        except Exception:
            return False

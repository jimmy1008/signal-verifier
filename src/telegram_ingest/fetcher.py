"""
Telegram 訊息抓取層

職責：
- 登入 Telegram 帳號
- 讀取指定群組歷史訊息
- 監聽新訊息（即時模式）
- 將原始訊息存入 raw_messages 表

輸入：Telegram API credentials + 頻道設定
輸出：RawMessageORM 寫入 DB
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from telethon import TelegramClient, events
from telethon.tl.types import Message

from src.database import get_session
from src.models import RawMessageORM

logger = logging.getLogger(__name__)


class TelegramFetcher:
    def __init__(self, api_id: int, api_hash: str, phone: str, session_name: str = "signal_verifier"):
        self.client = TelegramClient(session_name, api_id, api_hash)
        self.phone = phone

    async def connect(self) -> None:
        """連線並登入"""
        await self.client.start(phone=self.phone)
        logger.info("Telegram 已連線")

    async def disconnect(self) -> None:
        await self.client.disconnect()

    # ----------------------------------------------------------
    # 歷史訊息回補
    # ----------------------------------------------------------
    async def fetch_history(
        self,
        chat_id: int | str,
        source_name: str,
        limit: Optional[int] = None,
        offset_date: Optional[datetime] = None,
    ) -> int:
        """
        抓取指定頻道的歷史訊息並存入 DB。

        Args:
            chat_id: 頻道 ID 或 username
            source_name: 來源名稱（用於標記）
            limit: 最多抓幾則（None = 全部）
            offset_date: 從哪個時間點開始往回抓

        Returns:
            新儲存的訊息數量
        """
        session = get_session()
        saved = 0

        try:
            async for msg in self.client.iter_messages(
                chat_id, limit=limit, offset_date=offset_date
            ):
                if not isinstance(msg, Message) or not msg.text:
                    continue

                # 檢查是否已存在
                exists = (
                    session.query(RawMessageORM)
                    .filter_by(chat_id=str(chat_id), message_id=str(msg.id))
                    .first()
                )
                if exists:
                    continue

                raw = RawMessageORM(
                    source=source_name,
                    chat_id=str(chat_id),
                    message_id=str(msg.id),
                    timestamp=msg.date.replace(tzinfo=None) if msg.date else datetime.now(timezone.utc),
                    raw_text=msg.text,
                    reply_to_message_id=str(msg.reply_to_msg_id) if msg.reply_to_msg_id else None,
                    is_edited=msg.edit_date is not None,
                    is_forwarded=msg.forward is not None,
                    parsed_status="pending",
                )
                session.add(raw)
                saved += 1

                if saved % 100 == 0:
                    session.commit()
                    logger.info(f"已儲存 {saved} 則訊息...")

            session.commit()
            logger.info(f"歷史回補完成，共儲存 {saved} 則新訊息")
        finally:
            session.close()

        return saved

    # ----------------------------------------------------------
    # 即時監聽
    # ----------------------------------------------------------
    async def listen(self, chat_ids: list[int | str], source_name: str) -> None:
        """
        即時監聽新訊息。

        持續運行，收到新訊息就存入 DB。
        """
        @self.client.on(events.NewMessage(chats=chat_ids))
        async def handler(event: events.NewMessage.Event):
            msg = event.message
            if not msg.text:
                return

            session = get_session()
            try:
                raw = RawMessageORM(
                    source=source_name,
                    chat_id=str(msg.chat_id),
                    message_id=str(msg.id),
                    timestamp=msg.date.replace(tzinfo=None) if msg.date else datetime.now(timezone.utc),
                    raw_text=msg.text,
                    reply_to_message_id=str(msg.reply_to_msg_id) if msg.reply_to_msg_id else None,
                    is_edited=False,
                    is_forwarded=msg.forward is not None,
                    parsed_status="pending",
                )
                session.add(raw)
                session.commit()
                logger.info(f"新訊息已儲存: {msg.id}")
            finally:
                session.close()

        logger.info(f"開始監聽 {len(chat_ids)} 個頻道...")
        await self.client.run_until_disconnected()

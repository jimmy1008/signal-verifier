"""
腳本：抓取 Telegram 歷史訊息並解析信號

使用方式：
    python scripts/fetch_history.py

流程：
    1. 連線 Telegram
    2. 抓取指定頻道歷史訊息 → raw_messages
    3. 解析訊息 → signals
"""

import asyncio
import logging
import sys
from pathlib import Path

# 讓 import 從專案根目錄開始
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.database import init_db, get_session
from src.telegram_ingest.fetcher import TelegramFetcher
from src.parsers.registry import get_parser
from src.parsers.signal_processor import process_pending_messages

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def main():
    config = load_config()

    # 初始化 DB
    init_db(config["database"]["url"])

    # 連線 Telegram
    tg_cfg = config["telegram"]
    fetcher = TelegramFetcher(
        api_id=tg_cfg["api_id"],
        api_hash=tg_cfg["api_hash"],
        phone=tg_cfg["phone"],
        session_name=tg_cfg.get("session_name", "signal_verifier"),
    )
    await fetcher.connect()

    try:
        # 抓取每個頻道的歷史訊息
        for channel in tg_cfg["channels"]:
            chat_id = channel["chat_id"]
            name = channel.get("name", str(chat_id))
            parser_name = channel.get("parser", "default")

            logger.info(f"=== 抓取頻道: {name} ===")
            saved = await fetcher.fetch_history(chat_id, source_name=name)
            logger.info(f"新儲存 {saved} 則訊息")

            # 解析
            parser = get_parser(parser_name)
            session = get_session()
            try:
                parsed = process_pending_messages(session, parser, source=name)
                logger.info(f"解析出 {parsed} 筆信號")
            finally:
                session.close()

    finally:
        await fetcher.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

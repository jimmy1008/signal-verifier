"""
腳本：即時監聽 Telegram 新訊息

使用方式：
    python scripts/listen.py

持續運行，收到新訊息自動儲存並解析。
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.database import init_db
from src.telegram_ingest.fetcher import TelegramFetcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def main():
    config = load_config()
    init_db(config["database"]["url"])

    tg_cfg = config["telegram"]
    fetcher = TelegramFetcher(
        api_id=tg_cfg["api_id"],
        api_hash=tg_cfg["api_hash"],
        phone=tg_cfg["phone"],
        session_name=tg_cfg.get("session_name", "signal_verifier"),
    )
    await fetcher.connect()

    chat_ids = [ch["chat_id"] for ch in tg_cfg["channels"]]
    source_name = tg_cfg["channels"][0].get("name", "default")

    logger.info("開始即時監聽...")
    await fetcher.listen(chat_ids, source_name)


if __name__ == "__main__":
    asyncio.run(main())

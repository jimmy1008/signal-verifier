"""找出你加入的所有群組/頻道的 chat_id"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from telethon import TelegramClient


async def main():
    config = load_config()
    tg = config["telegram"]

    client = TelegramClient(tg["session_name"], tg["api_id"], tg["api_hash"])
    await client.start(phone=tg["phone"])

    print("\n你加入的群組/頻道：\n")
    print(f"{'chat_id':<20} {'類型':<10} {'名稱'}")
    print("-" * 60)

    async for dialog in client.iter_dialogs():
        dtype = "user"
        if dialog.is_group:
            dtype = "group"
        elif dialog.is_channel:
            dtype = "channel"

        if dtype in ("group", "channel"):
            print(f"{dialog.id:<20} {dtype:<10} {dialog.name}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

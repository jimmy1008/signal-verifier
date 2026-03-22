"""
漏跳通知 Bot

功能：
- 定時檢查信號有沒有漏發 TP/SL 回報
- 超過設定時間沒回報 → 發 Telegram 通知
- 可發到你自己或對方的群

使用：
    python scripts/notify_missed.py

設定 config.yaml:
    notify:
      bot_token: "YOUR_BOT_TOKEN"     # 從 @BotFather 取得
      chat_id: -1001234567890          # 要發通知的群/個人 chat_id
      check_interval: 300              # 每 5 分鐘檢查一次
      timeout_hours: 6                 # 超過 6 小時沒回報視為漏跳
"""

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from src.config import load_config
from src.database import init_db, get_session
from src.models import SignalORM, SignalUpdateORM

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def send_telegram(bot_token: str, chat_id: int, text: str):
    """發 Telegram 訊息"""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        })


async def check_missed(bot_token: str, chat_id: int, timeout_hours: int = 6):
    """檢查漏跳信號並發通知"""
    session = get_session()
    cutoff = datetime.utcnow() - timedelta(hours=timeout_hours)

    # 找超時沒回報的信號
    signals = (
        session.query(SignalORM)
        .filter(
            SignalORM.signal_time > cutoff,
            SignalORM.signal_time < datetime.utcnow() - timedelta(hours=1),  # 至少 1 小時前的
        )
        .order_by(SignalORM.signal_time.desc())
        .all()
    )

    missed = []
    for sig in signals:
        updates = session.query(SignalUpdateORM).filter_by(signal_id=sig.id).all()
        if not updates:
            # 超過 1 小時沒任何回報
            missed.append(sig)

    session.close()

    if not missed:
        return 0

    # 組通知訊息
    lines = [f"<b>漏跳偵測 - {len(missed)} 筆信號無回報</b>\n"]
    for sig in missed[:10]:  # 最多顯示 10 筆
        age = datetime.utcnow() - sig.signal_time
        hours = age.total_seconds() / 3600
        lines.append(
            f"  {sig.symbol} {sig.side.value.upper()} "
            f"@ {sig.entry} | {hours:.1f}h 前 | {sig.signal_key or ''}"
        )

    if len(missed) > 10:
        lines.append(f"\n  ...還有 {len(missed) - 10} 筆")

    text = "\n".join(lines)
    await send_telegram(bot_token, chat_id, text)
    logger.info(f"已發送漏跳通知: {len(missed)} 筆")
    return len(missed)


async def main():
    config = load_config()
    init_db(config["database"]["url"])

    notify_cfg = config.get("notify", {})
    bot_token = notify_cfg.get("bot_token", "")
    chat_id = notify_cfg.get("chat_id", 0)
    interval = notify_cfg.get("check_interval", 300)
    timeout = notify_cfg.get("timeout_hours", 6)

    if not bot_token or bot_token == "YOUR_BOT_TOKEN":
        logger.error("請在 config.yaml 設定 notify.bot_token")
        logger.info("從 @BotFather 建立 bot 取得 token")
        return

    logger.info(f"漏跳通知已啟動（每 {interval} 秒檢查，超過 {timeout} 小時無回報通知）")

    while True:
        try:
            count = await check_missed(bot_token, chat_id, timeout)
            if count:
                logger.info(f"偵測到 {count} 筆漏跳")
        except Exception as e:
            logger.error(f"檢查異常: {e}")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())

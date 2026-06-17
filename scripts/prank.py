"""24:00 發送 10 句「不要刪掉我」，每 2 秒一句，1 分鐘後全部收回"""
import httpx
import time
from datetime import datetime

BOT_TOKEN = "YOUR_BOT_TOKEN"
CHAT_ID = 0  # 對方的 chat_id

# Wait until 24:00
now = datetime.now()
target = now.replace(hour=23, minute=59, second=59, microsecond=0)
if now >= target:
    target = target.replace(day=target.day + 1)

wait = (target - now).total_seconds()
if wait > 0:
    print(f"Waiting {wait:.0f}s until {target}...")
    time.sleep(wait)

# Send 10 messages, 2 seconds apart
msg_ids = []
for i in range(10):
    resp = httpx.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": "不要刪掉我"}
    )
    mid = resp.json().get("result", {}).get("message_id")
    msg_ids.append(mid)
    print(f"Sent #{i+1}: msg_id={mid}")
    if i < 9:
        time.sleep(2)

print(f"All sent. Waiting 60s to delete...")
time.sleep(60)

# Delete all
for mid in msg_ids:
    if mid:
        resp = httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
            json={"chat_id": CHAT_ID, "message_id": mid}
        )
        print(f"Deleted {mid}: {resp.json().get('ok')}")

print("Done.")

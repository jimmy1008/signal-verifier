"""Bot 健康檢查 — 每 5 分鐘檢查 bot 是否在運行，停止則立即通知"""
import subprocess
import sys
import os
import json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["PYTHONIOENCODING"] = "utf-8"

from src.config import load_config

STATE_FILE = Path(__file__).resolve().parent.parent / "db" / "health_state.json"
LOG_PATH = Path(__file__).resolve().parent.parent / "auto_trade.log"


def notify(text):
    import httpx
    config = load_config()
    cfg = config.get("notify", {})
    token = cfg.get("bot_token", "")
    chats = cfg.get("chat_ids", [])
    for cid in chats:
        try:
            httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": cid, "text": text, "parse_mode": "HTML"}, timeout=10)
        except Exception:
            pass


def is_running():
    try:
        r = subprocess.run(
            ["wmic", "process", "where",
             "name like '%python%' and commandline like '%auto_trade%'",
             "get", "processid"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW)
        return any(line.strip().isdigit() for line in r.stdout.split("\n"))
    except Exception:
        return False


def get_stop_reason():
    if not LOG_PATH.exists():
        return "log 不存在"
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").split("\n")
        for line in reversed(lines[-50:]):
            if "熔斷" in line and "全平" in line:
                return "熔斷觸發"
            if "[ERROR]" in line and "SystemExit" not in line and "telethon" not in line.lower():
                return line.strip()[:100]
    except Exception:
        pass
    return "原因不明"


def main():
    prev = {"state": "unknown"}
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text())
        except Exception:
            pass

    running = is_running()
    current = "running" if running else "stopped"
    last = prev.get("state", "unknown")

    if last == "running" and current == "stopped":
        notify(f"<b>Bot 已停止運行</b>\n\n原因: {get_stop_reason()}\n\n請盡快重啟！")
    elif last == "stopped" and current == "running":
        notify("Bot 已恢復運行")

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"state": current, "last_check": datetime.now().isoformat()}))


if __name__ == "__main__":
    main()

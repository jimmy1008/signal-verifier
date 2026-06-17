"""每日交易報告 — 每天 08:00 (台灣時間) 生成昨日報告並發送 Telegram"""
import sys
import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["PYTHONIOENCODING"] = "utf-8"

from src.config import load_config

DB_DIR = Path(__file__).resolve().parent.parent / "db"
LOG_PATH = Path(__file__).resolve().parent.parent / "auto_trade.log"


def generate_report():
    config = load_config()
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_short = (datetime.now() - timedelta(days=1)).strftime("%m/%d")

    # ── 1. 交易數據 ──
    all_history = []
    for label in ["h1", "h4"]:
        hp = DB_DIR / f"trade_history_{label}.json"
        if hp.exists():
            try:
                with open(hp, "r", encoding="utf-8") as f:
                    for h in json.load(f):
                        h["_label"] = label.upper()
                        all_history.append(h)
            except Exception:
                pass

    yesterday_trades = []
    for h in all_history:
        closed_at = h.get("closed_at", "") or h.get("opened_at", "")
        if closed_at and closed_at[:10] == yesterday:
            yesterday_trades.append(h)

    tp_count = sum(1 for t in yesterday_trades if "TP" in t.get("close_reason", "") or "tp" in t.get("close_reason", ""))
    sl_count = len(yesterday_trades) - tp_count
    total_pnl = sum(t.get("realized_pnl", 0) for t in yesterday_trades)
    total_fee = sum(t.get("fee", 0) for t in yesterday_trades)

    best = max(yesterday_trades, key=lambda t: t.get("realized_pnl", 0)) if yesterday_trades else None
    worst = min(yesterday_trades, key=lambda t: t.get("realized_pnl", 0)) if yesterday_trades else None

    # 累計
    total_all_pnl = sum(t.get("realized_pnl", 0) for t in all_history)
    total_all_trades = len(all_history)
    total_all_tp = sum(1 for t in all_history if "TP" in t.get("close_reason", "") or "tp" in t.get("close_reason", ""))
    total_all_sl = total_all_trades - total_all_tp
    total_wr = total_all_tp / total_all_trades * 100 if total_all_trades else 0

    # 持倉
    open_count = 0
    for label in ["h1", "h4"]:
        sp = DB_DIR / f"executor_state_{label}.json"
        if sp.exists():
            try:
                with open(sp, "r", encoding="utf-8") as f:
                    open_count += sum(1 for s in json.load(f).values() if not s.get("closed"))
            except Exception:
                pass

    # ── 2. 系統檢測 ──

    # Bot 進程
    bot_ok = False
    bot_detail = "未運行"
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             "name like '%python%' and commandline like '%auto_trade%'",
             "get", "processid"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().split("\n"):
            if line.strip().isdigit():
                bot_ok = True
                bot_detail = f"PID {line.strip()}"
    except Exception:
        pass

    # BingX 連線 + 餘額
    bingx_detail = ""
    try:
        import ccxt
        bingx_cfg = config.get("bingx", {})
        parts = []
        for label, key, secret in [
            ("H1", bingx_cfg.get("api_key", ""), bingx_cfg.get("api_secret", "")),
            ("H4", bingx_cfg.get("sub_api_key", ""), bingx_cfg.get("sub_api_secret", "")),
        ]:
            if not key:
                continue
            ex = ccxt.bingx({"apiKey": key, "secret": secret, "options": {"defaultType": "swap"}})
            bal = ex.fetch_balance()
            usdt = float(bal.get("total", {}).get("USDT", 0))
            parts.append(f"{label}: ${usdt:.2f}")
        bingx_detail = " | ".join(parts)
        bingx_ok = True
    except Exception as e:
        bingx_detail = str(e)[:60]
        bingx_ok = False

    # 持倉一致性
    bingx_positions = 0
    try:
        bingx_cfg = config.get("bingx", {})
        for key, secret in [
            (bingx_cfg.get("api_key", ""), bingx_cfg.get("api_secret", "")),
            (bingx_cfg.get("sub_api_key", ""), bingx_cfg.get("sub_api_secret", "")),
        ]:
            if not key:
                continue
            ex = ccxt.bingx({"apiKey": key, "secret": secret, "options": {"defaultType": "swap"}})
            ex.load_markets()
            positions = ex.fetch_positions()
            bingx_positions += sum(1 for p in positions if abs(float(p.get("contracts", 0))) > 0)
    except Exception:
        pass
    pos_match = open_count == bingx_positions

    # Log 活躍度
    log_ok = False
    log_detail = "無 log"
    if LOG_PATH.exists():
        age = (datetime.now() - datetime.fromtimestamp(LOG_PATH.stat().st_mtime)).total_seconds()
        log_ok = age < 300
        log_detail = f"{int(age)}秒前" if log_ok else f"{int(age)}秒前 (超時)"

    # 昨日嚴重 log
    severe_logs = []
    error_count = warning_count = 0
    if LOG_PATH.exists():
        try:
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.startswith(yesterday):
                        continue
                    if "[ERROR]" in line:
                        error_count += 1
                        if "telethon" not in line.lower():
                            severe_logs.append(line.strip()[:100])
                    elif "[WARNING]" in line:
                        warning_count += 1
                        if "熔斷" in line or "失敗" in line:
                            severe_logs.append(line.strip()[:100])
        except Exception:
            pass
    # HTML 轉義 log 內容
    import html as _html
    severe_logs = [_html.escape(log) for log in severe_logs]
    unique_severe = list(dict.fromkeys(severe_logs))[:5]

    # 昨日開倉/平倉/加倉統計
    opens = closes = staged = 0
    if LOG_PATH.exists():
        try:
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.startswith(yesterday):
                        continue
                    if "已開倉:" in line:
                        opens += 1
                    if "部分平倉" in line and "成功" in line:
                        closes += 1
                    if "加倉成功" in line:
                        staged += 1
        except Exception:
            pass

    # ── 3. 組裝報告 ──
    report = f"<b>Daily Report {yesterday_short}</b>\n"

    # 昨日交易
    report += f"\n<b>昨日交易</b>\n"
    report += f"  筆數: {len(yesterday_trades)} (TP={tp_count} SL={sl_count})\n"
    report += f"  淨盈虧: <code>${total_pnl:+.2f}</code>\n"
    report += f"  手續費: <code>${total_fee:.2f}</code>\n"
    report += f"  開倉: {opens} | 平倉: {closes} | 加倉: {staged}"

    if best and best.get("realized_pnl", 0) > 0:
        b_sym = best.get("symbol", "").replace("USDT.P", "")
        report += f"\n\n<b>最大盈利</b>\n"
        report += f"  {b_sym} {best.get('side', '')} ${best.get('realized_pnl', 0):+.2f}"

    if worst and worst.get("realized_pnl", 0) < 0:
        w_sym = worst.get("symbol", "").replace("USDT.P", "")
        report += f"\n\n<b>最大虧損</b>\n"
        report += f"  {w_sym} {worst.get('side', '')} ${worst.get('realized_pnl', 0):+.2f}"

    # 累計
    report += f"\n\n<b>累計</b>\n"
    report += f"  {total_all_trades} 筆 (TP={total_all_tp} SL={total_all_sl}) WR={total_wr:.1f}%\n"
    report += f"  PnL: <code>${total_all_pnl:+.2f}</code>\n"
    report += f"  $300 -> <code>${300 + total_all_pnl:.2f}</code> ({total_all_pnl / 3:+.1f}%)"

    # 系統檢測
    report += f"\n\n<b>系統檢測</b>\n"
    report += f"  {'OK' if bot_ok else 'FAIL'} Bot: {bot_detail}\n"
    report += f"  {'OK' if bingx_ok else 'FAIL'} BingX: {bingx_detail}\n"
    report += f"  {'OK' if pos_match else 'FAIL'} 持倉: State={open_count} BingX={bingx_positions}\n"
    report += f"  {'OK' if log_ok else 'FAIL'} Log: {log_detail}\n"
    report += f"  昨日 ERROR: {error_count} | WARNING: {warning_count}"

    # 嚴重日誌
    if unique_severe:
        report += f"\n\n<b>嚴重日誌</b>"
        for log in unique_severe:
            report += f"\n  <code>{log}</code>"

    if not yesterday_trades:
        report += "\n\n(昨日無平倉交易)"

    return report


def send_report(report_text):
    import httpx
    config = load_config()
    notify = config.get("notify", {})
    bot_token = notify.get("bot_token", "")
    chat_ids = notify.get("chat_ids", [])

    if not bot_token or not chat_ids:
        return

    for chat_id in chat_ids:
        try:
            httpx.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": report_text, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception:
            pass

    report_dir = Path(__file__).resolve().parent.parent / "reports"
    report_dir.mkdir(exist_ok=True)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    (report_dir / f"daily_{yesterday}.txt").write_text(report_text, encoding="utf-8")


if __name__ == "__main__":
    report = generate_report()
    print(report)
    print()
    send_report(report)

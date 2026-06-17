# 交接文件

## 專案一句話

監聽 CRT Sniper Telegram 頻道，自動解析信號並在 BingX 永續合約下單，管理 TP/SL 出場，雙帳戶（H1/H4）分開執行。

---

## 重要檔案位置

| 檔案 | 說明 |
|------|------|
| `scripts/auto_trade.py` | 主程式，日常啟動這個 |
| `config/config.yaml` | 所有 API key、頻道 ID、風險設定（不在 git） |
| `db/signals.db` | SQLite，所有信號 + 成交紀錄（不在 git） |
| `db/executor_state_h1.json` | H1 帳戶持倉狀態，程式重啟時恢復用 |
| `db/executor_state_h4.json` | H4 帳戶持倉狀態 |
| `db/trade_history_h1.json` | H1 已平倉歷史 |
| `db/trade_history_h4.json` | H4 已平倉歷史 |
| `signal_verifier.session` | Telegram session，**不能刪**，刪了要重新登入驗證碼 |
| `STRATEGY_CHANGELOG.md` | 每次策略調整的原因與數據 |

---

## 啟動與停止

```bash
# 啟動實盤
python scripts/auto_trade.py

# 模擬模式（不下單，只 log）
python scripts/auto_trade.py --dry-run

# 帶參數
python scripts/auto_trade.py --risk 0.01 --max-positions 5

# 儀表板（另開終端）
streamlit run src/dashboard/app.py
```

停止直接 `Ctrl+C`，程式會優雅關機（停止監聽 → 停持倉監控 → 存檔狀態 → 斷線）。

---

## 目前策略（2026-04-05 起）

| 項目 | 設定 |
|------|------|
| 風險 | 1% / 筆（H1 和 H4 相同） |
| 進場 | 100% 全額，不分批 |
| TP2 觸及 | SL 移到 Entry（保本） |
| TP3 觸及 | 出場 50% |
| TP4 觸及 | 出場剩餘 50%（全平） |
| SL | 固定原始位置（不移動止盈） |
| 信號過濾 | CRT sweep 過濾器（見下） |
| 熔斷 | H1+H4 總淨值 < $200 → 全平停機 |

### CRT Sweep 過濾器

進場前確認信號K線有真正做到 sweep（LONG = 掃了前幾根的低點，SHORT = 掃了前幾根的高點）。不符合條件的信號直接跳過。

分析 3723 筆歷史信號：符合 sweep 的期望值 -0.005R（接近打平），不符合的 -0.088R（明確虧損）。

---

## 實盤成績摘要

| 階段 | 日期 | 結果 |
|------|------|------|
| v1 上線 | 2026-03-20 | $300 本金 |
| v2 調整前 | 2026-03-25 | H1 -$9.65，H4 -$0.72 |
| 累積至今 | 2026-04-xx | ~$205，虧 -$95（-32%） |
| 高 RR 信號 | — | +$24 |
| 低 RR 信號 | — | -$119 |

> 核心問題：低 RR（TP1 RR < 1）信號是虧損主因。Sweep filter 加入後尚待觀察。

---

## 常見狀況處理

### 程式掉線重啟
直接重啟即可。`executor_state_*.json` 會自動恢復持倉狀態，繼續監控未平倉。

### Telegram session 過期
刪除 `signal_verifier.session` 後重啟，輸入手機驗證碼重新登入。

### BingX API 報錯
1. 先確認 BingX 後台 API key 還有效
2. 確認 `is_demo: false`（實盤）或 `true`（測試網）設定正確
3. 查 `auto_trade.log`

### 熔斷觸發
程式自動全平並停機，TG 會推送通知。需人工確認帳戶狀態後再手動重啟。

### 漏單懷疑
查 `db/signals.db` → `raw_messages` 表，確認訊息有沒有進來。
或查 `auto_trade.log` 搜尋該信號 symbol。

---

## 架構說明

```
auto_trade.py（主程式）
│
├── TelegramClient（telethon）
│   ├── on(NewMessage) → 即時監聽
│   └── poll_missed()  → 每 30s 補抓防漏
│
├── CrtSniperParser → 解析信號格式
│
├── TradeExecutor H1（BingX 主帳戶，1H 信號）
│   └── _monitor_loop() → 每 10s 查持倉
│
├── TradeExecutor H4（BingX 子帳戶，4H 信號）
│   └── _monitor_loop() → 每 30s 查持倉
│
├── equity_kill_switch() → 每 60s 查總淨值熔斷
├── notify_missed_loop() → 每 5min 查漏跳信號
└── status_report()      → 台灣 08/12/16/20 點播報
```

---

## 已知限制與待改進

1. **Sweep filter 用推斷邏輯**：BingX ticker 無法直接拿歷史 K 線，目前用信號資訊推斷，精確度有限。
2. **低 RR 信號無 edge**：TP1 RR < 1 的信號歷史期望值差，可考慮加 RR 門檻過濾。
3. **OANDA 未接**：config 有 OANDA 設定但 CFD 信號目前觀察模式，不實際下單。
4. **子帳戶 rate limit**：H4 監控間隔設 30s（比 H1 的 10s 寬鬆）緩解，但高頻交易期間仍可能 429。

---

## 需要的 API 與帳號

| 服務 | 用途 | 哪裡申請 |
|------|------|----------|
| Telegram API | 監聽頻道 | my.telegram.org |
| CRT Sniper 頻道 | 信號來源 | 需付費訂閱 |
| BingX 帳號 | 下單 | bingx.com |
| BingX API Key | 程式控制帳戶 | BingX 後台 → API 管理 |
| Telegram Bot | 推送通知 | @BotFather |

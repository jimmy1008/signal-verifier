# Signal Verifier

Telegram 信號頻道全自動跟單系統

從頻道接收 CRT Sniper 信號，自動解析、下單、管理持倉、監控出場，並提供回測引擎與儀表板驗證策略有效性。

## 系統架構

```
Telegram 頻道
    │
    ▼
TelegramFetcher ──▶ DB (raw_messages)
    │
    ▼
Parser (CRT Sniper)
    │
    ▼
TradeExecutor
    ├── BingX H1 主帳戶（1H 信號）
    └── BingX H4 子帳戶（4H 信號）
    │
    ▼
持倉監控（每 10s）
    ├── TP2 觸及 → SL 移到 Entry（保本）
    ├── TP3 觸及 → 出場 50%
    └── TP4 觸及 → 出場 50%（全平）
```

## 核心功能

| 功能 | 說明 |
|------|------|
| 即時跟單 | 監聽 TG 頻道，收到信號秒下單 |
| 雙帳戶 | H1 主帳戶 + H4 子帳戶分開管理 |
| 分批止盈 | TP3=50% / TP4=50%，TP2 保本 |
| 補抓機制 | 每 30 秒回查近 5 分鐘防漏單 |
| 熔斷機制 | 總淨值 < $200 → 自動全平停機 |
| 漏跳通知 | 信號超過 6 小時無回報 → TG 推送 |
| 狀態報告 | 台灣時間 08/12/16/20 點定時播報 |
| 模擬模式 | `--dry-run` 或 `paper_mode: true` |
| 回測引擎 | 歷史 K 線回測，支援多種出場規則 |
| 儀表板 | Streamlit Dashboard，含 Equity Curve |

## Setup Guide

### Step 1 — 安裝環境

Python 3.11+ 建議。

```bash
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
```

---

### Step 2 — Telegram API（必須）

前往 [https://my.telegram.org](https://my.telegram.org) 登入後建立應用程式，取得：

- `api_id`
- `api_hash`

填入 `config.yaml` 的 `telegram` 區塊，並填上自己的手機號碼。

> 第一次執行時會要求輸入 Telegram 驗證碼，之後產生 `.session` 檔案，後續自動登入。

---

### Step 3 — 加入 CRT Sniper 頻道（必須）

程式只能監聽自己帳號已加入的頻道。加入後執行以下指令查詢 chat_id：

```bash
python scripts/find_chats.py
```

將取得的 chat_id 填入 `config.yaml` 的 `channels` 清單。

---

### Step 4 — BingX API（實盤必須，模擬可跳過）

1. 開 BingX 帳號並開通永續合約交易權限
2. 後台建立 API Key，勾選「交易」權限
3. 填入 `api_key` + `api_secret`
4. 選填：建子帳號並填入 `sub_api_key` + `sub_api_secret`（H4 信號用）

若只想模擬交易，設定 `trading.paper_mode: true`，BingX API key 不需要真實值。

---

### Step 5 — 通知 Bot（選填但建議）

熔斷、漏跳偵測、狀態報告都靠此推送。

1. 在 Telegram 找 @BotFather，建立 bot 取得 `bot_token`
2. 找 @userinfobot 查詢自己的 `chat_id`
3. 填入 `config.yaml` 的 `notify` 區塊

---

### Step 6 — 初始化資料庫

```bash
python -c "from src.config import load_config; from src.database import init_db; init_db(load_config()['database']['url'])"
```

---

### 啟動

**模擬模式（只需 Telegram API + 加入頻道）：**
```bash
python scripts/auto_trade.py --dry-run
```

**實盤：**
```bash
python scripts/auto_trade.py
```

**儀表板：**
```bash
streamlit run src/dashboard/app.py
```

**回測：**
```bash
python scripts/fetch_history.py   # 抓歷史訊息
python scripts/run_backtest.py    # 執行回測
```

---

## 設定速覽（config/config.yaml）

```yaml
telegram:
  api_id: ...
  api_hash: ...
  phone: "+886..."
  channels:
    - chat_id: -100xxxxxxx
      name: "CRT_SNIPER_CRYPTO"
      parser: "crt_sniper"

bingx:
  api_key: ...
  api_secret: ...
  is_demo: false
  sub_api_key: ...      # H4 子帳戶（選填）
  sub_api_secret: ...

trading:
  paper_mode: false
  h1_risk_per_trade: 0.01   # H1 每筆風險 1%
  h4_risk_per_trade: 0.01   # H4 每筆風險 1%

notify:
  bot_token: ...
  chat_ids: [...]
```

## 目錄結構

```
scripts/
  auto_trade.py         # 主程式（實盤 / 模擬）
  fetch_history.py      # 抓 TG 歷史訊息
  run_backtest.py       # 跑回測
  daily_report.py       # 日報產生
  health_check.py       # 系統健康檢查
src/
  parsers/
    crt_sniper_parser.py  # CRT Sniper 格式解析
    registry.py           # Parser 註冊表
  trader/
    bingx.py              # BingX 下單
    executor.py           # 執行引擎 + 持倉追蹤
    router.py             # Crypto / Forex 路由
    paper.py              # 模擬交易
  backtest/
    engine.py             # 回測核心
  dashboard/
    app.py                # Streamlit 主頁
STRATEGY_CHANGELOG.md   # 策略演進紀錄
```

## 策略現況

詳見 [STRATEGY_CHANGELOG.md](STRATEGY_CHANGELOG.md)

| 版本 | 日期 | 重點 |
|------|------|------|
| v1 | 2026-03-20 | 基礎策略上線，風險 2%，TP3/TP4 各 50% |
| v2 | 2026-03-25 | H1/H4 差異化風險，分批進場，熔斷機制 |
| — | 2026-04-04 | 移除移動止盈，加回 TP2 保本 |
| — | 2026-04-05 | 新增 CRT sweep 過濾器 |

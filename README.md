# Signal Verifier

Telegram 信號頻道全自動驗證系統

用固定、可驗證、可重現的規則，審判一個信號頻道到底有沒有 edge。

## 核心功能

1. **Telegram 訊息抓取** — 歷史回補 + 即時監聽
2. **訊號解析** — 可替換 parser，支援多頻道格式
3. **市場資料** — 自動抓取對應 K 線（Binance / 其他）
4. **回測引擎** — 多種出場規則，保守/樂觀模式
5. **績效統計** — 勝率、RR、期望值、回撤、Equity Curve
6. **儀表板** — Streamlit 本地 Dashboard

## 快速開始

```bash
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
# 填入 Telegram API credentials
python scripts/fetch_history.py
python scripts/run_backtest.py
streamlit run src/dashboard/app.py
```

## 開發階段

- **Phase 1 (MVP)**: 抓訊息 → 解析 → K線 → 單一規則回測 → 統計報表
- **Phase 2**: 分批止盈、保本、update 關聯、多資料源、Dashboard
- **Phase 3**: 品質評分、最佳規則搜尋、時段分析、一致性驗證
